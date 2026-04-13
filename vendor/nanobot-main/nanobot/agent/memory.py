"""Memory system for persistent agent memory.

中文速读（后续做 RAG/知识库也会用到这些思路）：
- 这里的 memory 指“会话太长时的归档/压缩”，不是向量检索。
- 两层持久化文件（在 workspace/memory/）：
  - `MEMORY.md`：长期事实（long-term facts）
  - `HISTORY.md`：可 grep 的时间线日志（每条以 [YYYY-MM-DD HH:MM] 开头）
- 两个核心类：
  1) `MemoryStore`：负责读写 MEMORY/HISTORY，以及一次 consolidation（让 LLM 产出更新）
  2) `MemoryConsolidator`：负责策略与并发控制（按 token 预算决定何时/归档多少）
"""

from __future__ import annotations

import asyncio
import json
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    # 有些 provider 的工具返回可能是 dict/list；落盘前统一转成可写入的文本
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    # provider 可能返回 str / list / dict，这里归一化成 dict（匹配 save_memory 的参数结构）
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None

_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(m in text for m in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._consecutive_failures = 0

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate the provided message chunk into MEMORY.md + HISTORY.md."""
 
        # - messages 是“要归档的一段历史对话片段”
        # - 这里会调用 LLM，并强制它调用 save_memory 工具，返回：
        #   - history_entry：写入 HISTORY.md（时间线摘要，便于 grep）
        #   - memory_update：写入 MEMORY.md（完整的新版本，包含旧内容 + 新内容）
        if not messages:
            return True

        # 读旧 memeory
        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        # 发给 LLM 的输入：system 规定职责 + user 提供当前 memory + 待处理片段
        chat_messages = [
            {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
            {"role": "user", "content": prompt},
        ]

        try:
            # tool_choice=forced：强制模型必须调用 save_memory（有的 provider 不支持，会在下面降级）
            forced = {"type": "function", "function": {"name": "save_memory"}}
            # 这里的 save_memory tool不是真实的tool，知识给了模型 tool 的参数和可返回的字段
            # 后续的执行是通过解析response.tool_calls[0].arguments，来解析参数，看是history_entry还是memory_update
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice=forced,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(
                response.content
            ):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages)

            # 在这里进行参数解析
            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages)

            if "history_entry" not in args or "memory_update" not in args:
                logger.warning("Memory consolidation: save_memory payload missing required fields")
                return self._fail_or_raw_archive(messages)

            entry = args["history_entry"]
            update = args["memory_update"]

            if entry is None or update is None:
                logger.warning("Memory consolidation: save_memory payload contains null required fields")
                return self._fail_or_raw_archive(messages)

            entry = _ensure_text(entry).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return self._fail_or_raw_archive(messages)

            # 落盘
            self.append_history(entry)
            update = _ensure_text(update)
            if update != current_memory:
                self.write_long_term(update)

            self._consecutive_failures = 0  # 成功则清空连续失败计数
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages)

    def _fail_or_raw_archive(self, messages: list[dict]) -> bool:
        """Increment failure count; after threshold, raw-archive messages and return True."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict]) -> None:
        """Fallback: dump raw messages to HISTORY.md without LLM summarization."""
        # consolidation 多次失败后的兜底：不再让模型总结，直接把原始片段落盘到 HISTORY.md
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
    ):
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        # 同一个 session 的 consolidation 必须串行，避免并发归档导致 offset/文件写乱
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive a selected message chunk into persistent memory."""
        return await self.store.consolidate(messages, self.provider, self.model)

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        # 归档边界必须选在 user turn 上：
        # 这样不会把 tool_call/tool_result 拆散（否则历史回放可能对不上 tool_call_id）

        # 从 session.last_consolidated 开始往后扫，每遇到一个 role=="user" 就把它记成一个候选边界 last_boundary=(idx, removed_tokens)；
        # 当累计 removed_tokens >= tokens_to_remove 时就返回这个边界。
        # 如果一直没凑够 tokens，就返回最后一个 user 边界（能归档一点是一点）；如果连 user 边界都没有，就返回 None。

        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        # 从 start 往后遍历
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            # 每遇到一条 user 消息，就记录一个潜在的边界（idx，removed_tokens）
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            # removed_tokens 是从 start 到当前遍历位置累计的 token（用 estimate_message_tokens(message) 加起来）
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        # 构造一次“真实会发给 LLM 的 messages”，把用户输入换成一个固定字符串 [token-probe]，然后用估算器算 token。
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        # 估算的不是纯历史 token，
        # 更贴近真实调用：system + memory + skills + history + runtime + tools 的总 prompt token。
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive messages with guaranteed persistence (retries until raw-dump fallback)."""
        # 保证落盘，尝试多次 consolidation，失败最终也 raw dump
        if not messages:
            return True
        for _ in range(self.store._MAX_FAILURES_BEFORE_RAW_ARCHIVE):
            if await self.consolidate_messages(messages):
                return True
        return True

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.
        """
        # 最核心的“控窗函数”：
        #
        # 目标：保证“下一次调用 LLM 时的 prompt 不会超过上下文窗口”。
        # 做法：如果估算的 prompt tokens 太大，就把更老的一段对话归档到：
        # - workspace/memory/HISTORY.md（可 grep 的摘要/日志）
        # - workspace/memory/MEMORY.md（长期事实）
        # 并把 session.last_consolidated 往前推进，从而让 history 变短。
        #
        # 关键变量：
        # - context_window_tokens：模型上下文窗口上限（配置）
        # - max_completion_tokens：为“模型回复”预留的 token 空间（避免把窗口占满导致无法回复）
        # - _SAFETY_BUFFER：估算误差缓冲（token 估算不是精确值，留点余量更稳）
        #
        # 预算策略：
        # - budget = context_window - max_completion_tokens - safety_buffer
        #   （budget 表示“允许 history + system + tools 占用的 token 上限”）
        # - target = budget // 2
        #   （更保守的目标：希望把 history 压到 budget 的一半，避免下一轮又立刻爆窗）
        #
        # 归档切块策略：
        # - 只能在 user-turn 边界切（pick_consolidation_boundary），避免把 tool_call/tool_result 拆散，
        #   否则历史回放可能出现 tool_call_id 对不上，导致 provider/模型拒绝。
        #
        # 为什么要加 lock：
        # - 同一个 session 可能会在“处理前”与“处理后后台任务”同时触发归档，
        #   lock 保证归档串行，避免并发写 MEMORY/HISTORY 或错乱 last_consolidated。
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            # 1) 计算 token 预算与目标值
            budget = self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER
            target = budget // 2

            # 2) 估算当前 prompt 的 token 数（estimated）以及估算来源（source）
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            if estimated < budget:
                # 小于预算：无需归档
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            # 3) 超预算：开始多轮归档（最多 _MAX_CONSOLIDATION_ROUNDS 轮）
            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    # 已经压到目标范围：结束
                    return

                # boundary 返回 (end_idx, removed_tokens)，end_idx 是建议归档到哪一条消息之前
                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                # chunk：从 last_consolidated 到 end_idx 这一段老消息将被归档进 MEMORY/HISTORY
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )

                # consolidate_messages() 会调用 MemoryStore.consolidate(...)：
                # - 成功返回 True（写入 HISTORY.md/MEMORY.md）
                # - 失败返回 False（未达降级阈值），此时直接返回，避免无限重试卡住主流程
                if not await self.consolidate_messages(chunk):
                    return

                # 归档成功：推进偏移，并落盘 session（以后 get_history() 会跳过已归档部分）
                session.last_consolidated = end_idx
                self.sessions.save(session)

                # 重新估算当前 prompt token（归档后应当变小）
                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return
