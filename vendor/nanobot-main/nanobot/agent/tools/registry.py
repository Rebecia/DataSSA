"""Tool registry for dynamic tool management."""

from typing import Any

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.

    中文速读：
    - 这里是“工具中枢”：负责管理有哪些 Tool、把它们转成 LLM 能理解的 tool schema，
      以及在 LLM 产生 tool_calls 后，按 name 找到对应工具并执行。
    - AgentLoop 会调用 `get_definitions()` 把工具列表传给模型；当模型要求调用工具时，
      AgentLoop 会调用 `execute(name, params)` 得到结果，再把结果作为 role=tool 回填进 messages。
    """

    def __init__(self):
        # name -> Tool 实例
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool.

        中文解释：把一个 Tool 实例按 `tool.name` 放进注册表。
        """
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name.

        中文解释：按工具名移除（不存在也不报错）。
        """
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name.

        中文解释：取工具实例；没有则返回 None。
        """
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered.

        中文解释：判断工具名是否已注册。
        """
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format.

        中文解释：把每个 Tool 的 `to_schema()` 汇总成 tools 列表（给 LLM 的“工具说明书”）。
        注意：这里不执行工具，只是暴露“工具名称/参数/描述”等静态定义。
        """
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """Execute a tool by name with given parameters.

        中文解释：按 name 找到 Tool，并把 LLM 提供的参数 params：
        1) cast 成正确类型（比如把 "1" 转成 int）
        2) 按 schema 校验（缺字段/类型错等）
        3) await tool.execute(**params) 真正执行
        返回值会作为 role=tool 的 content 回填给 LLM（由 AgentLoop 负责回填）。
        """
        # 给模型的“二次提示”：如果工具报错，鼓励它读懂错误信息并换一种做法（很实用的提示工程）
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            # 未注册工具：把可用工具名列出来，方便模型/开发者修正工具名
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            # 1) 参数类型“纠偏”：把 LLM 传来的 JSON 值尽量转为工具期望的类型
            params = tool.cast_params(params)

            # 2) 参数校验：返回错误列表（字符串），有错误就不要执行工具
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT

            # 3) 真正执行工具（异步）：注意这里用 **params 展开成关键字参数
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                # 工具自己用字符串返回错误：同样附加 _HINT 让模型换策略
                return result + _HINT
            return result
        except Exception as e:
            # 兜底异常：不要让工具异常把主循环打崩，返回错误字符串给模型自我修正
            return f"Error executing {name}: {str(e)}" + _HINT

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names.

        中文解释：返回当前注册表里所有工具的名字（用于提示/调试/报错信息）。
        """
        return list(self._tools.keys())

    def __len__(self) -> int:
        # 语法点：实现 len(registry)
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        # 语法点：实现 `"exec" in registry`
        return name in self._tools
