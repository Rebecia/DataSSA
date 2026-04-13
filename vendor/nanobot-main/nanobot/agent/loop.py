"""Agent loop: the core processing engine.

中文速读（给读源码的人）：
- 这是 nanobot 的“大脑主循环”：从 `MessageBus.inbound` 取 `InboundMessage`，调用 LLM + 工具，
  再把结果作为 `OutboundMessage` 放到 `MessageBus.outbound`。
- `AgentLoop` 不直接依赖具体渠道（Telegram/钉钉/邮件等），渠道只是生产/消费 bus 里的事件，达到解耦。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot import __version__
from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryConsolidator
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.utils.helpers import build_status_content
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, WebSearchConfig
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back

    中文理解：
    - `run()`：常驻任务，从 bus 读入站消息并分发执行（支持 /stop 等指令）
    - `_process_message()`：处理“单条入站消息”，负责会话、上下文、slash 命令、调用 `_run_agent_loop()`
    - `_run_agent_loop()`：核心迭代：LLM → tool_calls → tool_results → 再问 LLM，直到给出最终回复
    - `_save_turn()`：把本轮消息写回 Session（持久化），并裁剪/清洗可能很大的 tool 输出与 runtime metadata
    """

    _TOOL_RESULT_MAX_CHARS = 16_000

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: WebSearchConfig | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig, WebSearchConfig

        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_search_config=self.web_search_config,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._processing_lock = asyncio.Lock()
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
        )
        # 注册工具函数
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools.

        中文提示：工具是 agent“能动手做事”的能力（读写文件、跑命令、搜网页、发消息、启动子代理等）。
        `restrict_to_workspace=True` 时会限制文件/命令的作用范围（安全边界）。
        """
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read))
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        
        # shell 工具注册需要许可
        if self.exec_config.enable:
            self.tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
            ))
        self.tools.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        # 连接成功后，会把远端工具包装成 Tool 注册到 self.tools（工具名形如 mcp_{server}_{tool}）。
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            # 已连接 / 正在连接 / 没有配置 server：直接返回（幂等）
            return
        self._mcp_connecting = True  # 防止并发重复连接（多个消息同时触发时）
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            # AsyncExitStack：集中管理多个 async context（多个 server 的连接、http client 等），便于统一关闭
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            # 连接所有配置的 MCP server，并把它们的 tools 注册进 self.tools
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        from nanobot.utils.helpers import strip_think
        return strip_think(text) or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    def _status_response(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Build an outbound status message for a session."""
        ctx_est = 0
        try:
            ctx_est, _ = self.memory_consolidator.estimate_session_prompt_tokens(session)
        except Exception:
            pass
        if ctx_est <= 0:
            ctx_est = self._last_usage.get("prompt_tokens", 0)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=build_status_content(
                version=__version__, model=self.model,
                start_time=self._start_time, last_usage=self._last_usage,
                context_window_tokens=self.context_window_tokens,
                session_msg_count=len(session.get_history(max_messages=0)),
                context_tokens_estimate=ctx_est,
            ),
            metadata={"render_as": "text"},
        )

    # 分装单次对话
    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        中文理解：这是“一轮对话”内部的迭代器。
        - 每次向 LLM 发起请求，可能得到：①最终文本 ②tool_calls（需要执行工具）
        - 有 tool_calls 就执行工具，把 tool 结果作为 role=tool 的消息追加回 messages，再继续下一轮
        - 直到 LLM 给出最终文本，或达到 max_iterations 退出
        """
        messages = initial_messages  # 当前“对话轨迹”（system/history/user/tool 等消息列表），多模态信息已经处理好
        iteration = 0  # 当前迭代轮数（一次 tool_call 循环算一轮）
        final_content = None  # 最终要返回给用户的文本（没有则表示还在循环/或异常）
        tools_used: list[str] = []  # 本轮实际执行过的工具名（用于统计/调试）

        # 中文提示：有些模型会把“思考过程”用 <think>…< /think> 输出。
        # 这里在流式输出时做增量过滤，避免把 think 内容发到终端/聊天渠道。
        _raw_stream = on_stream  # 保存原始流式回调（下面包装一层过滤器）
        _stream_buf = ""  # 累积已收到的流式文本，用于做“增量去 think”计算

        async def _filtered_stream(delta: str) -> None:
            nonlocal _stream_buf  # Python 语法：声明使用外层函数的局部变量（允许赋值修改）
            # 这里把 strip_think 放在函数内部 import：减少模块加载时的耦合/成本（也避免循环依赖）
            from nanobot.utils.helpers import strip_think

            prev_clean = strip_think(_stream_buf)  # 之前“已经输出给用户”的干净文本
            _stream_buf += delta  # 把这次新到的流式片段追加到缓存
            new_clean = strip_think(_stream_buf)  # 新的“干净文本”（去掉 <think>…</think>）

            # 只输出“增量部分”，避免重复打印已经输出过的内容
            incremental = new_clean[len(prev_clean):]  
            if incremental and _raw_stream:
                await _raw_stream(incremental)  # await：等待异步回调完成（例如推送到 CLI/渠道）

        while iteration < self.max_iterations:
            iteration += 1  # 每进入一次循环，代表一次“问模型→（可选）跑工具”的迭代

            tool_defs = self.tools.get_definitions()  # OpenAI tool schema 列表（给 LLM 看的“工具说明书”）

            # 中文提示：同一套 messages + tool schema 交给 provider。
            # 如果 on_stream 存在则走流式接口，否则走普通接口。
            if on_stream:
                # 流式模式：provider 会边生成边回调 on_content_delta（这里传入过滤后的 _filtered_stream）
                response = await self.provider.chat_stream_with_retry(
                    messages=messages,
                    tools=tool_defs,
                    model=self.model,
                    on_content_delta=_filtered_stream,
                )
            else:
                # 非流式模式：一次性拿到完整 response
                response = await self.provider.chat_with_retry(
                    messages=messages,
                    tools=tool_defs,
                    model=self.model,
                )

            # 记录最近一次请求的 token 用量（用于 /status 与日志展示）
            usage = response.usage or {}
            self._last_usage = {
                # int(...) + or 0：把 None/空值安全地转成 int，避免后续显示报错
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            }

            if response.has_tool_calls:
                # LLM 要求调用工具：先把 assistant(tool_calls) 追加到 messages，再逐个执行工具并追加 tool 结果。
                if on_stream and on_stream_end:
                    # 流式输出结束（但还要继续执行工具并再次请求模型）
                    await on_stream_end(resuming=True)
                    _stream_buf = ""  # 清空缓存，下一段流式输出重新计算增量

                if on_progress:
                    if not on_stream:
                        # 非流式时：可把“模型这轮说的话（可能含 think）”当作 progress 提示打印出来
                        thought = self._strip_think(response.content)
                        if thought:
                            await on_progress(thought)
                    # tool_hint：把工具调用格式化成简短提示（例如 web_search("xxx")）
                    tool_hint = self._tool_hint(response.tool_calls)
                    tool_hint = self._strip_think(tool_hint)
                    # 第二个参数 tool_hint=True：让 UI/渠道知道“这是工具提示，不是正式回复”
                    await on_progress(tool_hint, tool_hint=True)

                tool_call_dicts = [
                    # 列表推导式：把内部 ToolCallRequest 转成 OpenAI 格式的 tool_call payload
                    tc.to_openai_tool_call()
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    # 把 assistant 的这条消息追加到 messages：
                    # - content 可能为空（有些 provider 在纯 tool_calls 时不会给 content）
                    # - tool_calls 写进去，保证后续 role=tool 能“对上号”
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    # 逐个执行 LLM 请求的工具调用
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)  # 真实执行工具（异步）
                    messages = self.context.add_tool_result(
                        # 把工具结果作为 role=tool 追加回 messages，供下一轮 LLM 阅读
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # LLM 给出最终答复（没有 tool_calls）
                if on_stream and on_stream_end:
                    # 流式输出结束（resuming=False 表示这是最终输出，不会再继续跑工具）
                    await on_stream_end(resuming=False)
                    _stream_buf = ""  # 清空缓存，为下一次用户消息做准备

                clean = self._strip_think(response.content)  # 最终展示给用户的文本（去 think）
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break  # 退出 while：本轮结束
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break  # 正常结束本轮：拿到最终文本

        if final_content is None and iteration >= self.max_iterations:
            # 保护：避免模型一直 tool_call 循环导致“永远不结束”
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages  # 返回最终文本 + 工具列表 + 完整消息轨迹（用于保存 session）

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop.

        中文理解：这是 daemon 主循环。
        - 通过 bus 消费入站消息
        - 对普通消息创建异步 task 执行（便于 /stop 取消）
        - 对 /stop、/restart、/status 等控制命令直接处理
        """
        self._running = True  # 用于控制 while 循环退出（stop() 会把它置为 False）
        await self._connect_mcp()  # 连接 MCP 服务器（如果配置了）；失败会记录并在下一次消息时重试
        logger.info("Agent loop started")  # 仅日志：表示主循环已启动

        while self._running:
            try:
                # asyncio.wait_for：给 consume_inbound 加一个超时，这样循环可以定期醒来检查 self._running
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                # 超时说明 1 秒内没有新消息，继续循环（相当于“轮询 + 可中断”）
                continue
            except asyncio.CancelledError:
                # 中文解释：CancelledError 既可能来自“真正的任务取消”（比如系统关闭），也可能是第三方集成误抛。
                # 这里通过条件判断尽量“保留真实取消”，避免无意吞掉取消信号导致无法退出。
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                # 任何其他异常：记录并继续（主循环不应因为单次异常就退出）
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            cmd = msg.content.strip().lower()  # strip 去两端空白；lower 便于大小写不敏感匹配
            if cmd == "/stop":
                # 会话级“停止”：取消该 session_key 下的活跃任务（以及子代理）
                await self._handle_stop(msg)
            elif cmd == "/restart":
                # 进程级“重启”：通过 os.execv 覆盖当前进程（会先发一条 "Restarting..."）
                await self._handle_restart(msg)
            elif cmd == "/status":
                # 轻量状态：不走 LLM，直接构造一条状态 OutboundMessage
                session = self.sessions.get_or_create(msg.session_key)
                await self.bus.publish_outbound(self._status_response(msg, session))
            else:
                # 普通消息：创建 task，记录到 session_key 对应的活跃任务列表，便于 /stop 定向取消。
                task = asyncio.create_task(self._dispatch(msg))
                # setdefault：如果 key 不存在就初始化一个空列表，然后把 task 追加进去
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                # add_done_callback：task 完成时回调，用于从活跃列表里移除（防止列表越积越多）
                # lambda 捕获 k=msg.session_key：把当前 key 固定下来，避免闭包变量变化带来的误删
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])  # 取出并移除该 session 的活跃任务列表（之后不再跟踪）

        # 逐个尝试取消任务：
        # - t.done()：任务已结束则无需取消
        # - t.cancel()：向任务注入 CancelledError；返回 True 表示“已成功发起取消请求”
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())

        # 等待这些任务真正结束（让 finally/清理逻辑有机会执行）
        for t in tasks:
            try:
                await t  # 被取消的任务通常会抛 CancelledError
            except (asyncio.CancelledError, Exception):
                # /stop 的语义是“尽力停止”，不把任务内部异常再向上传播
                pass

        # 子代理（subagents）也可能在跑：按同一个 session_key 取消
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)

        total = cancelled + sub_cancelled  # 本次 stop 影响的任务数（主任务 + 子代理任务）
        content = f"Stopped {total} task(s)." if total else "No active task to stop."  # 用户可见提示文案
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        # 先给用户回一条提示（否则重启时用户会以为“没反应”）
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        ))

        async def _do_restart():
            await asyncio.sleep(1)  # 给 outbound 一点时间发出去，再重启进程
            # Use -m nanobot instead of sys.argv[0] for Windows compatibility
            # (sys.argv[0] may be just "nanobot" without full path on Windows)
            # os.execv：用新进程映像替换当前进程（不会返回）
            os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

        # create_task：把重启动作放到后台，不阻塞当前 handler（run() 主循环还能继续处理其他清理/消息）
        asyncio.create_task(_do_restart())

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        # 中文提示：这里用全局锁串行化处理，避免同一进程里并发跑多个 LLM/工具导致资源争用、
        # 以及 session/memory 写入时的竞态；同时仍保留 task 级别的可取消性（/stop）。
        async with self._processing_lock:
            try:
                on_stream = on_stream_end = None  # 默认不启用“流式输出“
                if msg.metadata.get("_wants_stream"):  
                    # 将“流式 delta / 流式结束”包装成 OutboundMessage 事件，交给外层消费者（CLI/ChannelManager）。
                    async def on_stream(delta: str) -> None:
                        # 把流式文本片段封装成 OutboundMessage，交给 ChannelManager/CLI 渲染
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content=delta, metadata={"_stream_delta": True},
                        ))

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        # 流式边界事件：
                        # - resuming=True：本段流式结束，但马上要跑工具并继续下一段流式
                        # - resuming=False：最终输出结束（本轮对话结束）
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata={"_stream_end": True, "_resuming": resuming},
                        ))

                # 进入“单条消息处理”主逻辑：可能返回 OutboundMessage，也可能返回 None（例如 message tool 已发送）
                response = await self._process_message(
                    msg, on_stream=on_stream, on_stream_end=on_stream_end,
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    # 特判：CLI 交互模式需要一个“回合结束信号”。
                    # 当最终内容通过流式发送时，_process_message 可能返回 None；
                    # 这里发送一个空内容的 OutboundMessage 让 CLI 的 turn_done 能结束等待。
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                # 该 task 被取消（例如 /stop）：记录日志后继续抛出，让取消生效
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                # 兜底：不要让任何异常把 daemon 主循环打崩
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    # 处理单条信息
    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response.

        中文理解：这是处理“单条入站消息”的统一入口（来自 CLI、聊天渠道、cron/heartbeat 等）。
        主要步骤：
        - 选定 session（会话）并做必要的 memory consolidation（防止上下文爆窗）
        - 处理 slash 命令（/new /status /help）
        - 通过 ContextBuilder 组装 messages（system prompt + history + 当前输入）
        - 调 `_run_agent_loop()` 得到最终回复 + 完整消息轨迹
        - `_save_turn()` 写回 session 并落盘
        """
        if msg.channel == "system":
            # 中文提示：system 通道用于内部触发（例如子代理回调、后台任务），chat_id 里携带原始来源。
            channel, chat_id = (  # 如果 chat_id 自带前缀则拆开，否则默认认为来源是 cli
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)  # 仅日志：系统消息来源
            key = f"{channel}:{chat_id}"  # system 消息要落到“真实来源会话”里
            session = self.sessions.get_or_create(key)  # 取/建 Session（历史 JSONL）
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)  # 先归档旧消息，保证 prompt 不超窗
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))  # 给工具设置路由上下文
            history = session.get_history(max_messages=0)  # 取可用历史（会对齐 tool_call 边界）
            current_role = "assistant" if msg.sender_id == "subagent" else "user"  # 子代理回传时用 assistant 角色
            messages = self.context.build_messages(  # 构造 LLM 输入：system prompt + history + 当前 system 消息
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                current_role=current_role,
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages)  # 调模型 + 工具循环
            self._save_turn(session, all_msgs, 1 + len(history))  # 保存本轮新增消息（跳过 system + history）
            self.sessions.save(session)  # 持久化 session
            self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))  # 后台再检查是否需归档
            return OutboundMessage(  # system 触发也返回一条 outbound，交由 bus 发送
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content  # 截断日志内容避免刷屏
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)  # 普通消息入口日志

        key = session_key or msg.session_key  # 调用方可显式覆写 session_key（例如 cron/heartbeat）
        session = self.sessions.get_or_create(key)  # 取/建会话

        # Slash commands
        # 中文提示：这些是“本地指令”，不进入 LLM。
        cmd = msg.content.strip().lower()  # 规范化命令：去空白 + 小写
        if cmd == "/new":
            snapshot = session.messages[session.last_consolidated:]  # 取未归档的片段，用于写入 MEMORY/HISTORY
            session.clear()  # 清空会话，开启新对话
            self.sessions.save(session)  # 立即落盘
            self.sessions.invalidate(session.key)  # 清缓存，避免后续复用旧对象

            if snapshot:
                self._schedule_background(self.memory_consolidator.archive_messages(snapshot))  # 后台归档旧片段

            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/status":
            return self._status_response(msg, session)  # 不走 LLM：直接返回状态
        if cmd == "/help":
            lines = [
                "🐈 nanobot commands:",
                "/new — Start a new conversation",
                "/stop — Stop the current task",
                "/restart — Restart the bot",
                "/status — Show bot status",
                "/help — Show available commands",
            ]
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="\n".join(lines),
                metadata={"render_as": "text"},
            )
        await self.memory_consolidator.maybe_consolidate_by_tokens(session)  # 正常对话前先做一次“预算检查/归档”

        # 给需要路由/线程信息的工具设置上下文（message/spawn/cron 等）
        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()  # 标记“新一轮开始”，用于统计本轮是否主动 message.send

        history = session.get_history(max_messages=0)  # 取历史（内部会保证 tool_call 边界合法）
        # 组装发给 LLM 的 messages：system prompt + 历史 + 当前输入（含 runtime metadata）
        # 这里处理了多模态信息，将图片转为image_url block 发给“支持多模态的模型”，让模型自己理解图片。
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            # 把“进度信息”也当做 OutboundMessage 发送（由 UI/渠道决定是否展示）
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        # 进入核心：LLM ↔ tools 迭代，直到产生最终文本
        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."  # 兜底：避免返回 None

        self._save_turn(session, all_msgs, 1 + len(history))  # 保存本轮新增消息到 session
        self.sessions.save(session)  # 会话落盘（JSONL）
        self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))  # 后台归档旧消息

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            # 中文提示：如果模型使用了 message tool 把结果“主动发到渠道”，这里就不再返回普通 OutboundMessage。
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content  # 截断日志
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)  # 回复日志

        meta = dict(msg.metadata or {})  # 保留并透传原 metadata（例如渠道侧 message_id/thread）
        if on_stream is not None:
            meta["_streamed"] = True  # 标记本轮采用流式输出（上层可用来判断 turn 已结束）
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=meta,
        )

    @staticmethod
    def _image_placeholder(block: dict[str, Any]) -> dict[str, str]:
        """Convert an inline image block into a compact text placeholder."""
        path = (block.get("_meta") or {}).get("path", "")
        return {"type": "text", "text": f"[image: {path}]" if path else "[image]"}

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if (
                block.get("type") == "image_url"
                and block.get("image_url", {}).get("url", "").startswith("data:image/")
            ):
                filtered.append(self._image_placeholder(block))
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if truncate_text and len(text) > self._TOOL_RESULT_MAX_CHARS:
                    text = text[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results.

        中文理解：把本轮新增的 messages 追加到 session（JSONL 持久化）。
        这里做了两类“清洗/裁剪”以保证可用性与成本：
        - tool 输出可能极大：截断到 `_TOOL_RESULT_MAX_CHARS`，避免历史膨胀
        - runtime metadata（时间/渠道/chat_id）只用于本轮 prompt，不应永久写入历史：写入时剥离
        """
        from datetime import datetime  # 局部 import：把依赖范围缩小在函数内

        # skip 的含义：messages 的前面通常是 system message + 本轮调用前的历史 history。
        # 这里仅把“本轮新产生的消息”追加到 session，避免把历史重复写入。
        for m in messages[skip:]:
            entry = dict(m)  # 拷贝：避免原 messages 被修改（方便调试/复用）
            role, content = entry.get("role"), entry.get("content")  # role 常见为 user/assistant/tool

            if role == "assistant" and not content and not entry.get("tool_calls"):
                # 空 assistant 消息既无文本也无工具调用：写入会污染后续上下文，因此跳过
                continue  
            if role == "tool":
                # tool 结果可能是大文本或多模态块，写入历史前做截断/替换图片占位
                if isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                    # 超长文本截断：避免 session 文件膨胀影响性能/成本
                    entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                elif isinstance(content, list):
                    # 多模态 block 清洗：去掉 data:image base64、可选截断 text block
                    filtered = self._sanitize_persisted_blocks(content, truncate_text=True)
                    if not filtered:
                        # 清洗后没有可保存内容（例如全是 runtime/空块），直接跳过该条消息
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    # ContextBuilder 会把 runtime metadata 与用户文本合并到同一条 user 消息里；
                    # 持久化时剥离 runtime 部分，避免历史里堆积“当前时间/渠道/Chat ID”。
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        # 只有 runtime metadata 没有用户输入 → 不保存
                        continue
                if isinstance(content, list):
                    # 多模态 user content：丢弃 runtime block，图片替换为占位符
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())  # 如果没时间戳就补一个
            session.messages.append(entry)  # 追加到会话内存列表（save() 时写入 JSONL）

        session.updated_at = datetime.now()  # 更新会话最后修改时间

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(
            msg, session_key=session_key, on_progress=on_progress,
            on_stream=on_stream, on_stream_end=on_stream_end,
        )
