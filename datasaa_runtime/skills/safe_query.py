from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..harness import QueryVerificationHarness
from ..querybus import QueryBus, QueryResult


@dataclass(frozen=True)
class SafeQueryResult:
    answer: str
    sql: str
    warnings: list[str]
    artifacts: dict[str, Any]


def _md_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not columns:
        return "_(no columns)_"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body_lines: list[str] = []
    for r in rows:
        body_lines.append("| " + " | ".join(str(r.get(c, "")) for c in columns) + " |")
    return "\n".join([header, sep, *body_lines])


class SafeQueryTool:
    """把“可信查询闭环”封装为单入口工具：safe_query_run(...)。

    设计要点（当前实现）：
    - schema snapshot：通过 MCP list/describe 工具拿到表与字段信息
    - sql 生成：test-mode 用规则；真实模式用 LLM 输出 {sql, explanation}
    - 执行：交给 QueryBus（validate_sql → query_database），避免 Skill 关心并发/路由/重试
    - 结果校验：QueryVerificationHarness（生成结构化 warnings/stats）
    - 输出：answer（结果置顶）+ sql + warnings + artifacts（证据链）
    """

    def __init__(self, *, provider: Any, tools: Any, model: str | None = None, querybus: QueryBus | None = None) -> None:
        self._provider = provider
        self._tools = tools
        self._model = model
        self._querybus = querybus
        self._harness = QueryVerificationHarness()

    @property
    def name(self) -> str:
        # NOTE: Some OpenAI-compatible providers enforce a strict pattern for tool
        # function names (e.g. ^[a-zA-Z0-9_-]+$). Use underscore instead of dot.
        return "safe_query_run"

    @property
    def description(self) -> str:
        return "安全查询闭环：schema→SQL→执行→报告，默认返回 SQL + 结果 + trace_id。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户自然语言问题"},
                "datasource_id": {
                    "type": "string",
                    "description": "数据源标识（MVP 仅支持 'database'）",
                    "default": "database",
                },
                "trace_id": {"type": "string", "description": "可选：trace_id（用于 audit/trace 关联）"},
                "max_rows": {"type": "integer", "default": 200, "description": "最大返回行数（用于展示）"},
            },
            "required": ["question"],
        }

    async def _call_tool(self, name: str, args: dict[str, Any] | None = None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise RuntimeError(f"missing tool: {name}")
        return await tool.execute(**(args or {}))

    async def execute(self, **kwargs: Any) -> str:
        question = (kwargs.get("question") or "").strip()
        datasource_id = (kwargs.get("datasource_id") or "database").strip()
        trace_id = (kwargs.get("trace_id") or "").strip() or None
        max_rows = int(kwargs.get("max_rows") or 200)
        warnings: list[str] = []

        if not question:
            return json.dumps(
                SafeQueryResult(
                    answer="## 结果\n_(empty question)_\n\n## 解读\n问题为空。\n\n## SQL\n```sql\n\n```",
                    sql="",
                    warnings=["empty_question"],
                    artifacts={"datasource_id": datasource_id},
                ).__dict__,
                ensure_ascii=False,
            )

        # P1: datasource_id 用于路由到 mcp_{datasource_id}_* 工具
        if not datasource_id:
            datasource_id = "database"

        # 1) schema snapshot
        prefix = f"mcp_{datasource_id}_"
        raw_tables = await self._call_tool(prefix + "list_tables", {"trace_id": trace_id} if trace_id else None)
        try:
            tables_obj = json.loads(raw_tables)
        except Exception:
            tables_obj = {"raw": raw_tables}

        # 尽量把表名/行数提供给 LLM
        table_lines: list[str] = []
        for t in (tables_obj.get("tables") or []) if isinstance(tables_obj, dict) else []:
            name = t.get("name")
            typ = t.get("type")
            rc = t.get("row_count")
            if name:
                table_lines.append(f"- {name} ({typ}, rows={rc})")
        schema_hint = "\n".join(table_lines) if table_lines else str(raw_tables)[:2000]

        # 2) describe tables (MVP: describe all discovered tables, small DB)
        describes: list[dict[str, Any]] = []
        for line in table_lines:
            # parse "- users (..."
            parts = line.split()
            if len(parts) >= 2:
                table = parts[1]
                try:
                    raw_desc = await self._call_tool(
                        prefix + "describe_table",
                        {"table_name": table, **({"trace_id": trace_id} if trace_id else {})},
                    )
                    describes.append({"table": table, "desc": raw_desc})
                except Exception:
                    continue

        describe_hint = "\n\n".join(
            [f"### {d['table']}\n{d['desc']}" for d in describes]
        )[:12000]

        sql = ""
        explanation = ""

        # 3) generate SQL (LLM or test-mode heuristic)
        if getattr(self._provider, "is_dummy", False):
            q = question.lower()
            # 测试模式也要覆盖“写操作被拦截”的路径，便于 E2E 验证 validate_sql / guard 生效。
            write_signals = ["删除", "drop", "update", "insert", "delete", "truncate", "alter", "create"]
            if any(s in q for s in write_signals) or any(s in question for s in ["删除", "插入", "更新", "写入", "修改"]):
                sql = "DROP TABLE users"
                explanation = "测试模式：模拟写操作请求（应被安全校验拦截）。"
            else:
                if "users" in q and ("多少" in question or "count" in q):
                    sql = "SELECT COUNT(*) AS total_users FROM users LIMIT 1"
                    explanation = "统计 users 表的总行数。"
                elif "orders" in q and ("多少" in question or "count" in q):
                    sql = "SELECT COUNT(*) AS total_orders FROM orders LIMIT 1"
                    explanation = "统计 orders 表的总行数。"
                else:
                    sql = "SELECT * FROM users LIMIT 10"
                    explanation = "测试模式：返回 users 表前 10 行。"
        else:
            prompt = (
                "你是一个专业的数据分析师。根据用户问题与数据库 schema 生成只读 SQL。\n"
                "约束：只允许 SELECT 或 WITH 开头；不要写任何修改语句；尽量加 LIMIT；用清晰别名。\n"
                "输出要求：请输出 JSON：{sql: string, explanation: string}，其中 explanation 用 1-3 条要点解释口径/过滤条件/结果含义。\n\n"
                f"## 用户问题\n{question}\n\n"
                f"## 表清单\n{schema_hint}\n\n"
                f"## 表结构\n{describe_hint or '(no describe)'}\n\n"
            )
            resp = await self._provider.chat(
                messages=[{"role": "system", "content": "Return only valid JSON."}, {"role": "user", "content": prompt}],
                tools=None,
                tool_choice=None,
                max_tokens=1200,
                temperature=0.2,
            )
            choice = (resp.get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "").strip()
            try:
                obj = json.loads(content)
                sql = (obj.get("sql") or "").strip()
                explanation = (obj.get("explanation") or "").strip()
            except Exception:
                # Fallback: treat as plain text
                sql = content

        if not sql:
            return json.dumps(
                SafeQueryResult(
                    answer="未能生成 SQL，请换一种问法或补充约束（如时间范围、表名）。",
                    sql="",
                    warnings=["sql_generation_failed"],
                    artifacts={"datasource_id": datasource_id, "tables": tables_obj},
                ).__dict__,
                ensure_ascii=False,
            )

        # 3.5) validate before execute
        # Phase2: when QueryBus is enabled, validate_sql is executed inside the bus (统一链路)。
        validate_obj: dict[str, Any] | None = None
        if self._querybus is None:
            try:
                raw_validate = await self._call_tool(prefix + "validate_sql", {"sql": sql})
                parsed = json.loads(raw_validate)
                validate_obj = parsed if isinstance(parsed, dict) else None
                if validate_obj is not None:
                    if not validate_obj.get("is_safe", False):
                        reason = validate_obj.get("reason") or "安全检查未通过"
                        return json.dumps(
                            SafeQueryResult(
                                answer=f"## 已拦截\n安全检查未通过：{reason}",
                                sql=sql,
                                warnings=["blocked", str(reason)],
                                artifacts={"datasource_id": datasource_id, "tables": tables_obj, "validate": validate_obj},
                            ).__dict__,
                            ensure_ascii=False,
                        )
                    for w in validate_obj.get("warnings") or []:
                        warnings.append(str(w))
            except Exception:
                # validate tool is best-effort; query tool will still enforce server-side safety.
                pass

        # 4) execute
        query_result: QueryResult | None = None
        if self._querybus is not None:
            # 提交任务：创建 Future，放入队列
            task_id = self._querybus.submit(
                datasource_id=datasource_id,
                sql=sql,
                max_rows=max_rows,
                trace_id=trace_id,
            )
            # 等待 Future 被 worker 填好结果（或超时）
            query_result = await self._querybus.wait(task_id, timeout_s=30.0)
            validate_obj = query_result.validate
            if validate_obj is not None:
                for w in validate_obj.get("warnings") or []:
                    warnings.append(str(w))
            if query_result.blocked:
                reason = ""
                if validate_obj is not None:
                    reason = str(validate_obj.get("reason") or "")
                if not reason and query_result.error:
                    reason = str(query_result.error.get("message") or "")
                reason = reason or "安全检查未通过"
                return json.dumps(
                    SafeQueryResult(
                        answer=f"## 已拦截\n安全检查未通过：{reason}",
                        sql=sql,
                        warnings=["blocked", str(reason)],
                        artifacts={"datasource_id": datasource_id, "tables": tables_obj, "validate": validate_obj},
                    ).__dict__,
                    ensure_ascii=False,
                )
        else:
            # Fallback（无 QueryBus）：直接调用 MCP 工具执行
            raw_result = await self._call_tool(prefix + "query_database", {"sql": sql, "max_rows": max_rows})
            try:
                obj = json.loads(raw_result)
            except Exception:
                obj = {"raw": raw_result}
            if isinstance(obj, dict):
                query_result = QueryResult(
                    ok=bool(obj.get("success", True)),
                    blocked=False,
                    datasource_id=datasource_id,
                    sql=sql,
                    validate=validate_obj,
                    columns=obj.get("columns") or [],
                    rows=obj.get("rows") or [],
                    row_count=int(obj.get("row_count") or 0),
                    truncated=bool(obj.get("truncated")),
                    max_rows=int(obj.get("max_rows") or max_rows),
                    duration_ms=float(obj.get("duration_ms") or 0.0),
                    error={"code": "QueryFailed", "message": str(obj.get("error") or "")} if obj.get("success") is False else None,
                )

        if query_result is None or not query_result.ok:
            msg = "查询执行失败"
            if query_result and query_result.error:
                msg = f"{msg}：{query_result.error.get('message') or query_result.error.get('code')}"
            return json.dumps(
                SafeQueryResult(
                    answer=f"## 已失败\n{msg}",
                    sql=sql,
                    warnings=["execute_failed"],
                    artifacts={"datasource_id": datasource_id, "tables": tables_obj, "validate": validate_obj},
                ).__dict__,
                ensure_ascii=False,
            )

        columns = query_result.columns
        rows = query_result.rows
        table_md = _md_table(rows, columns) if isinstance(rows, list) else "_(no rows)_"

        sql_execute = {
            "duration_ms": query_result.duration_ms,
            "row_count": query_result.row_count,
            "truncated": query_result.truncated,
            "max_rows": query_result.max_rows,
        }

        # 5) result verification (post-check)
        verify = self._harness.verify(
            sql=sql,
            row_count=query_result.row_count,
            truncated=query_result.truncated,
            max_rows=query_result.max_rows,
        )
        for w in verify.warnings:
            warnings.append(str(w))

        # 输出格式：把“结果”放最上面，便于用户快速看到结论；SQL 放在后面作为证据链。
        answer = (
            f"## 结果\n{table_md}\n\n"
            f"## 解读\n{explanation or '（略）'}\n\n"
            f"## SQL\n```sql\n{sql}\n```\n"
        )

        return json.dumps(
            SafeQueryResult(
                answer=answer,
                sql=sql,
                warnings=warnings,
                artifacts={
                    "datasource_id": datasource_id,
                    "tables": tables_obj,
                    "validate": validate_obj,
                    "sql_execute": sql_execute,
                    "result_verify": {"warnings": verify.warnings, "stats": verify.stats},
                },
            ).__dict__,
            ensure_ascii=False,
        )
