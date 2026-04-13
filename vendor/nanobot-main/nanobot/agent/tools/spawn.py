"""Spawn tool for creating background subagents.

中文速读：
- 这是一个名为 `spawn` 的工具：让主 Agent 把某个“耗时/复杂但可并行”的任务交给子代理（subagent）去做。
- 子代理通常在后台运行，做完后把结果以 system message/回调的方式汇报回来（由 SubagentManager 负责实现）。
- 这个工具本身不实现“子代理怎么跑”，它只是把参数转发给 `SubagentManager.spawn(...)`。
"""

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        # 下面这些是“发起 spawn 的原始对话上下文”：
        # 子代理完成后要回到哪个 channel/chat_id 汇报，以及用哪个 session_key 归档历史。
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the origin context for subagent announcements."""
        # 中文解释：AgentLoop 每轮会调用 tools.set_context(...)，
        # 这样 spawn 出来的子代理知道“向谁汇报”和“写进哪个 session”。
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = f"{channel}:{chat_id}"

    @property
    def name(self) -> str:
        # 工具名：LLM 的 tool_call 里会用这个名字来调用
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        # 工具参数 schema：主参数是 task；label 仅用于显示/标记
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (for display)",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        """Spawn a subagent to execute the given task."""
        # 中文解释：直接把参数交给 SubagentManager。
        # 返回值通常是一个“已启动”的确认信息（或 subagent id），由 manager 决定格式。
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
            session_key=self._session_key,
        )
