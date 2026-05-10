#!/usr/bin/env python3
"""
db_server.py - 安全的数据库查询 MCP Server

提供四个工具：
- query_database: 执行只读 SQL 查询
- list_tables: 列出所有数据表
- describe_table: 查看表结构
- get_statistics: 获取表的统计信息

安全特性：
- 只允许 SELECT 语句
- SQL 注入检测
- 查询超时控制
- 结果行数限制
- 完整的审计日志

这个 MCP Server 的定位：
- 给“数据分析助手 Agent”提供一个安全的只读查询接口
- 用最少的能力满足常见分析流程：先看有哪些表 → 看字段结构 → 再查数据 → 看统计信息
- 默认以 SQLite 为示例（也便于本地复现），并通过多层校验尽量降低误用风险

关于 trace_id：
- DataSSA 在上游会生成 trace_id，并通过 MCP tool 的入参透传到本 server
- 本 server 会把 trace_id 写入 audit.log 的 meta 中，用于“trace ↔ audit”关联
"""

import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional


# ==================== 配置 ====================

class Config:
    """
    运行时配置（全部来自环境变量，提供默认值）。

    说明：
    - DB_PATH：SQLite 数据库文件路径（默认 business.db）
    - QUERY_TIMEOUT：单次查询超时（秒）
    - MAX_ROWS：最大返回行数（防止一次性拉太多数据）
    - MAX_CONCURRENT：最大并发查询数（防止资源被打爆）
    - AUDIT_LOG_PATH：审计日志输出文件（每行一个 JSON）
    - READONLY：是否用只读方式打开 DB（推荐 true）
    """
    DB_PATH = os.getenv("DB_PATH", "business.db")
    QUERY_TIMEOUT = int(os.getenv("QUERY_TIMEOUT", "30"))
    MAX_ROWS = int(os.getenv("MAX_ROWS", "1000"))
    MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "5"))
    AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "audit.log")
    READONLY = os.getenv("DB_READONLY", "true").lower() == "true"
    DATASSA_DATASOURCE_ID = os.getenv("DATASSA_DATASOURCE_ID", "")


# ==================== 审计日志 ====================

class AuditLogger:
    """查询审计日志记录器"""

    def __init__(self, log_path: str):
        self.log_path = log_path
        # 多线程写日志需要互斥，避免日志行互相穿插
        self._lock = threading.Lock()

    def log_query(
        self,
        sql: str,
        success: bool,
        duration_ms: float,
        rows_returned: int = 0,
        error: str = "",
        meta: dict | None = None,
    ):
        """记录一次查询（成功/失败、耗时、返回行数等）。"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "query",
            "sql": sql,
            "success": success,
            "duration_ms": round(duration_ms, 2),
            "rows_returned": rows_returned,
            "error": error,
        }
        if meta:
            entry["meta"] = meta
        self._write(entry)

    def log_blocked(self, sql: str, reason: str, meta: dict | None = None):
        """记录一次被安全策略拦截的查询。"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "blocked",
            "sql": sql,
            "reason": reason,
        }
        if meta:
            entry["meta"] = meta
        self._write(entry)

    def _write(self, entry: dict):
        """向审计日志文件追加一行 JSON；写入失败时静默忽略（避免影响主流程）。"""
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except IOError:
                pass


# ==================== SQL 安全检查 ====================

