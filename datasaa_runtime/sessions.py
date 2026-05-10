from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_consolidated: int = 0
    datasource_id: str | None = None
    updated_at: str | None = None

    def add(self, role: str, content: str, *, meta: dict[str, Any] | None = None) -> None:
        self.messages.append(
            {
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat(),
                **({"meta": meta} if meta else {}),
            }
        )
        self.updated_at = datetime.now().isoformat()

    def get_history(self, *, max_messages: int = 60) -> list[dict[str, Any]]:
        history = self.messages[self.last_consolidated :]
        if max_messages <= 0:
            return history
        return history[-max_messages:]


class SessionStore:
    """Simple JSON session store under workspace/sessions/."""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace
        self.dir = self.root / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace(":", "_")
        return self.dir / f"{safe}.json"

    def load(self, key: str) -> Session:
        path = self._path(key)
        if not path.exists():
            return Session(key=key)
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session(
            key=key,
            messages=data.get("messages") or [],
            last_consolidated=int(data.get("last_consolidated") or 0),
            datasource_id=data.get("datasource_id"),
            updated_at=data.get("updated_at"),
        )

    def save(self, session: Session) -> None:
        path = self._path(session.key)
        payload = {
            "key": session.key,
            "messages": session.messages,
            "last_consolidated": session.last_consolidated,
            "datasource_id": session.datasource_id,
            "updated_at": session.updated_at,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
