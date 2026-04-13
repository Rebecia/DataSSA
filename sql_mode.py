from __future__ import annotations

"""
sql_mode.py

实现 “SQL 模式（只读、不调用 LLM）” 的交互与命令解析。

特点：
- 通过 MCP client 直连本项目的 database MCP server（stdio），所有查询依然会写入 audit.log
- 支持命令：/tables /desc /stats /sql /mode /help
- 支持直接输入 SELECT/WITH 开头的 SQL
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio


@dataclass(frozen=True)
class ModeState:
    session_id: str
    mode: str  # "sql" | "nl"


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _workspace_path_from_config(config_path: Path) -> Path:
    data = _load_json(config_path)
    workspace = (
        (data.get("agents") or {})
        .get("defaults", {})
        .get("workspace", "./workspace")
    )
    return (config_path.parent / workspace).resolve()


def _session_state_path(config_path: Path) -> Path:
    workspace = _workspace_path_from_config(config_path)
    return (workspace / "memory" / "session_state.json").resolve()


def read_session_mode(config_path: Path, session_id: str) -> str | None:
    state_path = _session_state_path(config_path)
    data = _load_json(state_path)
    sessions = data.get("sessions") or {}
    entry = sessions.get(session_id) or {}
    mode = entry.get("mode")
    if mode in {"sql", "nl"}:
        return mode
    return None


def write_session_mode(config_path: Path, session_id: str, mode: str) -> None:
    state_path = _session_state_path(config_path)
    data = _load_json(state_path)
    data.setdefault("sessions", {})
    data["sessions"][session_id] = {
        "mode": mode,
        "updated_at": datetime.now().isoformat(),
    }
    _atomic_write(state_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def prompt_mode_once() -> str:
    while True:
        print(
            "请选择对话模式（只需选择一次，会记住）：\n"
            "1) SQL 模式（不调用 LLM，速度快，只读）\n"
            "2) 自然语言模式（调用 LLM，自动生成 SQL 并解读）\n"
            "请输入 1 或 2：",
            end="",
            flush=True,
        )
        choice = (input() or "").strip()
        if choice == "1":
            return "sql"
        if choice == "2":
            return "nl"
        print("输入无效，请输入 1 或 2。")


def _is_select_or_with(sql: str) -> bool:
    cleaned = sql.lstrip("( \t\r\n")
    head = cleaned[:8].upper()
    return head.startswith("SELECT") or head.startswith("WITH")


def _parse_command(line: str) -> tuple[str, list[str]] | None:
    if not line.startswith("/"):
        return None
    parts = line.strip().split()
    if not parts:
        return None
    cmd = parts[0][1:].lower()
    return cmd, parts[1:]


def help_text() -> str:
    return (
        "SQL 模式（只读，不调用 LLM）\n"
        "- /tables                 列出表\n"
        "- /desc <table>           查看表结构\n"
        "- /stats <table>          查看统计信息\n"
        "- /sql <SELECT/WITH...>   执行只读 SQL\n"
        "- 直接输入 SELECT/WITH... 也可执行\n"
        "- /mode                   查看当前模式\n"
        "- /mode nl                切换到自然语言模式\n"
        "- exit                    退出\n"
    )


async def _call_tool(session, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await session.call_tool(name, arguments=arguments or {})
    text_parts: list[str] = []
    for item in result.content or []:
        if getattr(item, "type", None) == "text":
            text_parts.append(getattr(item, "text", "") or "")
    raw = "\n".join(text_parts).strip()
    if not raw:
        return {"success": True, "raw": ""}
    try:
        return json.loads(raw)
    except Exception:
        return {"success": True, "raw": raw}


def _print_result(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def sql_repl(
    config_path: Path,
    session_id: str,
    once_message: str | None = None,
) -> str:
    """
    SQL 模式主循环。

    返回值：
    - "nl"：表示用户切换到了自然语言模式（上层应交给 nanobot 接管）
    - "exit"：退出
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cfg = _load_json(config_path)
    mcp_servers = ((cfg.get("tools") or {}).get("mcpServers") or {})
    db_cfg = mcp_servers.get("database") or {}
    if not db_cfg:
        raise RuntimeError("config.json 未配置 tools.mcpServers.database")

    command = db_cfg.get("command") or "python3"
    args = db_cfg.get("args") or ["mcp_server/db_server.py"]
    env = db_cfg.get("env") or {}
    cwd = str(config_path.parent.resolve())

    params = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()

            async def handle_line(line: str) -> str | None:
                line = (line or "").strip()
                if not line:
                    return None
                if line.lower() in {"exit", "quit"}:
                    return "exit"

                # 兼容用户习惯：允许直接输入 `mode nl` / `model nl`（不加 `/`）
                lowered = line.lower()
                if lowered.startswith("mode ") or lowered.startswith("model "):
                    # 标准化为 /mode ...
                    parts = line.split(maxsplit=1)
                    tail = parts[1] if len(parts) == 2 else ""
                    line = f"/mode {tail}".strip()

                parsed = _parse_command(line)
                if parsed is not None:
                    cmd, argv = parsed
                    if cmd == "help":
                        print(help_text())
                        return None
                    if cmd == "mode":
                        if not argv:
                            print(f"当前模式：sql（session={session_id}）")
                            return None
                        target = argv[0].lower()
                        if target in {"nl", "sql"}:
                            # SQL REPL 内切换模式依然记到 session_state（方便当前 session 里生效）
                            write_session_mode(config_path, session_id, target)
                            if target == "nl":
                                print("已切换到自然语言模式。")
                                return "nl"
                            print("已切换到 SQL 模式。")
                            return None
                        print("用法：/mode nl 或 /mode sql")
                        return None
                    if cmd == "tables":
                        res = await _call_tool(mcp, "list_tables")
                        _print_result(res)
                        return None
                    if cmd == "desc":
                        if not argv:
                            print("用法：/desc <table>")
                            return None
                        res = await _call_tool(mcp, "describe_table", {"table_name": argv[0]})
                        _print_result(res)
                        return None
                    if cmd == "stats":
                        if not argv:
                            print("用法：/stats <table>")
                            return None
                        res = await _call_tool(mcp, "get_statistics", {"table_name": argv[0]})
                        _print_result(res)
                        return None
                    if cmd == "sql":
                        sql = line[len("/sql") :].strip()
                        if not sql:
                            print("用法：/sql <SELECT/WITH ...>")
                            return None
                        res = await _call_tool(mcp, "query_database", {"sql": sql})
                        _print_result(res)
                        return None

                    print("未知命令。输入 /help 查看可用命令。")
                    return None

                # 直接 SQL
                if _is_select_or_with(line):
                    res = await _call_tool(mcp, "query_database", {"sql": line})
                    _print_result(res)
                    return None

                print("SQL 模式只接受只读查询。输入 /help 查看可用命令。")
                return None

            if once_message is not None:
                outcome = await handle_line(once_message)
                return outcome or "exit"

            print("SQL 模式（只读，不调用 LLM）。输入 /help 查看命令；输入 /mode nl 切换；输入 exit 退出。")
            while True:
                try:
                    line = await anyio.to_thread.run_sync(lambda: input("SQL> "))
                except (EOFError, KeyboardInterrupt):
                    return "exit"
                outcome = await handle_line(line)
                if outcome in {"nl", "exit"}:
                    return outcome


def decide_mode(
    *,
    config_path: Path,
    session_id: str,
    explicit_mode: str | None,
    interactive: bool,
) -> str:
    if explicit_mode in {"sql", "nl"}:
        # 显式指定模式：直接使用，并写入 session_state（方便后续命令内读取）
        write_session_mode(config_path, session_id, explicit_mode)
        return explicit_mode

    if not interactive:
        # 单次消息默认仍走自然语言（保持原行为）
        return "nl"

    # 交互模式：每次启动都重新询问（不自动沿用上次选择）
    mode = prompt_mode_once()
    write_session_mode(config_path, session_id, mode)
    return mode
