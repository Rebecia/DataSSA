"""Async message queue for decoupled channel-agent communication.

中文速读：
- 这就是 nanobot 的“消息总线”（MessageBus），用两个 `asyncio.Queue` 解耦：
  - inbound：渠道/CLI 把用户消息放进来（InboundMessage）
  - outbound：AgentLoop 把回复放出来（OutboundMessage）
- 这样 channels/CLI 和 agent 不直接互相调用，只通过队列交换事件。

你在其他地方会看到：
- 生产 inbound：
  - `nanobot/channels/base.py:_handle_message()`（各渠道收到消息后 publish_inbound）
  - `nanobot/cli/commands.py` 交互模式（用户输入后 publish_inbound）
- 消费 inbound：
  - `nanobot/agent/loop.py:AgentLoop.run()`（consume_inbound）
- 生产 outbound：
  - `nanobot/agent/loop.py`（publish_outbound：progress/stream/final）
- 消费 outbound：
  - `nanobot/cli/commands.py:_consume_outbound()`（CLI 渲染）
  - `nanobot/channels/manager.py:_dispatch_outbound()`（渠道分发）
"""

import asyncio

from nanobot.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        # inbound/outbound 都是 asyncio.Queue：
        # - put/get 都是 awaitable（不会阻塞线程，只会挂起当前协程）
        # - 默认不限制大小（无 maxsize），适合“消息事件流”
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel/CLI to the agent (inbound queue).

        中文解释：生产者调用这个方法把一条 InboundMessage 放入 inbound 队列。
        语法点：`await self.inbound.put(msg)` —— 如果队列满（有 maxsize）会等待；默认无限大一般不会等。
        """
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (waits until available).

        中文解释：消费者（AgentLoop.run）用它从 inbound 取消息。
        语法点：`await self.inbound.get()` 会挂起当前协程，直到队列里有新消息。
        """
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels/CLI (outbound queue).

        中文解释：AgentLoop 把回复（或进度/流式片段）放入 outbound 队列，交给 CLI/ChannelManager 消费。
        """
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (waits until available).

        中文解释：CLI 渲染器或 ChannelManager 分发器使用它，从 outbound 取消息并发送/显示。
        """
        return await self.outbound.get()

    # 下面这个装饰器，是把一个办法变成一个属性一样访问的东西
    # 访问时写bus.inbound_size（不加括号），实际执行的是：bus.inbound_size() 这个函数体
    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages.

        中文解释：队列里当前积压的 inbound 消息数量（用于状态/监控）。
        注意：qsize() 在并发场景下是“近似值”，但足够做展示。
        """
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages.

        中文解释：队列里当前积压的 outbound 消息数量（用于状态/监控）。
        """
        return self.outbound.qsize()