class SQLSecurityChecker:
    """SQL 安全检查器 - 多层防护"""

    # 这些关键字即便出现在子句里也很危险；我们选择“宁可错杀”以换取安全性
    DANGEROUS_KEYWORDS = [
        "DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE",
        "CREATE", "REPLACE", "GRANT", "REVOKE", "EXEC", "EXECUTE",
        "CALL", "MERGE", "UPSERT", "ATTACH", "DETACH",
    ]

    # 常见的注入/绕过模式（正则）：多语句、UNION、注释、延时、系统表等
    INJECTION_PATTERNS = [
        r";\s*(DROP|DELETE|UPDATE|INSERT|ALTER)",
        r"UNION\s+(ALL\s+)?SELECT",
        r"OR\s+1\s*=\s*1",
        r"OR\s+'1'\s*=\s*'1'",
        # 拦截 SQL 单行注释（例如：`SELECT * FROM users -- comment`）
        r"--.*$",
        r"/\*.*\*/",
        r"SLEEP\s*\(",
        r"BENCHMARK\s*\(",
        r"LOAD_FILE\s*\(",
        r"INTO\s+(OUTFILE|DUMPFILE)",
        r"@@(version|datadir|basedir)",
        r"INFORMATION_SCHEMA",
    ]

    def check(self, sql: str) -> tuple[bool, str]:
        """
        检查 SQL 是否安全。
        返回 (is_safe, reason)
        """
        if not sql or not sql.strip():
            return False, "空查询"

        normalized = sql.strip().upper()

        # 只允许 SELECT / WITH (CTE) 开头
        if not self._is_select_statement(normalized):
            return False, "只允许 SELECT 查询语句"

        # 关键字黑名单
        keyword_check = self._check_dangerous_keywords(normalized)
        if not keyword_check[0]:
            return keyword_check

        # 注入模式检测
        injection_check = self._check_injection_patterns(sql)
        if not injection_check[0]:
            return injection_check

        # 防止多语句执行（例如 SELECT ...; DROP TABLE ...）
        if normalized.count(";") > 0:
            parts = [p.strip() for p in normalized.split(";") if p.strip()]
            if len(parts) > 1:
                return False, "不允许多语句执行"

        return True, "通过安全检查"

    def _is_select_statement(self, sql: str) -> bool:
        """判断是否以 SELECT / WITH 开头（允许最前面有括号/空白）。"""
        cleaned = sql.lstrip("( \t\n")
        return cleaned.startswith("SELECT") or cleaned.startswith("WITH")

    def _check_dangerous_keywords(self, sql: str) -> tuple[bool, str]:
        """把 SQL 拆成 token，检查是否包含危险关键字。"""
        words = set(re.findall(r'\b[A-Z_]+\b', sql))
        for keyword in self.DANGEROUS_KEYWORDS:
            if keyword in words:
                return False, f"包含禁止的关键字: {keyword}"
        return True, ""

    def _check_injection_patterns(self, sql: str) -> tuple[bool, str]:
        """用正则扫描常见注入模式。"""
        for pattern in self.INJECTION_PATTERNS:
            if re.search(pattern, sql, re.IGNORECASE):
                return False, f"检测到可疑的注入模式"
        return True, ""


# ==================== 数据库连接管理 ====================

