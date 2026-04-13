"""Cron tool for scheduling reminders and tasks.

中文速读：
- 这个文件提供一个名为 `cron` 的工具，让 LLM 能“创建/查看/删除”定时任务。
- 它本身不负责计时与触发（那是 `CronService` 的事），CronTool 只做两件事：
  1) 把“用户自然语言的需求”映射成结构化的 `CronSchedule`
  2) 调用 `CronService.add_job/list_jobs/remove_job` 做持久化与调度
- 重点注意：`ContextVar _in_cron_context` 用来防止“在 cron 回调里再次创建 cron”
  形成无限递归/自我触发（属于防御性设计）。
"""

from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJobState, CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        # 这两个字段由 AgentLoop 在每轮处理前通过 set_context(...) 注入，
        # 用来决定“任务触发时要把消息投递到哪个渠道/哪个 chat”。
        self._channel = ""
        self._chat_id = ""
        # ContextVar：协程上下文变量（类似线程局部变量，但适用于 async）。
        # 用于标记“当前是否在 cron job 的执行回调里”。
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        # 中文解释：记录当前对话的路由目标，后续 add_job 才知道 deliver 到哪里
        self._channel = channel
        self._chat_id = chat_id

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        # 返回 token：调用方可在 finally 里 reset 回去（见 commands.py gateway 的 on_cron_job）
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Schedule reminders and recurring tasks. Actions: add, list, remove."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {"type": "string", "description": "Reminder message (for add)"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron expressions (e.g. 'America/Vancouver')",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')",
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        # action 相当于子命令：add/list/remove
        if action == "add":
            if self._in_cron_context.get():
                # 防止在“被定时任务触发的回调”里再创建新的定时任务（避免递归/滥用）
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(message, every_seconds, cron_expr, tz, at)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
    ) -> str:
        # 1) 基础校验：message 与路由上下文必须存在
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            # 2) tz 合法性校验：用 zoneinfo 查 IANA 时区名
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(tz)
            except (KeyError, Exception):
                return f"Error: unknown timezone '{tz}'"

        # Build schedule
        delete_after = False
        if every_seconds:
            # 每隔 N 秒触发一次（内部用毫秒）
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            # cron 表达式触发（可选时区）
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        elif at:
            from datetime import datetime

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            # 单次执行：转成 epoch 毫秒；并标记 delete_after_run（执行一次后自动删除）
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        # 3) 交给 CronService 创建 job（CronService 负责持久化与计时触发）
        job = self._cron.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=self._channel,
            to=self._chat_id,
            delete_after_run=delete_after,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    @staticmethod
    def _format_timing(schedule: CronSchedule) -> str:
        """Format schedule as a human-readable timing string."""
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            dt = datetime.fromtimestamp(schedule.at_ms / 1000, tz=timezone.utc)
            return f"at {dt.isoformat()}"
        return schedule.kind

    @staticmethod
    def _format_state(state: CronJobState) -> list[str]:
        """Format job run state as display lines."""
        lines: list[str] = []
        if state.last_run_at_ms:
            last_dt = datetime.fromtimestamp(state.last_run_at_ms / 1000, tz=timezone.utc)
            info = f"  Last run: {last_dt.isoformat()} — {state.last_status or 'unknown'}"
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            next_dt = datetime.fromtimestamp(state.next_run_at_ms / 1000, tz=timezone.utc)
            lines.append(f"  Next run: {next_dt.isoformat()}")
        return lines

    def _list_jobs(self) -> str:
        # 列出所有 jobs（这里不按 channel/chat 过滤；是“全局列表”）
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            parts.extend(self._format_state(j.state))
            lines.append("\n".join(parts))
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        # 删除一个 job（按 id）
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
