from __future__ import annotations

import asyncio
import json
import weakref
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .token_estimator import estimate_message_tokens


class ChatProvider(Protocol):
    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
    ) -> dict[str, Any]:
        """Return OpenAI-compatible response dict (content/tool_calls)."""


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
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_tool_args(args: Any) -> dict[str, Any] | None:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return None
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


@dataclass
class MemoryPaths:
    memory_file: Path
    history_file: Path


class MemoryStore:
    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(self, workspace: Path) -> None:
        mem_dir = workspace / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        self.paths = MemoryPaths(memory_file=mem_dir / "MEMORY.md", history_file=mem_dir / "HISTORY.md")
        self._consecutive_failures = 0

    def read_long_term(self) -> str:
        if self.paths.memory_file.exists():
            return self.paths.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.paths.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.paths.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for msg in messages:
            content = msg.get("content")
            if not content:
                continue
            ts = (msg.get("timestamp") or "?")[:16]
            role = (msg.get("role") or "?").upper()
            lines.append(f"[{ts}] {role}: {content}")
        return "\n".join(lines)

    async def consolidate(self, *, messages: list[dict[str, Any]], provider: ChatProvider) -> bool:
        if not messages:
            return True

        current_memory = self.read_long_term()
        prompt = (
            "Process this conversation and call the save_memory tool with your consolidation.\n\n"
            "## Current Long-term Memory\n"
            f"{current_memory or '(empty)'}\n\n"
            "## Conversation to Process\n"
            f"{self._format_messages(messages)}"
        )
        chat_messages = [
            {
                "role": "system",
                "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation.",
            },
            {"role": "user", "content": prompt},
        ]

        forced = {"type": "function", "function": {"name": "save_memory"}}
        try:
            resp = await provider.chat(messages=chat_messages, tools=_SAVE_MEMORY_TOOL, tool_choice=forced)
            tool_calls = ((resp.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
            if not tool_calls:
                return self._fail_or_raw_archive(messages)

            args = _normalize_tool_args((tool_calls[0].get("function") or {}).get("arguments"))
            if not args or "history_entry" not in args or "memory_update" not in args:
                return self._fail_or_raw_archive(messages)

            entry = _ensure_text(args["history_entry"]).strip()
            update = _ensure_text(args["memory_update"])
            if not entry:
                return self._fail_or_raw_archive(messages)

            self.append_history(entry)
            if update != current_memory:
                self.write_long_term(update)

            self._consecutive_failures = 0
            return True
        except Exception:
            return self._fail_or_raw_archive(messages)

    def _fail_or_raw_archive(self, messages: list[dict[str, Any]]) -> bool:
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict[str, Any]]) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(f"[{ts}] [RAW] {len(messages)} messages\n{self._format_messages(messages)}")


class MemoryConsolidator:
    _SAFETY_BUFFER = 1024
    _MAX_CONSOLIDATION_ROUNDS = 5

    def __init__(
        self,
        *,
        workspace: Path,
        provider: ChatProvider,
        session_store: Any,
        context_window_tokens: int,
        max_completion_tokens: int = 4096,
    ) -> None:
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.session_store = session_store
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_boundary(self, *, messages: list[dict[str, Any]], start: int, tokens_to_remove: int) -> int | None:
        removed = 0
        last_boundary: int | None = None
        for idx in range(start, len(messages)):
            msg = messages[idx]
            if idx > start and msg.get("role") == "user":
                last_boundary = idx
                if removed >= tokens_to_remove:
                    return last_boundary
            removed += estimate_message_tokens(msg)
        return last_boundary

    def estimate_prompt_tokens(self, *, messages: list[dict[str, Any]], memory_text: str) -> int:
        # Heuristic: approximate tokens of memory + history
        total = 0
        total += len(memory_text) // 2
        for m in messages:
            total += estimate_message_tokens(m)
        return max(total, 1)

    async def maybe_consolidate(self, *, session: Any) -> None:
        if self.context_window_tokens <= 0:
            return
        lock = self.get_lock(session.key)
        async with lock:
            budget = self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER
            target = budget // 2
            if budget <= 0:
                return

            memory_text = self.store.read_long_term()
            estimated = self.estimate_prompt_tokens(messages=session.messages[session.last_consolidated :], memory_text=memory_text)
            if estimated < budget:
                return

            for _ in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return
                start = session.last_consolidated
                boundary = self.pick_boundary(messages=session.messages, start=start, tokens_to_remove=max(1, estimated - target))
                if boundary is None:
                    return
                chunk = session.messages[start:boundary]
                if not chunk:
                    return
                ok = await self.store.consolidate(messages=chunk, provider=self.provider)
                if not ok:
                    return
                session.last_consolidated = boundary
                self.session_store.save(session)
                memory_text = self.store.read_long_term()
                estimated = self.estimate_prompt_tokens(messages=session.messages[session.last_consolidated :], memory_text=memory_text)

