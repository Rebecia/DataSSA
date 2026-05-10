from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class OpenAICompatConfig:
    api_base: str
    api_key: str
    model: str
    timeout_s: float = 60.0
    extra_headers: dict[str, str] | None = None


class OpenAICompatProvider:
    """Minimal OpenAI-compatible Chat Completions client (vendor-free)."""

    def __init__(self, cfg: OpenAICompatConfig) -> None:
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        # Some http clients reject an empty header value (e.g. "Bearer ").
        headers: dict[str, str] = {}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        if self.cfg.extra_headers:
            headers.update(self.cfg.extra_headers)
        return headers

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        url = self.cfg.api_base.rstrip("/") + "/v1/chat/completions"
        if not self.cfg.api_key:
            raise RuntimeError("LLM_API_KEY is empty (set env LLM_API_KEY)")
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        async with httpx.AsyncClient(timeout=self.cfg.timeout_s) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            # Some providers return non-JSON on error; keep it readable.
            try:
                data = resp.json()
            except Exception:
                raise RuntimeError(f"LLM http {resp.status_code}: {resp.text[:500]}")
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM http {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:800]}")
            return data
