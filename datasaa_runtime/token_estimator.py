from __future__ import annotations

import math
from typing import Any


def estimate_text_tokens(text: str) -> int:
    # Cheap heuristic: ~4 chars per token for English-ish; Chinese closer to 1-2 chars.
    # We deliberately keep it simple; use real tokenizer if you need precision.
    if not text:
        return 0
    # Weighted: count non-ascii as 1 char, ascii as 0.5 char to reflect compression.
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii_chars = len(text) - ascii_chars
    approx_chars = non_ascii_chars * 1.2 + ascii_chars * 0.5
    return max(1, int(math.ceil(approx_chars / 1.6)))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    return estimate_text_tokens(str(message.get("content") or ""))

