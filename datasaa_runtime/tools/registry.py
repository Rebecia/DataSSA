from __future__ import annotations

from typing import Any

from .base import Tool, ToolDef


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._allowlist: set[str] | None = None
        self._denylist: set[str] = set()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def configure_access(
        self,
        *,
        allowed: set[str] | None = None,
        denied: set[str] | None = None,
    ) -> None:
        """配置工具访问控制（allow/deny）。

        规则：
        - allowlist=None 表示默认允许全部（再叠加 denylist）
        - allowlist=set(...) 表示只允许该集合中的工具（再叠加 denylist）
        - denylist 优先级最高：即使在 allowlist 中也会被禁止
        """
        self._allowlist = allowed
        self._denylist = set(denied or set())

    def is_allowed(self, name: str) -> bool:
        if name in self._denylist:
            return False
        if self._allowlist is None:
            return True
        return name in self._allowlist

    def get(self, name: str) -> Tool | None:
        if not self.is_allowed(name):
            return None
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return [t for t in self._tools.values() if self.is_allowed(t.name)]

    def definitions(self) -> list[dict[str, Any]]:
        defs: list[dict[str, Any]] = []
        for tool in self.list():
            defs.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return defs

    def as_tool_defs(self) -> list[ToolDef]:
        return [
            ToolDef(name=t.name, description=t.description, parameters=t.parameters)
            for t in self.list()
        ]
