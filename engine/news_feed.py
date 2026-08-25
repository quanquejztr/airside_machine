"""
Thread-safe rolling news line for the flight board footer and CLI alerts.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_MAX_LINES = 200
_lines: deque[str] = deque(maxlen=_MAX_LINES)
_lock = threading.Lock()
_scroll = 0


def push_news(message: str) -> None:
    """Append one ticker message (flight events, fuel, auto-pause, etc.)."""
    if not message or not str(message).strip():
        return
    ts = time.strftime("%H:%M:%S")
    with _lock:
        _lines.append(f"{ts}  {message.strip()}")


def recent_lines(n: int = 30) -> list[str]:
    with _lock:
        return list(_lines)[-n:]


def ticker_plain_text(width: int = 96) -> str:
    """
    Single-line scrolling text for Rich footer. Advances scroll position each call.
    """
    with _lock:
        parts = list(_lines)[-40:]
        global _scroll
        if not parts:
            return "News: —"
        blob = "  ·  ".join(parts)
        if len(blob) <= width:
            return blob
        _scroll = (_scroll + 1) % len(blob)
        doubled = blob + "  ·  "
        chunk = doubled[_scroll : _scroll + width]
        if len(chunk) < width:
            chunk = (chunk + doubled[: width - len(chunk)])[:width]
        return chunk


def clear_feed() -> None:
    with _lock:
        _lines.clear()
        global _scroll
        _scroll = 0
