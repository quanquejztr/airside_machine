"""Per-competitor narrative tokens for the current AI weekly turn."""

from __future__ import annotations

from typing import Dict, List, Optional

_NARRATIVE: Dict[str, List[str]] = {}


def begin_ai_narrative(competitor_id: str, *, reset: bool = False) -> None:
    cid = str(competitor_id)
    if reset or cid not in _NARRATIVE:
        _NARRATIVE[cid] = []


def append_ai_narrative(competitor_id: str, token: str) -> None:
    cid = str(competitor_id)
    tok = str(token).strip()
    if not tok:
        return
    _NARRATIVE.setdefault(cid, []).append(tok)


def take_ai_narrative(competitor_id: str) -> str:
    toks = _NARRATIVE.pop(str(competitor_id), [])
    return "; ".join(toks)


def competitor_display_name(competitor_id: str) -> str:
    from db import db

    row = db.fetch_one(
        "SELECT name FROM competitors WHERE competitor_id = ?",
        (str(competitor_id),),
    )
    return str(row["name"]) if row and row["name"] else str(competitor_id)


def push_ai_news(message: str) -> None:
    try:
        from engine.news_feed import push_news

        push_news(str(message))
    except Exception:
        pass
