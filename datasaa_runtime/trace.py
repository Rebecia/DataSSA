from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    type: str
    ts: str
    trace_id: str
    session_id: str
    user_id: str | None
    datasource_id: str | None
    payload: dict[str, Any]


class TraceLogger:
    """把一次请求的关键过程以 jsonl 形式落盘，便于回放/排查/统计。"""

    def __init__(
        self,
        *,
        workspace: Path,
        trace_id: str,
        session_id: str,
        datasource_id: str | None = None,
    ) -> None:
        self.trace_id = trace_id
        self.session_id = session_id
        self.datasource_id = datasource_id
        self.user_id = None
        if session_id and ":" in session_id:
            self.user_id = session_id.split(":", 1)[0] or None

        date = datetime.now().strftime("%Y-%m-%d")
        self.dir = (workspace / "traces" / date).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = (self.dir / f"{trace_id}.jsonl").resolve()

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        ev = TraceEvent(
            type=event_type,
            ts=datetime.now().isoformat(),
            trace_id=self.trace_id,
            session_id=self.session_id,
            user_id=self.user_id,
            datasource_id=self.datasource_id,
            payload=payload or {},
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev.__dict__, ensure_ascii=False, default=str) + "\n")
