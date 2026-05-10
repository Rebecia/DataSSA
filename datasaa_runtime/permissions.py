from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class UserPermission:
    """最小权限模型：用户可访问哪些 datasource。

    约定：
    - allowed_datasources 为空/None => 全允许（由上层解释）
    """

    user_id: str
    allowed_datasources: list[str] | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UserPermission":
        raw = data.get("allowed_datasources")
        if raw is None:
            allowed: list[str] | None = None
        elif isinstance(raw, list):
            allowed = [str(x) for x in raw if str(x).strip()]
        else:
            allowed = [str(raw)]
        return UserPermission(
            user_id=str(data.get("user_id") or ""),
            allowed_datasources=allowed,
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


class PermissionRegistry:
    """本地权限注册表（存储：workspace/permissions/index.json）。"""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace
        self.dir = (workspace / "permissions").resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = (self.dir / "index.json").resolve()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "users": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "users": {}}

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def get(self, user_id: str) -> UserPermission | None:
        uid = (user_id or "").strip()
        if not uid:
            return None
        data = self._load()
        users = data.get("users") or {}
        if not isinstance(users, dict):
            return None
        item = users.get(uid)
        if not isinstance(item, dict):
            return None
        perm = UserPermission.from_dict(item)
        return perm if perm.user_id else None

    def list(self) -> list[UserPermission]:
        data = self._load()
        users = data.get("users") or {}
        if not isinstance(users, dict):
            return []
        out: list[UserPermission] = []
        for uid, item in users.items():
            if isinstance(item, dict):
                perm = UserPermission.from_dict({"user_id": uid, **item})
                if perm.user_id:
                    out.append(perm)
        return sorted(out, key=lambda p: p.user_id)

    def upsert(self, perm: UserPermission) -> None:
        uid = (perm.user_id or "").strip()
        if not uid:
            raise ValueError("user_id is required")
        now = datetime.now().isoformat()
        if not perm.created_at:
            perm.created_at = now
        perm.updated_at = now

        data = self._load()
        users = data.get("users")
        if not isinstance(users, dict):
            users = {}
        users[uid] = {
            "allowed_datasources": perm.allowed_datasources,
            "created_at": perm.created_at,
            "updated_at": perm.updated_at,
        }
        data["users"] = users
        self._save(data)

    def delete(self, user_id: str) -> bool:
        uid = (user_id or "").strip()
        if not uid:
            return False
        data = self._load()
        users = data.get("users")
        if not isinstance(users, dict):
            users = {}
        existed = uid in users
        users.pop(uid, None)
        data["users"] = users
        self._save(data)
        return existed