class DatabaseManager:
    """数据库连接和查询管理器"""

    def __init__(self, db_path: str, readonly: bool = True):
        self.db_path = db_path
        self.readonly = readonly
        # 控制并发：同一时间最多允许 MAX_CONCURRENT 个查询在跑
        self._semaphore = threading.Semaphore(Config.MAX_CONCURRENT)

    @contextmanager
    def get_connection(self):
        """获取数据库连接（带并发控制）"""
        # acquire(timeout=10)：避免请求永远阻塞；超时则抛错提示稍后重试
        acquired = self._semaphore.acquire(timeout=10)
        if not acquired:
            raise RuntimeError("并发查询数已达上限，请稍后重试")

        try:
            # SQLite 只读模式：使用 URI file:xxx?mode=ro
            uri = f"file:{self.db_path}?mode=ro" if self.readonly else self.db_path
            conn = sqlite3.connect(
                uri if self.readonly else self.db_path,
                uri=self.readonly,
                timeout=Config.QUERY_TIMEOUT,
            )
            conn.row_factory = sqlite3.Row
            # busy_timeout：当数据库被占用时最多等待多少毫秒
            conn.execute(f"PRAGMA busy_timeout = {Config.QUERY_TIMEOUT * 1000}")
            yield conn
        finally:
            conn.close()
            self._semaphore.release()

    def execute_query(self, sql: str, max_rows: int = None) -> dict:
        """执行查询并返回结果"""
        if max_rows is None:
            max_rows = Config.MAX_ROWS

        start_time = time.time()

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 通过定时器 + conn.interrupt() 实现“软超时”
            timer = threading.Timer(Config.QUERY_TIMEOUT, self._cancel_query, [conn])
            timer.start()

            try:
                cursor.execute(sql)
                # fetchmany：只拉取最多 max_rows+1 行，用来判断是否截断
                rows = cursor.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                if truncated:
                    rows = rows[:max_rows]

                columns = (
                    [description[0] for description in cursor.description]
                    if cursor.description
                    else []
                )

                duration_ms = (time.time() - start_time) * 1000

                return {
                    "success": True,
                    "columns": columns,
                    "rows": [dict(row) for row in rows],
                    "row_count": len(rows),
                    "truncated": truncated,
                    "max_rows": max_rows,
                    "duration_ms": round(duration_ms, 2),
                }
            except sqlite3.OperationalError as e:
                # conn.interrupt() 通常会导致 "interrupted" 的 OperationalError
                if "interrupted" in str(e).lower():
                    raise TimeoutError("查询超时")
                raise
            finally:
                timer.cancel()

    def list_tables(self) -> list[dict]:
        """列出所有表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # sqlite_master：SQLite 系统表，包含所有表/视图定义
            cursor.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name"
            )
            tables = []
            for row in cursor.fetchall():
                count_cursor = conn.cursor()
                try:
                    # 统计行数可能在某些对象（视图/权限受限）上失败，因此 try/except
                    count_cursor.execute(f'SELECT COUNT(*) FROM "{row["name"]}"')
                    count = count_cursor.fetchone()[0]
                except Exception:
                    count = -1

                tables.append({
                    "name": row["name"],
                    "type": row["type"],
                    "row_count": count,
                })
            return tables

    def describe_table(self, table_name: str) -> dict:
        """查看表结构"""
        # 表名白名单校验：防止通过表名拼接进行注入（PRAGMA 也能被利用）
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name):
            raise ValueError("无效的表名")

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f'PRAGMA table_info("{table_name}")')
            columns = []
            for row in cursor.fetchall():
                columns.append({
                    "name": row["name"],
                    "type": row["type"],
                    "nullable": not row["notnull"],
                    "default": row["dflt_value"],
                    "primary_key": bool(row["pk"]),
                })

            cursor.execute(f'PRAGMA index_list("{table_name}")')
            indexes = []
            for row in cursor.fetchall():
                indexes.append({
                    "name": row["name"],
                    "unique": bool(row["unique"]),
                })

            return {
                "table_name": table_name,
                "columns": columns,
                "indexes": indexes,
                "column_count": len(columns),
            }

    def get_statistics(self, table_name: str) -> dict:
        """获取表的统计信息"""
        # 表名白名单校验：避免注入
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name):
            raise ValueError("无效的表名")

        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(f'SELECT COUNT(*) as total FROM "{table_name}"')
            total = cursor.fetchone()[0]

            cursor.execute(f'PRAGMA table_info("{table_name}")')
            columns_info = cursor.fetchall()

            column_stats = []
            for col in columns_info:
                col_name = col["name"]
                col_type = col["type"].upper()
                stats = {"name": col_name, "type": col["type"]}

                try:
                    # 唯一值数量
                    cursor.execute(
                        f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}"'
                    )
                    stats["distinct_count"] = cursor.fetchone()[0]

                    # 空值数量
                    cursor.execute(
                        f'SELECT COUNT(*) FROM "{table_name}" WHERE "{col_name}" IS NULL'
                    )
                    stats["null_count"] = cursor.fetchone()[0]

                    # 数值列统计（min/max/avg）
                    if col_type in ("INTEGER", "REAL", "NUMERIC", "FLOAT", "DOUBLE"):
                        cursor.execute(
                            f'SELECT MIN("{col_name}"), MAX("{col_name}"), '
                            f'AVG("{col_name}") FROM "{table_name}"'
                        )
                        row = cursor.fetchone()
                        stats["min"] = row[0]
                        stats["max"] = row[1]
                        stats["avg"] = round(row[2], 2) if row[2] else None
                except Exception:
                    pass

                column_stats.append(stats)

            return {
                "table_name": table_name,
                "total_rows": total,
                "column_stats": column_stats,
            }

    @staticmethod
    def _cancel_query(conn):
        """超时回调：中断当前连接上的执行。"""
        try:
            conn.interrupt()
        except Exception:
            pass


# ==================== MCP Server ====================

class DatabaseMCPServer:
    """数据库查询 MCP Server"""

    def __init__(self):
        # 数据库访问/安全/审计三件套
        self.db = DatabaseManager(Config.DB_PATH, Config.READONLY)
        self.security = SQLSecurityChecker()
        self.audit = AuditLogger(Config.AUDIT_LOG_PATH)
        self._datasource_id = Config.DATASSA_DATASOURCE_ID or None

    def handle_request(self, request: dict) -> dict | None:
        """处理 JSON-RPC 请求，根据 method 分发到对应 handler。

        注意：MCP 客户端会发送 notification（没有 id）。
        对于 notification，Server 不应返回任何响应，否则会产生 `id: null` 的非法响应，
        进而导致客户端 JSON-RPC 解析失败。
        """
        method = request.get("method", "")
        req_id = request.get("id")

        handlers = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_list_tools,
            "tools/call": self._handle_call_tool,
        }

        handler = handlers.get(method)
        if handler:
            result = handler(request)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        # Notification：没有 id，不回任何内容
        if req_id is None:
            return None

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }

    def _handle_initialize(self, request: dict) -> dict:
        """MCP initialize：返回协议版本与服务信息。"""
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "database-query-server",
                "version": "1.0.0",
            },
            "capabilities": {"tools": {}},
        }

    def _handle_list_tools(self, request: dict) -> dict:
        """tools/list：声明可用工具与 JSON Schema 入参。"""
        return {
            "tools": [
                {
                    "name": "validate_sql",
                    "description": (
                        "执行 SQL 静态安全校验（不执行查询）。"
                        "用于在真正执行前提前给出是否允许/风险提示。"
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "sql": {
                                "type": "string",
                                "description": "要校验的 SQL（仅允许 SELECT/WITH）",
                            },
                            "trace_id": {"type": "string", "description": "可选：trace_id（用于审计关联）"},
                        },
                        "required": ["sql"],
                    },
                },
                {
                    "name": "query_database",
                    "description": (
                        "执行只读 SQL 查询。只允许 SELECT 语句，"
                        "内置 SQL 注入防护和查询超时控制。"
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "sql": {
                                "type": "string",
                                "description": "要执行的 SELECT SQL 语句",
                            },
                            "max_rows": {
                                "type": "integer",
                                "description": "最大返回行数（默认1000）",
                                "default": 1000,
                            },
                            "trace_id": {"type": "string", "description": "可选：trace_id（用于审计关联）"},
                        },
                        "required": ["sql"],
                    },
                },
                {
                    "name": "list_tables",
                    "description": "列出数据库中的所有表和视图，包含行数统计",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"trace_id": {"type": "string", "description": "可选：trace_id（用于审计关联）"}},
                    },
                },
                {
                    "name": "describe_table",
                    "description": "查看指定表的结构信息，包含字段名、类型、索引等",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "table_name": {
                                "type": "string",
                                "description": "表名",
                            },
                            "trace_id": {"type": "string", "description": "可选：trace_id（用于审计关联）"},
                        },
                        "required": ["table_name"],
                    },
                },
                {
                    "name": "get_statistics",
                    "description": (
                        "获取指定表的统计信息，包含行数、各列的"
                        "唯一值数量、空值数量、数值列的最大/最小/平均值"
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "table_name": {
                                "type": "string",
                                "description": "表名",
                            },
                            "trace_id": {"type": "string", "description": "可选：trace_id（用于审计关联）"},
                        },
                        "required": ["table_name"],
                    },
                },
            ]
        }

    def _handle_call_tool(self, request: dict) -> dict:
        """tools/call：执行工具并返回结果（文本 JSON）。"""
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        tool_handlers = {
            "validate_sql": self._tool_validate_sql,
            "query_database": self._tool_query_database,
            "list_tables": self._tool_list_tables,
            "describe_table": self._tool_describe_table,
            "get_statistics": self._tool_get_statistics,
        }

        handler = tool_handlers.get(tool_name)
        if not handler:
            return self._error_response(f"未知工具: {tool_name}")

        try:
            result = handler(arguments)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, default=str),
                    }
                ]
            }
        except TimeoutError as e:
            # 超时场景：也记一条审计日志，便于排查慢查询
            self.audit.log_query(
                str(arguments), False, 0, error="查询超时"
            )
            return self._error_response(f"查询超时（超过 {Config.QUERY_TIMEOUT} 秒）")
        except ValueError as e:
            # 安全检查不通过、表名非法等会走 ValueError
            return self._error_response(str(e))
        except Exception as e:
            # 其它错误统一返回泛化信息，避免泄露底层细节
            return self._error_response("查询执行出错，请检查 SQL 语法")

    def _tool_query_database(self, args: dict) -> dict:
        """query_database 工具：先安全检查，再执行查询，最后审计记录。"""
        sql = args.get("sql", "")
        max_rows = args.get("max_rows", Config.MAX_ROWS)
        trace_id = args.get("trace_id", "") or ""
        trace_id = trace_id.strip() if isinstance(trace_id, str) else ""

        is_safe, reason = self.security.check(sql)
        if not is_safe:
            meta = {"tool": "query_database", "datasource_id": self._datasource_id, **({"trace_id": trace_id} if trace_id else {})}
            self.audit.log_blocked(sql, reason, meta=meta)
            raise ValueError(f"安全检查未通过: {reason}")

        result = self.db.execute_query(sql, max_rows)

        # 审计：记录成功与耗时、行数
        self.audit.log_query(
            sql,
            result["success"],
            result["duration_ms"],
            result["row_count"],
            meta={
                "tool": "query_database",
                "datasource_id": self._datasource_id,
                "truncated": result.get("truncated"),
                "max_rows": result.get("max_rows"),
                **({"trace_id": trace_id} if trace_id else {}),
            },
        )

        return result

    def _tool_validate_sql(self, args: dict) -> dict:
        """validate_sql 工具：只做静态安全校验，不执行 SQL。"""
        sql = args.get("sql", "")
        trace_id = args.get("trace_id", "") or ""
        trace_id = trace_id.strip() if isinstance(trace_id, str) else ""
        is_safe, reason = self.security.check(sql)

        normalized = (sql or "").strip()
        upper = normalized.upper()

        # 轻量提示：是否缺少 LIMIT / WHERE（不作为拦截规则，仅提示）
        warnings: list[str] = []
        if is_safe:
            if " LIMIT " not in f" {upper} ":
                warnings.append("建议加 LIMIT（防止返回结果过大）")
            if " WHERE " not in f" {upper} ":
                warnings.append("建议加 WHERE/时间范围（避免全表扫描）")

        if not is_safe:
            # 对 validate 也记录拦截（便于安全分析）
            meta = {"tool": "validate_sql", "datasource_id": self._datasource_id, **({"trace_id": trace_id} if trace_id else {})}
            self.audit.log_blocked(sql, reason, meta=meta)
        else:
            meta = {"tool": "validate_sql", "datasource_id": self._datasource_id, **({"trace_id": trace_id} if trace_id else {})}
            self.audit.log_query("VALIDATE SQL", True, 0, 0, meta=meta)

        return {
            "is_safe": is_safe,
            "reason": reason,
            "warnings": warnings,
            "policy": {
                "timeout_s": Config.QUERY_TIMEOUT,
                "max_rows": Config.MAX_ROWS,
                "readonly": Config.READONLY,
            },
        }

    def _tool_list_tables(self, args: dict) -> dict:
        """list_tables 工具：列出表/视图，并记录审计。"""
        trace_id = args.get("trace_id", "") or ""
        trace_id = trace_id.strip() if isinstance(trace_id, str) else ""
        tables = self.db.list_tables()
        meta = {"tool": "list_tables", "datasource_id": self._datasource_id, **({"trace_id": trace_id} if trace_id else {})}
        self.audit.log_query("LIST TABLES", True, 0, len(tables), meta=meta)
        return {"tables": tables, "count": len(tables)}

    def _tool_describe_table(self, args: dict) -> dict:
        """describe_table 工具：返回字段/索引信息。"""
        table_name = args.get("table_name", "")
        trace_id = args.get("trace_id", "") or ""
        trace_id = trace_id.strip() if isinstance(trace_id, str) else ""
        result = self.db.describe_table(table_name)
        self.audit.log_query(
            f"DESCRIBE {table_name}",
            True,
            0,
            meta={"tool": "describe_table", "table": table_name, "datasource_id": self._datasource_id, **({"trace_id": trace_id} if trace_id else {})},
        )
        return result

    def _tool_get_statistics(self, args: dict) -> dict:
        """get_statistics 工具：返回表总行数 + 每列统计（distinct/null/min/max/avg）。"""
        table_name = args.get("table_name", "")
        trace_id = args.get("trace_id", "") or ""
        trace_id = trace_id.strip() if isinstance(trace_id, str) else ""
        result = self.db.get_statistics(table_name)
        self.audit.log_query(
            f"STATISTICS {table_name}",
            True,
            0,
            meta={"tool": "get_statistics", "table": table_name, "datasource_id": self._datasource_id, **({"trace_id": trace_id} if trace_id else {})},
        )
        return result

    @staticmethod
    def _error_response(message: str) -> dict:
        return {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        }

    def run(self):
        """启动 MCP Server"""
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break

                # MCP stdio 模式：每行一个 JSON-RPC 请求
                request = json.loads(line.strip())
                response = self.handle_request(request)
                # Notification 没有 response（None），不要写任何输出
                if response is not None:
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
            except json.JSONDecodeError:
                # 忽略非法输入行
                continue
            except KeyboardInterrupt:
                break


# ==================== 测试用例 ====================

def create_sample_database():
    """创建示例数据库用于测试"""
    # 注意：这部分用于本地演示/测试安全规则，不参与 MCP server 的运行逻辑
    conn = sqlite3.connect(Config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            department TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            is_active INTEGER DEFAULT 1
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    sample_users = [
        ("张三", "zhangsan@example.com", "工程部"),
        ("李四", "lisi@example.com", "产品部"),
        ("王五", "wangwu@example.com", "市场部"),
        ("赵六", "zhaoliu@example.com", "工程部"),
        ("陈七", "chenqi@example.com", "销售部"),
    ]

    cursor.executemany(
        "INSERT OR IGNORE INTO users (name, email, department) VALUES (?, ?, ?)",
        sample_users,
    )

    sample_orders = [
        (1, "云服务器 ECS", 2999.00, "completed"),
        (1, "对象存储 OSS", 599.00, "completed"),
        (2, "域名注册", 99.00, "pending"),
        (3, "CDN 加速", 1299.00, "completed"),
        (4, "数据库 RDS", 3999.00, "processing"),
        (5, "安全证书 SSL", 299.00, "completed"),
    ]

    cursor.executemany(
        "INSERT OR IGNORE INTO orders (user_id, product, amount, status) VALUES (?, ?, ?, ?)",
        sample_orders,
    )

    conn.commit()
    conn.close()


def run_security_tests():
    """运行安全检查测试"""
    checker = SQLSecurityChecker()

    test_cases = [
        ("SELECT * FROM users", True, "正常查询"),
        ("SELECT name FROM users WHERE id = 1", True, "带条件查询"),
        ("DROP TABLE users", False, "DROP 攻击"), # 删除整个表
        ("DELETE FROM users", False, "DELETE 攻击"),# 删除表中所有数据
        ("SELECT * FROM users; DROP TABLE users", False, "多语句注入"),
        ("SELECT * FROM users UNION SELECT * FROM passwords", False, "UNION 注入"), #把另一个表的数据“拼”进结果里，数据泄露
        ("SELECT * FROM users WHERE id = 1 OR 1=1", False, "OR 1=1 注入"), # 逻辑绕过，返回所有数据
        ("UPDATE users SET name = 'hacked'", False, "UPDATE 攻击"), # 数据篡改
        ("SELECT * FROM users -- comment", False, "注释注入"), # --在SQL里表示后面的内容是注释，全部忽略，攻击者可以在SQL末尾加上 -- 来注释掉后续的安全检查或条件限制
        ("SELECT SLEEP(10)", False, "延时注入"), #时间盲注，攻击者看不到结果但是可以通过响应时间来判断
        (
            "WITH cte AS (SELECT * FROM users) SELECT * FROM cte",
            True,
            "CTE 查询",
        ),
        # 正常查询，防止检测结果把复杂查询当错误
    ]

    print("=" * 60)
    print("SQL 安全检查测试")
    print("=" * 60)

    passed = 0
    total = len(test_cases)

    for sql, expected_safe, description in test_cases:
        is_safe, reason = checker.check(sql)
        status = "✅" if is_safe == expected_safe else "❌"
        if is_safe == expected_safe:
            passed += 1
        print(f"{status} {description}")
        print(f"   SQL: {sql}")
        print(f"   预期: {'安全' if expected_safe else '拦截'} | "
              f"实际: {'安全' if is_safe else '拦截'}")
        if not is_safe:
            print(f"   原因: {reason}")
        print()

    print(f"测试结果: {passed}/{total} 通过")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        create_sample_database()
        run_security_tests()
    elif len(sys.argv) > 1 and sys.argv[1] == "--init-db":
        create_sample_database()
        print(f"示例数据库已创建: {Config.DB_PATH}")
    else:
        # 默认启动 MCP server（stdio 模式）
        server = DatabaseMCPServer()
        server.run()
