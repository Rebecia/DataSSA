from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VerificationResult:
    warnings: list[str]
    stats: dict[str, Any]


class QueryVerificationHarness:
    """Query Verification Harness（执行后校验）。

    目标：
    - 对执行结果做轻量 sanity check（行数/截断/空结果）
    - 给出可运营的结构化 warnings/stats，便于 trace 记录与复盘统计
    """

    def verify(self, *, sql: str, row_count: int, truncated: bool, max_rows: int | None = None) -> VerificationResult:
        warnings: list[str] = []
        s = (sql or "").strip()
        upper = s.upper()

        if row_count <= 0:
            warnings.append("结果为空：可能是过滤条件过严/数据不存在/表名不匹配。")

        if truncated:
            warnings.append(f"结果已截断（max_rows={max_rows}）。")

        # 经验规则：没有 WHERE 且没有明显聚合时，提醒风险（避免全表扫描）
        if "WHERE" not in upper:
            # 对 count(*) 这类聚合也可以提醒，但弱化为建议
            if "COUNT(" in upper or "SUM(" in upper or "AVG(" in upper:
                warnings.append("建议加 WHERE/时间范围（避免全表扫描）。")
            else:
                warnings.append("建议增加 WHERE/时间范围（避免读取过多数据）。")

        if "SELECT *" in upper:
            warnings.append("建议显式选择列（避免无意返回敏感字段/大字段）。")

        stats = {
            "row_count": int(row_count),
            "truncated": bool(truncated),
            "max_rows": int(max_rows) if max_rows is not None else None,
            "has_where": ("WHERE" in upper),
        }
        return VerificationResult(warnings=warnings, stats=stats)
