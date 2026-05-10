from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .memory import MemoryConsolidator
from .sessions import Session, SessionStore
from .trace import TraceLogger
from .tools.registry import ToolRegistry


@dataclass(frozen=True)
class AgentConfig:
    """DataSSA（vendor-free）运行时配置。"""

    workspace: str
    context_window_tokens: int = 65536
    max_tool_iterations: int = 20
    max_completion_tokens: int = 4096
    temperature: float = 0.2


class AgentRuntime:
    """最小可用的 Agent Runtime（兼容 OpenAI 风格 tools/tool_calls）。

    职责概览：
    - 会话管理：按 `session_key` 读取/落盘历史（`workspace/sessions/`）
    - 上下文构建：system 指令 + 长期记忆（MEMORY.md）+ 近期对话 + 当前用户输入
    - LLM 调用：携带工具定义（OpenAI tools schema）让模型自主选择调用工具
    - 工具执行：解析 tool_calls → 执行本地/远端工具 → 把结果作为 role=tool 回填继续推理
    - 记忆压缩：当上下文接近窗口上限时，归档旧消息到 `workspace/memory/`
    """

    def __init__(
        self,
        *,
        cfg: AgentConfig,
        provider: Any,
        tools: ToolRegistry,
    ) -> None:
        from pathlib import Path

        self.cfg = cfg
        # OpenAI 兼容 Provider：需实现 `chat(...)`，返回结构包含：
        # resp["choices"][0]["message"]，并可能带有 message["tool_calls"]。
        self.provider = provider
        # 工具注册表：决定暴露给模型哪些工具，以及它们的 schema/描述。
        self.tools = tools
        self.workspace = Path(cfg.workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        # 会话存储：按 session_key 将历史落盘，便于多会话与重启恢复。
        self.sessions = SessionStore(self.workspace)
        self.mcp_stack: AsyncExitStack | None = None
        # 记忆压缩器：把旧历史归档到 `workspace/memory/{MEMORY,HISTORY}.md`，
        # 并推进 `session.last_consolidated`，让后续 prompt 不爆上下文窗口。
        self.memory = MemoryConsolidator(
            workspace=self.workspace,
            provider=provider,
            session_store=self.sessions,
            context_window_tokens=cfg.context_window_tokens,
            max_completion_tokens=cfg.max_completion_tokens,
        )

    def _system_prompt(self) -> str:
        # 先保持最小指令集；并尝试加载 `workspace/AGENTS.md` 作为可配置规则（若存在）。
        base = (
            "You are DataSSA, a safe data analysis assistant.\n"
            "- Prefer using database tools to answer data questions.\n"
            "- Never attempt write operations; only read-only queries are allowed.\n"
            "- Explain results clearly and concisely.\n"
        )
        # 约定：workspace/AGENTS.md 用于沉淀“角色/规则/工具约束”，便于不用改代码就能调行为。
        try:
            path = (self.workspace / "AGENTS.md").resolve()
            if path.exists() and path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    return base + "\n## Workspace Rules (AGENTS.md)\n" + text + "\n"
        except Exception:
            pass
        return base

    def _load_memory_context(self) -> str:
        # 长期记忆由 MemoryConsolidator 维护（写入 workspace/memory/MEMORY.md）。
        return self.memory.store.read_long_term()

    def _build_messages(self, session: Session, user_message: str) -> list[dict[str, Any]]:
        # 构造发给 LLM 的 messages：
        # system + 长期记忆 + 近期历史 + 当前用户输入
        messages: list[dict[str, Any]] = []
        messages.append({"role": "system", "content": self._system_prompt()})

        mem = self._load_memory_context().strip()
        if mem:
            messages.append({"role": "system", "content": f"## Long-term Memory\n{mem}"})

        for m in session.get_history(max_messages=60):
            role = m.get("role")
            content = m.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_message})
        return messages

    @staticmethod
    def _extract_assistant_message(resp: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
        # OpenAI 兼容返回结构：
        # resp["choices"][0]["message"] = {"content": "...", "tool_calls": [...]}
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []
        return (content if isinstance(content, str) else None), tool_calls

    async def _call_llm(
        self,
        *,
        messages: list[dict[str, Any]],
        tool_choice: Any | None = "auto",
    ) -> dict[str, Any]:
        # 把工具定义传给模型，让模型决定何时、以何参数调用工具。
        return await self.provider.chat(
            messages=messages,
            tools=self.tools.definitions(),
            tool_choice=tool_choice,
            max_tokens=self.cfg.max_completion_tokens,
            temperature=self.cfg.temperature,
        )

    async def process(
        self,
        *,
        session_key: str,
        user_message: str,
        datasource_id: str | None = None,
    ) -> dict[str, Any]:
        """处理一条用户输入，返回最终回复（以及 trace_id、tools_used）。

        重要约定：
        - session_key 固定格式：{user}:{session}
        - datasource_id 会写入 session（首次绑定后后续默认沿用）
        - trace 会落盘到 workspace/traces/<date>/<trace_id>.jsonl
        """
        session = self.sessions.load(session_key)
        if datasource_id:
            session.datasource_id = datasource_id
        session.add("user", user_message)

        # 必要时进行“记忆压缩”，避免 prompt 超过上下文窗口。
        await self.memory.maybe_consolidate(session=session)

        # 轻量 trace_id：用于观测与排查；同时会透传给 safe_query，用于 audit/trace 关联。
        trace_id = f"trace_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{abs(hash((session_key, user_message)))%100000}"
        dsid = session.datasource_id or "database"
        trace = TraceLogger(workspace=self.workspace, trace_id=trace_id, session_id=session_key, datasource_id=dsid)
        trace.emit("nl_input", {"message": user_message})

        messages = self._build_messages(session, user_message)

        # tools_used：用于前端/调试展示本次对话使用了哪些工具
        tools_used: list[str] = []
        for _ in range(self.cfg.max_tool_iterations):
            resp = await self._call_llm(messages=messages, tool_choice="auto")
            content, tool_calls = self._extract_assistant_message(resp)

            # 工具调用分支：逐个执行工具，并把输出作为 role=tool 回填到 messages。
            if tool_calls:
                # IMPORTANT: Append the assistant tool-calls message first.
                # Some OpenAI-compatible providers require every `role=tool` message
                # to directly respond to a preceding assistant message that contains `tool_calls`.
                messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = (tc.get("function") or {}).get("name")
                    raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                    except Exception:
                        args = {}
                    # 如果模型没有传 datasource_id，则使用 session 已绑定的数据源。
                    if fn == "safe_query_run" and isinstance(args, dict):
                        args.setdefault("datasource_id", session.datasource_id or "database")
                        args.setdefault("trace_id", trace_id)

                    tool = self.tools.get(fn)
                    if tool is None:
                        tool_out = f"(unknown tool: {fn})"
                    else:
                        tools_used.append(fn)
                        trace.emit("tool_call", {"tool": fn, "arguments": args})
                        try:
                            tool_out = await tool.execute(**(args if isinstance(args, dict) else {}))
                        except Exception as exc:
                            tool_out = f"(tool error: {type(exc).__name__}: {exc})"
                        trace.emit(
                            "tool_result",
                            {
                                "tool": fn,
                                "output_preview": (tool_out or "")[:400],
                                "output_len": len(tool_out or ""),
                            },
                        )

                    # workflow skill: safe_query_run 返回结构化 JSON，直接作为最终产出（避免模型二次改写导致证据链被改乱）。
                    if fn == "safe_query_run" and tool_out:
                        try:
                            obj = json.loads(tool_out)
                            answer = (obj.get("answer") or "").strip()
                            sql = (obj.get("sql") or "").strip() or None
                            warnings = obj.get("warnings") or []
                            artifacts = obj.get("artifacts") or {}
                            if isinstance(artifacts, dict):
                                artifacts.setdefault("trace_path", str(trace.path))
                            else:
                                artifacts = {"trace_path": str(trace.path)}
                            if not answer:
                                answer = "(no answer)"
                            # 重要：把 skill 产物里的关键结构化信息再写成 trace 事件（便于复盘/统计）
                            # - sql_validate / sql_execute / result_verify
                            datasource_id = "database"
                            if isinstance(artifacts, dict):
                                ds = artifacts.get("datasource_id")
                                if isinstance(ds, str) and ds.strip():
                                    datasource_id = ds.strip()
                                v = artifacts.get("validate")
                                if isinstance(v, dict):
                                    trace.emit(
                                        "sql_validate",
                                        {
                                            "is_safe": v.get("is_safe"),
                                            "reason": v.get("reason"),
                                            "warnings": v.get("warnings") or [],
                                        },
                                    )
                                e = artifacts.get("sql_execute")
                                if isinstance(e, dict):
                                    trace.emit(
                                        "sql_execute",
                                        {
                                            "datasource_id": datasource_id,
                                            "sql": sql,
                                            "duration_ms": e.get("duration_ms"),
                                            "row_count": e.get("row_count"),
                                            "truncated": e.get("truncated"),
                                            "max_rows": e.get("max_rows"),
                                        },
                                    )
                                rv = artifacts.get("result_verify")
                                if isinstance(rv, dict):
                                    trace.emit(
                                        "result_verify",
                                        {
                                            "warnings": rv.get("warnings") or [],
                                            "stats": rv.get("stats") or {},
                                        },
                                    )
                            trace.emit("final_answer", {"answer_preview": answer[:400]})
                            session.add(
                                "assistant",
                                answer,
                                meta={"tools_used": tools_used, "trace_id": trace_id, "sql": sql},
                            )
                            self.sessions.save(session)
                            return {
                                "trace_id": trace_id,
                                "content": answer,
                                "sql": sql,
                                "warnings": warnings if isinstance(warnings, list) else [str(warnings)],
                                "artifacts": artifacts,
                                "tools_used": tools_used,
                            }
                        except Exception:
                            # Fall back to normal tool-message path.
                            pass

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": fn,
                            "content": tool_out,
                        }
                    )
                continue

            # 最终回复分支：落盘并返回。
            if content is None:
                content = "(no response)"
            trace.emit("final_answer", {"answer_preview": content[:400]})
            session.add("assistant", content, meta={"tools_used": tools_used, "trace_id": trace_id})
            self.sessions.save(session)
            return {
                "trace_id": trace_id,
                "content": content,
                "sql": None,
                "warnings": [],
                "artifacts": {"trace_path": str(trace.path)},
                "tools_used": tools_used,
            }

        # Fallback if we hit max iterations.
        content = "(stopped: max_tool_iterations reached)"
        trace.emit("error", {"reason": "max_tool_iterations_reached"})
        session.add("assistant", content, meta={"tools_used": tools_used, "trace_id": trace_id})
        self.sessions.save(session)
        return {
            "trace_id": trace_id,
            "content": content,
            "sql": None,
            "warnings": ["max_tool_iterations_reached"],
            "artifacts": {"trace_path": str(trace.path)},
            "tools_used": tools_used,
        }
