from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from datasaa_runtime.agent import AgentConfig, AgentRuntime
from datasaa_runtime.datasources import Datasource, DatasourceRegistry
from datasaa_runtime.mcp import MCPTool, connect_mcp_stdio_server
from datasaa_runtime.mcp_manager import MCPManager
from datasaa_runtime.permissions import PermissionRegistry, UserPermission
from datasaa_runtime.providers.openai_compat import OpenAICompatConfig, OpenAICompatProvider
from datasaa_runtime.querybus import QueryBus
from datasaa_runtime.skills.safe_query import SafeQueryTool
from datasaa_runtime.tools.registry import ToolRegistry


def _parse_tool_set(value: str | None) -> set[str]:
    v = (value or "").strip()
    if not v:
        return set()
    return {item.strip() for item in v.split(",") if item.strip()}


def _is_safe_id(value: str) -> bool:
    v = (value or "").strip()
    if not (1 <= len(v) <= 64):
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    return all(c in allowed for c in v)


def _resolve_project_path(p: str) -> Path:
    """将相对路径按项目根目录解析，用于 sqlite_path 校验/连接。"""
    raw = (p or "").strip()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (Path(".").resolve() / path).resolve()


class ChatRequest(BaseModel):
    # 固定格式：{user_id}:{session_id}
    # 例：alice:s1 / u_123:conv_001
    session_id: str = Field(default="api:default")
    datasource_id: str | None = Field(default=None, description="可选：本会话绑定的数据源 id（首次传入即绑定）")
    message: str

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        v = (value or "").strip()
        # 约束：必须包含且仅包含一个冒号，左右两段只允许字母数字、下划线、短横线，长度 1..64
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("session_id 必须为 {user_id}:{session_id} 格式（且只包含一个 ':'）")
        user_id, sid = parts[0].strip(), parts[1].strip()
        if not (1 <= len(user_id) <= 64 and 1 <= len(sid) <= 64):
            raise ValueError("session_id 两段长度必须在 1..64")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if any(c not in allowed for c in user_id) or any(c not in allowed for c in sid):
            raise ValueError("session_id 仅允许字母数字、下划线(_)与短横线(-)")
        return f"{user_id}:{sid}"


class ChatResponse(BaseModel):
    trace_id: str
    answer: str
    sql: str | None = None
    warnings: list[str] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    tools_used: list[str] = Field(default_factory=list)


class DatasourceIn(BaseModel):
    id: str
    type: str = Field(default="sqlite")  # sqlite|postgres|mysql
    enabled: bool = True
    display_name: str | None = None
    sqlite_path: str | None = None
    dsn: str | None = None
    readonly: bool = True
    query_timeout_s: int = 30
    max_rows: int = 1000
    max_concurrent: int = 5


class DatasourceOut(DatasourceIn):
    created_at: str | None = None
    updated_at: str | None = None


class EnabledIn(BaseModel):
    enabled: bool = True


class PermissionIn(BaseModel):
    user_id: str
    allowed_datasources: list[str] | None = Field(
        default=None, description="为空/None 表示全允许；否则为 datasource id 列表"
    )


def _require_admin(req: Request) -> None:
    """最小保护：当 DATASSA_ADMIN_TOKEN 设置后，写操作必须携带 Header: x-admin-token。"""
    token = (os.getenv("DATASSA_ADMIN_TOKEN") or "").strip()
    if not token:
        raise HTTPException(status_code=403, detail="Admin API disabled (set DATASSA_ADMIN_TOKEN to enable)")
    got = (req.headers.get("x-admin-token") or "").strip()
    if got != token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _is_admin(req: Request) -> bool:
    token = (os.getenv("DATASSA_ADMIN_TOKEN") or "").strip()
    if not token:
        return False
    got = (req.headers.get("x-admin-token") or "").strip()
    return got == token


