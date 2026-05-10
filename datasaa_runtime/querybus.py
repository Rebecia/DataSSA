from __future__ import annotations

"""datasaa_runtime.querybus

全局 QueryBus（进程内）：
- 统一接收“执行 SQL”的任务（按 datasource_id 路由）
- 统一执行 validate_sql → query_database（只读可信闭环的执行平面）
- 统一做并发控制（全局 worker + per-datasource semaphore）
- 通过 task_id -> Future 把结果回传给等待方（AnsBus 的最小实现）

定位：
QueryBus 负责“怎么查、去哪查、并发怎么控”，不负责“问什么/SQL 怎么写/答案怎么排版”。
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QueryTask:
    """提交给 QueryBus 的任务（执行请求）。

    说明：
    - trace_id 仅用于审计关联（audit.log 里落 trace_id）
    - session_id/user_id 目前不参与执行逻辑，但可用于未来做配额/限流/复盘过滤
    """
    task_id: str
    datasource_id: str
    sql: str
    max_rows: int

    trace_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class QueryResult:
    """QueryBus 回传的执行结果（结构化，供 Skill/Agent 排版与 trace 记录）。"""
    ok: bool
    blocked: bool
    datasource_id: str
    sql: str

    validate: dict[str, Any] | None
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    max_rows: int
    duration_ms: float | None = None

    error: dict[str, Any] | None = None


class QueryBus:
    """全局 QueryBus（Phase 2）：统一执行 validate + execute 并回传结构化结果。

    约束：
    - 该 Bus 是进程内组件：asyncio.Queue + worker tasks + task_id -> Future。
    """

    def __init__(
        self,
        *,
        tools: Any,
        worker_count: int = 4,
        per_datasource_limit: int = 4,
        max_retries: int = 1,
    ) -> None:
        # tools：ToolRegistry（含 MCP tools），用于调用 mcp_{dsid}_validate_sql/query_database
        self._tools = tools
        self._worker_count = max(1, int(worker_count))
        self._queue: asyncio.Queue[QueryTask] = asyncio.Queue()
        # task_id -> Future，用于等待/回传结果（AnsBus 最小形态）
        self._tasks: dict[str, asyncio.Future[QueryResult]] = {}
        # task_id -> status（pending/running/done/failed/cancelled）
        self._status: dict[str, str] = {}
        # 被取消的 task_id（best-effort：worker 拿到任务后会跳过）
        self._cancelled: set[str] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._closing = False

        self._per_ds_limit = max(1, int(per_datasource_limit))
        self._per_ds_sem: dict[str, asyncio.Semaphore] = {}
        self._max_retries = max(0, int(max_retries))

    def _sem(self, datasource_id: str) -> asyncio.Semaphore:
        """每个 datasource 单独一把 semaphore，避免单库被打爆。"""
        dsid = datasource_id.strip() or "database"
        if dsid not in self._per_ds_sem:
            self._per_ds_sem[dsid] = asyncio.Semaphore(self._per_ds_limit)
        return self._per_ds_sem[dsid]

    def start(self) -> None:
        """启动 worker 线程（asyncio task）。通常在 FastAPI lifespan 启动时调用一次。"""
        if self._workers:
            return
        self._closing = False
        for i in range(self._worker_count):
            # workloop 启动后会持续运行，直到 QueryBus 被 close（期间不断从队列取任务执行）
            self._workers.append(asyncio.create_task(self._worker_loop(i)))

    async def close(self) -> None:
        """关闭 QueryBus：取消 worker，并清理未完成任务。"""
        self._closing = True
        # cancel workers
        for w in list(self._workers):
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        # cancel pending tasks
        for fut in list(self._tasks.values()):
            if not fut.done():
                fut.cancel()
        self._tasks.clear()
        self._status.clear()
        self._cancelled.clear()

    def submit(
        self,
        *,
        datasource_id: str,
        sql: str,
        max_rows: int,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """提交一个执行任务并返回 task_id（调用方随后 wait(task_id)）。"""
        if self._closing:
            raise RuntimeError("QueryBus is closing")
        task_id = f"q_{uuid.uuid4().hex}"
        fut: asyncio.Future[QueryResult] = asyncio.get_event_loop().create_future()
        self._tasks[task_id] = fut
        self._status[task_id] = "pending"
        self._queue.put_nowait(
            QueryTask(
                task_id=task_id,
                datasource_id=(datasource_id or "").strip() or "database",
                sql=(sql or "").strip(),
                max_rows=int(max_rows or 200),
                trace_id=trace_id,
                session_id=session_id,
                user_id=user_id,
            )
        )
        return task_id

    async def wait(self, task_id: str, *, timeout_s: float = 30.0) -> QueryResult:
        """等待任务完成并返回结果。超时会抛 asyncio.TimeoutError。"""
        fut = self._tasks.get(task_id)
        if fut is None:
            raise RuntimeError(f"unknown task_id: {task_id}")
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        finally:
            # best-effort cleanup
            self._tasks.pop(task_id, None)
            self._status.pop(task_id, None)
            self._cancelled.discard(task_id)

    def get_status(self, task_id: str) -> str | None:
        """查询任务状态（best-effort）。"""
        return self._status.get(task_id)

    def cancel(self, task_id: str) -> bool:
        """取消任务（best-effort）。

        注意：asyncio.Queue 不支持直接删除排队项，所以这里采用：
        - 标记 cancelled
        - 若 Future 未完成则立即返回 Cancelled 结果，避免调用方一直等待
        """
        fut = self._tasks.get(task_id)
        if fut is None or fut.done():
            return False
        self._cancelled.add(task_id)
        self._status[task_id] = "cancelled"
        fut.set_result(
            QueryResult(
                ok=False,
                blocked=False,
                datasource_id="",
                sql="",
                validate=None,
                columns=[],
                rows=[],
                row_count=0,
                truncated=False,
                max_rows=0,
                duration_ms=None,
                error={"code": "Cancelled", "message": "task cancelled"},
            )
        )
        return True

    async def _worker_loop(self, worker_idx: int) -> None:
        """worker 主循环：从队列取任务，执行并写回 Future。"""
        while True:
            task = await self._queue.get()
            fut = self._tasks.get(task.task_id)
            if fut is None or fut.done():
                continue
            if task.task_id in self._cancelled:
                continue
            try:
                self._status[task.task_id] = "running"
                res = await self._execute(task)
                if not fut.done():
                    fut.set_result(res)
                self._status[task.task_id] = "done" if (res.ok or res.blocked) else "failed"
            except Exception as exc:
                if not fut.done():
                    fut.set_result(
                        QueryResult(
                            ok=False,
                            blocked=False,
                            datasource_id=task.datasource_id,
                            sql=task.sql,
                            validate=None,
                            columns=[],
                            rows=[],
                            row_count=0,
                            truncated=False,
                            max_rows=task.max_rows,
                            duration_ms=None,
                            error={"code": type(exc).__name__, "message": str(exc), "worker": worker_idx},
                        )
                    )
                self._status[task.task_id] = "failed"

    @staticmethod
    def _is_transient_error(message: str) -> bool:
        """判断是否为“可重试”的临时错误（简单启发式）。"""
        msg = (message or "").lower()
        return ("timed out" in msg) or ("timeout" in msg) or ("并发查询数已达上限" in message) or ("too many" in msg)

    async def _execute(self, task: QueryTask) -> QueryResult:
        """真正执行：validate → execute（带有限重试）。"""
        dsid = task.datasource_id
        sem = self._sem(dsid)
        async with sem:
            prefix = f"mcp_{dsid}_"
            tool_validate = self._tools.get(prefix + "validate_sql")
            tool_query = self._tools.get(prefix + "query_database")
            if tool_validate is None:
                raise RuntimeError(f"missing tool: {prefix}validate_sql")
            if tool_query is None:
                raise RuntimeError(f"missing tool: {prefix}query_database")

            # 1) validate
            raw_validate = await tool_validate.execute(sql=task.sql, **({"trace_id": task.trace_id} if task.trace_id else {}))
            validate_obj: dict[str, Any] | None = None
            try:
                parsed = json.loads(raw_validate)
                validate_obj = parsed if isinstance(parsed, dict) else None
            except Exception:
                validate_obj = None

            if validate_obj is not None and not bool(validate_obj.get("is_safe", False)):
                reason = str(validate_obj.get("reason") or "安全检查未通过")
                return QueryResult(
                    ok=False,
                    blocked=True,
                    datasource_id=dsid,
                    sql=task.sql,
                    validate=validate_obj,
                    columns=[],
                    rows=[],
                    row_count=0,
                    truncated=False,
                    max_rows=task.max_rows,
                    duration_ms=None,
                    error={"code": "Blocked", "message": reason},
                )

            # 2) execute (with retry)
            start = time.time()
            last_error_msg = ""
            for attempt in range(self._max_retries + 1):
                raw = await tool_query.execute(
                    sql=task.sql,
                    max_rows=task.max_rows,
                    **({"trace_id": task.trace_id} if task.trace_id else {}),
                )
                dur_ms = (time.time() - start) * 1000.0

                try:
                    obj = json.loads(raw)
                except Exception:
                    return QueryResult(
                        ok=False,
                        blocked=False,
                        datasource_id=dsid,
                        sql=task.sql,
                        validate=validate_obj,
                        columns=[],
                        rows=[],
                        row_count=0,
                        truncated=False,
                        max_rows=task.max_rows,
                        duration_ms=dur_ms,
                        error={"code": "BadToolOutput", "message": "query_database returned non-JSON"},
                    )

                if not isinstance(obj, dict):
                    raise RuntimeError("query_database returned invalid payload")

                if obj.get("success") is False:
                    last_error_msg = str(obj.get("error") or "")
                    if attempt < self._max_retries and self._is_transient_error(last_error_msg):
                        await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
                        continue
                    return QueryResult(
                        ok=False,
                        blocked=False,
                        datasource_id=dsid,
                        sql=task.sql,
                        validate=validate_obj,
                        columns=[],
                        rows=[],
                        row_count=0,
                        truncated=False,
                        max_rows=int(obj.get("max_rows") or task.max_rows),
                        duration_ms=float(obj.get("duration_ms") or dur_ms),
                        error={"code": "QueryFailed", "message": last_error_msg},
                    )

                columns = obj.get("columns") or []
                rows = obj.get("rows") or []
                return QueryResult(
                    ok=True,
                    blocked=False,
                    datasource_id=dsid,
                    sql=task.sql,
                    validate=validate_obj,
                    columns=[str(c) for c in columns] if isinstance(columns, list) else [],
                    rows=rows if isinstance(rows, list) else [],
                    row_count=int(obj.get("row_count") or 0),
                    truncated=bool(obj.get("truncated")),
                    max_rows=int(obj.get("max_rows") or task.max_rows),
                    duration_ms=float(obj.get("duration_ms") or dur_ms),
                )

            raise RuntimeError(last_error_msg or "query failed")
