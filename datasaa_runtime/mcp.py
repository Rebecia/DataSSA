from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    if not isinstance(options, list):
        return None
    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)
    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def normalize_schema_for_tools(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    normalized: dict[str, Any] = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            merged["nullable"] = True
            normalized = merged
            break

    if isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {
            k: normalize_schema_for_tools(v) if isinstance(v, dict) else v
            for k, v in normalized["properties"].items()
        }
    if isinstance(normalized.get("items"), dict):
        normalized["items"] = normalize_schema_for_tools(normalized["items"])

    if normalized.get("type") != "object":
        return normalized
    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class MCPTool:
    def __init__(self, *, session: Any, server_name: str, tool_def: Any, timeout_s: int = 30) -> None:
        self._session = session
        self._raw_name = tool_def.name
        self._name = f"mcp_{server_name}_{tool_def.name}"
        self._description = tool_def.description or tool_def.name
        self._parameters = normalize_schema_for_tools(
            tool_def.inputSchema or {"type": "object", "properties": {}}
        )
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._raw_name, arguments=kwargs),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            return f"(MCP tool call timed out after {self._timeout_s}s)"
        except Exception as exc:
            return f"(MCP tool call failed: {type(exc).__name__}: {exc})"

        parts: list[str] = []
        for block in result.content or []:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(parts) or "(no output)"


async def connect_mcp_stdio_server(
    *,
    stack: AsyncExitStack,
    server_name: str,
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> Any:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command, args=args, env=env or None, cwd=cwd)
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session