def _build_runtime() -> AgentRuntime:
    workspace = os.getenv("DATASSA_WORKSPACE", "./workspace").strip() or "./workspace"
    api_base = os.getenv("LLM_API_BASE", "https://api.deepseek.com").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "deepseek-reasoner").strip()

    # Test-mode: allow local smoke/concurrency tests without external LLM.
    if os.getenv("DATASSA_TEST_MODE", "").strip().lower() in {"1", "true", "yes"}:
        class _DummyProvider:
            is_dummy = True

            async def chat(self, **kwargs):  # type: ignore[no-untyped-def]
                # Always delegate to the workflow skill.
                msg = (kwargs.get("messages") or [])[-1]
                question = (msg.get("content") if isinstance(msg, dict) else "") or ""
                datasource_id = (os.getenv("DATASSA_TEST_DATASOURCE_ID") or "").strip() or None
                return {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_safe_query_1",
                                        "type": "function",
                                        "function": {
                                            "name": "safe_query_run",
                                            "arguments": json.dumps(
                                                {"question": question, **({"datasource_id": datasource_id} if datasource_id else {})},
                                                ensure_ascii=False,
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }

        provider = _DummyProvider()
    else:
        provider = OpenAICompatProvider(OpenAICompatConfig(api_base=api_base, api_key=api_key, model=model))

    tools = ToolRegistry()
    # Tool access control (P0):
    # - DATASSA_ENABLED_TOOLS="toolA,toolB" => only allow these (denylist still applies)
    # - DATASSA_DISABLED_TOOLS="toolX,toolY" => deny these
    enabled = _parse_tool_set(os.getenv("DATASSA_ENABLED_TOOLS"))
    disabled = _parse_tool_set(os.getenv("DATASSA_DISABLED_TOOLS"))
    tools.configure_access(allowed=(enabled or None), denied=disabled)

    # Lazily connect MCP in lifespan; we keep stack on app.state
    cfg = AgentConfig(workspace=str(Path(workspace).resolve()))
    runtime = AgentRuntime(cfg=cfg, provider=provider, tools=tools)
    runtime.mcp_manager = MCPManager(
        project_root=Path(".").resolve(),
        tools=runtime.tools,
        command=os.getenv("MCP_DB_COMMAND", "python3").strip() or "python3",
        args=(os.getenv("MCP_DB_ARGS", "mcp_server/db_server.py").split()),
        audit_log_path=os.getenv("AUDIT_LOG_PATH", "./audit.log"),
    )
    # Phase 1: global QueryBus (execute only)
    runtime.querybus = QueryBus(tools=runtime.tools, worker_count=int(os.getenv("DATASSA_QUERYBUS_WORKERS", "4")), per_datasource_limit=4)
    # querybus 启动
    runtime.querybus.start()
    # Register the workflow skill (single entry) so any agent can call it.
    runtime.tools.register(SafeQueryTool(provider=provider, tools=runtime.tools, model=model, querybus=runtime.querybus))
    return runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime: AgentRuntime = _build_runtime()
    app.state.runtime = runtime

    yield

    try:
        if getattr(runtime, "querybus", None):
            await runtime.querybus.close()
        if getattr(runtime, "mcp_manager", None):
            await runtime.mcp_manager.close_all()
    except Exception:
        pass


app = FastAPI(title="DataSSA API", version="0.1.0", lifespan=lifespan)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# 说明：
# - /login /app /admin 为三套静态页面入口
# - /chat 为用户查询入口（只读）
# - /admin/* 为管理员配置入口（需要 DATASSA_ADMIN_TOKEN + x-admin-token）
# - /replay/* 为复盘入口（用户只读、管理员可看全量）


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/login")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "login.html")


@app.get("/app")
def app_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "app.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "admin.html")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/admin/datasources")
def admin_list_datasources() -> dict[str, Any]:
    runtime: AgentRuntime = app.state.runtime
    reg = DatasourceRegistry(runtime.workspace)
    items = [DatasourceOut(**ds.to_dict()).model_dump() for ds in reg.list()]
    return {"default": reg.get_default_id(), "items": items}


@app.post("/admin/datasources")
def admin_upsert_datasource(req: Request, body: DatasourceIn) -> dict[str, Any]:
    _require_admin(req)
    runtime: AgentRuntime = app.state.runtime
    reg = DatasourceRegistry(runtime.workspace)
    ds = Datasource.from_dict(body.model_dump())
    if not ds.id:
        raise HTTPException(status_code=400, detail="datasource id is required")
    if not _is_safe_id(ds.id):
        raise HTTPException(status_code=400, detail="datasource id 仅允许字母数字、下划线(_)与短横线(-)，长度 1..64")
    if ds.type not in {"sqlite", "postgres", "mysql"}:
        raise HTTPException(status_code=400, detail=f"unsupported datasource type: {ds.type}")

    # P1-2：最小字段校验（避免出现 sqlite_path=null 这种不可用配置）
    if ds.type == "sqlite":
        if not (ds.sqlite_path or "").strip():
            raise HTTPException(status_code=400, detail="sqlite datasource requires sqlite_path")
        resolved = _resolve_project_path(ds.sqlite_path or "")
        if not resolved.exists() or not resolved.is_file():
            raise HTTPException(status_code=400, detail=f"sqlite_path not found: {ds.sqlite_path}")
    else:
        # postgres/mysql 目前仅预留字段；如果配置了也不能用于 /chat（会返回 501）
        if not (ds.dsn or "").strip():
            raise HTTPException(status_code=400, detail=f"{ds.type} datasource requires dsn (field reserved)")

    reg.upsert(ds)
    out = reg.get(ds.id)
    return {"item": DatasourceOut(**(out.to_dict() if out else ds.to_dict())).model_dump(), "default": reg.get_default_id()}


@app.delete("/admin/datasources/{ds_id}")
def admin_delete_datasource(req: Request, ds_id: str) -> dict[str, Any]:
    _require_admin(req)
    runtime: AgentRuntime = app.state.runtime
    reg = DatasourceRegistry(runtime.workspace)
    ok = reg.delete(ds_id)
    return {"deleted": ok, "default": reg.get_default_id()}


@app.post("/admin/datasources/{ds_id}/default")
def admin_set_default_datasource(req: Request, ds_id: str) -> dict[str, Any]:
    _require_admin(req)
    runtime: AgentRuntime = app.state.runtime
    reg = DatasourceRegistry(runtime.workspace)
    if reg.get(ds_id) is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    reg.set_default(ds_id)
    return {"default": reg.get_default_id()}


@app.post("/admin/datasources/{ds_id}/enabled")
def admin_set_datasource_enabled(req: Request, ds_id: str, body: EnabledIn) -> dict[str, Any]:
    _require_admin(req)
    runtime: AgentRuntime = app.state.runtime
    reg = DatasourceRegistry(runtime.workspace)
    ds = reg.get(ds_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    ds.enabled = bool(body.enabled)
    reg.upsert(ds)
    out = reg.get(ds_id)
    return {"item": DatasourceOut(**(out.to_dict() if out else ds.to_dict())).model_dump(), "default": reg.get_default_id()}


@app.post("/admin/datasources/{ds_id}/test")
async def admin_test_datasource(req: Request, ds_id: str) -> dict[str, Any]:
    """只读连接测试：用于管理员在前端确认该 datasource 可用。"""
    _require_admin(req)
    runtime: AgentRuntime = app.state.runtime
    reg = DatasourceRegistry(runtime.workspace)
    ds = reg.get(ds_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="datasource not found")

    try:
        await runtime.mcp_manager.ensure_connected(ds)
    except RuntimeError as exc:
        msg = str(exc)
        code = 501 if "not implemented yet" in msg else 400
        raise HTTPException(status_code=code, detail=msg)

    # list_tables + simple SELECT 1 (via query_database) to prove query path works.
    prefix = f"mcp_{ds_id}_"
    list_tool = runtime.tools.get(prefix + "list_tables")
    query_tool = runtime.tools.get(prefix + "query_database")
    if list_tool is None or query_tool is None:
        raise HTTPException(status_code=500, detail="mcp tools not registered")

    raw_tables = await list_tool.execute()
    raw_ping = await query_tool.execute(sql="SELECT 1 AS ok", max_rows=1)
    try:
        tables = json.loads(raw_tables)
    except Exception:
        tables = {"raw": raw_tables}
    try:
        ping = json.loads(raw_ping)
    except Exception:
        ping = {"raw": raw_ping}
    return {"ok": True, "datasource_id": ds_id, "type": ds.type, "tables": tables, "ping": ping}


@app.get("/admin/permissions")
def admin_list_permissions(req: Request) -> dict[str, Any]:
    _require_admin(req)
    runtime: AgentRuntime = app.state.runtime
    reg = PermissionRegistry(runtime.workspace)
    items = [p.to_dict() for p in reg.list()]
    return {"items": items}


@app.post("/admin/permissions")
def admin_upsert_permission(req: Request, body: PermissionIn) -> dict[str, Any]:
    _require_admin(req)
    runtime: AgentRuntime = app.state.runtime
    reg = PermissionRegistry(runtime.workspace)
    uid = (body.user_id or "").strip()
    if not uid or not _is_safe_id(uid):
        raise HTTPException(status_code=400, detail="user_id 不合法：仅允许字母数字、下划线(_)与短横线(-)，长度 1..64")
    allowed = body.allowed_datasources
    if allowed is not None:
        allowed = [x for x in [str(i).strip() for i in allowed] if x]
    perm = UserPermission(user_id=uid, allowed_datasources=allowed)
    reg.upsert(perm)
    out = reg.get(uid)
    return {"item": (out.to_dict() if out else perm.to_dict())}


@app.delete("/admin/permissions/{user_id}")
def admin_delete_permission(req: Request, user_id: str) -> dict[str, Any]:
    _require_admin(req)
    runtime: AgentRuntime = app.state.runtime
    reg = PermissionRegistry(runtime.workspace)
    ok = reg.delete(user_id)
    return {"deleted": ok}


@app.get("/permissions/{user_id}")
def get_user_permission(user_id: str) -> dict[str, Any]:
    """用户侧只读：获取自己的 datasource 权限（未配置 => 全允许）。"""
    runtime: AgentRuntime = app.state.runtime
    reg = PermissionRegistry(runtime.workspace)
    perm = reg.get(user_id)
    if perm is None:
        return {"user_id": user_id, "allowed_datasources": None}
    return {"user_id": perm.user_id, "allowed_datasources": perm.allowed_datasources}


def _iter_trace_files(root: Path, *, date: str | None = None) -> list[Path]:
    base = (root / "traces").resolve()
    if not base.exists():
        return []
    if date:
        d = (base / date).resolve()
        return sorted(d.glob("trace_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
    files = list(base.glob("*/*.jsonl"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _read_trace_events(path: Path, *, limit_lines: int | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            if limit_lines is not None and i >= limit_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


@app.get("/replay/traces")
def replay_list_traces(
    req: Request,
    user_id: str | None = None,
    datasource_id: str | None = None,
    date: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """trace 列表：User 只能查自己的；Admin 可查全部。"""
    runtime: AgentRuntime = app.state.runtime
    is_admin = _is_admin(req)
    if not is_admin:
        # user 侧必须给 user_id，且只能看自己的（best-effort）
        uid = (user_id or "").strip()
        if not uid:
            raise HTTPException(status_code=400, detail="user_id is required for non-admin replay")
        user_id = uid

    limit = max(1, min(int(limit), 200))
    dsid = (datasource_id or "").strip() or None
    uid_filter = (user_id or "").strip() or None

    items: list[dict[str, Any]] = []
    for path in _iter_trace_files(runtime.workspace, date=date)[: max(limit * 5, 200)]:
        events = _read_trace_events(path, limit_lines=6)
        if not events:
            continue
        first = events[0]
        t_id = first.get("trace_id") or path.stem
        sess = first.get("session_id")
        uid = first.get("user_id")
        ds = first.get("datasource_id")
        ts = first.get("ts")

        if uid_filter and uid != uid_filter:
            continue
        if dsid and ds != dsid:
            continue

        question = None
        for ev in events:
            if ev.get("type") == "nl_input":
                payload = ev.get("payload") or {}
                question = payload.get("message")
                break

        items.append(
            {
                "trace_id": t_id,
                "ts": ts,
                "session_id": sess,
                "user_id": uid,
                "datasource_id": ds,
                "question": question,
            }
        )
        if len(items) >= limit:
            break

    return {"items": items}


@app.get("/replay/trace/{trace_id}")
def replay_get_trace(req: Request, trace_id: str, user_id: str | None = None, date: str | None = None) -> dict[str, Any]:
    """trace 详情：User 只能读自己的（best-effort），Admin 可读全部。"""
    runtime: AgentRuntime = app.state.runtime
    is_admin = _is_admin(req)
    if not is_admin:
        uid = (user_id or "").strip()
        if not uid:
            raise HTTPException(status_code=400, detail="user_id is required for non-admin replay")

    # find trace file
    candidates = _iter_trace_files(runtime.workspace, date=date)
    target: Path | None = None
    for p in candidates:
        if p.stem == trace_id:
            target = p
            break
    if target is None:
        raise HTTPException(status_code=404, detail="trace not found")

    events = _read_trace_events(target)
    if not events:
        raise HTTPException(status_code=404, detail="trace empty")

    if not is_admin:
        uid = (user_id or "").strip()
        if events[0].get("user_id") != uid:
            raise HTTPException(status_code=403, detail="forbidden")

    return {"trace_id": trace_id, "events": events, "path": str(target)}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    runtime: AgentRuntime = app.state.runtime
    # resolve datasource:
    reg = DatasourceRegistry(runtime.workspace)
    dsid = req.datasource_id or runtime.sessions.load(req.session_id).datasource_id or reg.get_default_id() or "db1"
    ds = reg.get(dsid)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"datasource not found: {dsid}")
    if not ds.enabled:
        raise HTTPException(status_code=403, detail=f"datasource disabled: {dsid}")
    if ds.type == "sqlite" and not (ds.sqlite_path or "").strip():
        raise HTTPException(status_code=400, detail=f"sqlite_path not configured for datasource: {dsid}")

    # ensure MCP connected for this datasource
    try:
        await runtime.mcp_manager.ensure_connected(ds)
    except Exception as exc:
        msg = str(exc) or type(exc).__name__
        code = 501 if "not implemented yet" in msg else 400
        raise HTTPException(status_code=code, detail=f"datasource connect failed: {msg}")

    try:
        out = await runtime.process(session_key=req.session_id, user_message=req.message, datasource_id=dsid)
    except Exception as exc:
        msg = str(exc) or type(exc).__name__
        raise HTTPException(status_code=500, detail=f"runtime error: {msg}")
    # MVP contract:
    # - `answer`: markdown/text for frontend
    # - `sql`: optional (default include when available)
    return ChatResponse(
        trace_id=out.get("trace_id", ""),
        answer=out.get("content", "") or "",
        sql=out.get("sql"),
        warnings=out.get("warnings") or [],
        artifacts=out.get("artifacts") or {},
        tools_used=out.get("tools_used") or [],
    )
