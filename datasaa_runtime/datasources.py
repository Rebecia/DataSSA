from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


DatasourceType = Literal["sqlite", "postgres", "mysql"]


@dataclass
class Datasource:
    """运行时数据源配置（落盘到 workspace/datasources/index.json）。

    说明：
    - 当前 MVP 只实现 sqlite（sqlite_path）
    - postgres/mysql 仅预留 dsn 字段；真正实现需要扩展 MCP server 与连接管理
    - policy 字段是“数据源级默认策略”（readonly/timeout/max_rows/max_concurrent）
    """

    id: str
    type: DatasourceType
    enabled: bool = True
    display_name: str | None = None

    # sqlite
    sqlite_path: str | None = None

    # sql databases (reserved fields; implementation may come later)
    dsn: str | None = None  # e.g. postgres://user:pass@host:5432/db

    # policy defaults (can be overridden later per user/role)
    readonly: bool = True
    query_timeout_s: int = 30
    max_rows: int = 1000
    max_concurrent: int = 5

    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Datasource":
        return Datasource(
            id=str(data.get("id") or ""),
            type=data.get("type") or "sqlite",
            enabled=bool(data.get("enabled", True)),
            display_name=data.get("display_name"),
            sqlite_path=data.get("sqlite_path"),
            dsn=data.get("dsn"),
            readonly=bool(data.get("readonly", True)),
            query_timeout_s=int(data.get("query_timeout_s", 30)),
            max_rows=int(data.get("max_rows", 1000)),
            max_concurrent=int(data.get("max_concurrent", 5)),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


class DatasourceRegistry:
    """本地数据源注册表（存储：workspace/datasources/index.json）。"""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace
        self.dir = (workspace / "datasources").resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = (self.dir / "index.json").resolve()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "default": None, "items": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "default": None, "items": []}

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[Datasource]:
        data = self._load()
        out: list[Datasource] = []
        for item in data.get("items") or []:
            if isinstance(item, dict):
                ds = Datasource.from_dict(item)
                if ds.id:
                    out.append(ds)
        return out

    def get(self, ds_id: str) -> Datasource | None:
        ds_id = (ds_id or "").strip()
        if not ds_id:
            return None
        for ds in self.list():
            if ds.id == ds_id:
                return ds
        return None

    def get_default_id(self) -> str | None:
        data = self._load()
        v = data.get("default")
        return str(v) if v else None

    def set_default(self, ds_id: str | None) -> None:
        data = self._load()
        data["default"] = ds_id
        self._save(data)

    def upsert(self, ds: Datasource) -> None:
        now = datetime.now().isoformat()
        if not ds.created_at:
            ds.created_at = now
        ds.updated_at = now

        data = self._load()
        items = data.get("items") or []
        if not isinstance(items, list):
            items = []

        replaced = False
        new_items: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("id") == ds.id:
                new_items.append(ds.to_dict())
                replaced = True
            elif isinstance(item, dict):
                new_items.append(item)
        if not replaced:
            new_items.append(ds.to_dict())

        data["items"] = new_items
        if not data.get("default"):
            data["default"] = ds.id
        self._save(data)

    def delete(self, ds_id: str) -> bool:
        data = self._load()
        items = data.get("items") or []
        if not isinstance(items, list):
            items = []
        before = len(items)
        data["items"] = [it for it in items if not (isinstance(it, dict) and it.get("id") == ds_id)]
        if data.get("default") == ds_id:
            data["default"] = None
        self._save(data)
        return len(data["items"]) != before
