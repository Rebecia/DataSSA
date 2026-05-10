from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .datasources import Datasource
from .mcp import MCPTool, connect_mcp_stdio_server
from .tools.registry import ToolRegistry


@dataclass
class _MCPClient:
    stack: AsyncExitStack
    session: Any


class MCPManager:
    """按 datasource_id 维护 MCP stdio 子进程与连接池（复用，避免每次请求重启）。

    设计要点：
    - 每个 datasource 对应一个 MCP 子进程（stdio），并复用连接
    - MCP tools 会注册为：mcp_{datasource_id}_{tool_name}
    - 通过 env 把 datasource 的策略（timeout/max_rows/readonly/并发）下发给 MCP server
    """

    def __init__(
        self,
        *,
        project_root: Path,
        tools: ToolRegistry,
        command: str = "python3",
        args: list[str] | None = None,
        audit_log_path: str = "./audit.log",
    ) -> None:
        self.project_root = project_root.resolve()
        self.tools = tools
        self.command = command
        self.args = args or ["mcp_server/db_server.py"]
        self.audit_log_path = audit_log_path

        self._clients: dict[str, _MCPClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, dsid: str) -> asyncio.Lock:
        if dsid not in self._locks:
            self._locks[dsid] = asyncio.Lock()
        return self._locks[dsid]

    async def ensure_connected(self, ds: Datasource) -> None:
        """确保某个 datasource 的 MCP 连接已建立，并把工具注册到 ToolRegistry。"""
        dsid = ds.id
        if dsid in self._clients:
            return

        async with self._lock(dsid):
            if dsid in self._clients:
                return

            if ds.type != "sqlite":
                # 先预留字段：postgres/mysql 暂不实现连接（后续 Multi-DB 再做）
                raise RuntimeError(f"datasource type not implemented yet: {ds.type}")
            if not ds.sqlite_path:
                raise RuntimeError("sqlite datasource missing sqlite_path")

            stack = AsyncExitStack()
            await stack.__aenter__()
            try:
                env: dict[str, str] = {
                    "DB_PATH": ds.sqlite_path,
                    "QUERY_TIMEOUT": str(ds.query_timeout_s),
                    "MAX_ROWS": str(ds.max_rows),
                    "DB_READONLY": "true" if ds.readonly else "false",
                    "MAX_CONCURRENT": str(ds.max_concurrent),
                    "AUDIT_LOG_PATH": self.audit_log_path,
                    "DATASSA_DATASOURCE_ID": dsid,
                }
                session = await connect_mcp_stdio_server(
                    stack=stack,
                    server_name=dsid,
                    command=self.command,
                    args=self.args,
                    env=env,
                    cwd=str(self.project_root),
                )
                tools = await session.list_tools()
                for tool_def in tools.tools:
                    self.tools.register(MCPTool(session=session, server_name=dsid, tool_def=tool_def))

                self._clients[dsid] = _MCPClient(stack=stack, session=session)
            except Exception:
                await stack.aclose()
                raise

    async def close_all(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.stack.aclose()
            except Exception:
                pass
        self._clients.clear()
