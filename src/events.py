"""Audit log + live event bus.

Every state change is written to the events table (the audit trail) and pushed
to connected dashboards over server-sent events. Safe to call from any thread.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from src import clock, db

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def _fanout(event: dict[str, Any]) -> None:
    for q in list(_subscribers):
        if not q.full():
            q.put_nowait(event)


def push(kind: str, data: dict[str, Any] | None = None) -> None:
    """Send a live-only event (not audited), e.g. a transcript line."""
    event = {"kind": kind, "ts": clock.stamp(), "data": data or {}}
    if _loop is None or _loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        _fanout(event)
    else:
        _loop.call_soon_threadsafe(_fanout, event)


def log(kind: str, message: str, data: dict[str, Any] | None = None) -> None:
    """Record an auditable event and broadcast it."""
    db.execute(
        "INSERT INTO events (ts, kind, message, data) VALUES (?, ?, ?, ?)",
        (clock.stamp(), kind, message, db.dumps(data or {})),
    )
    push(kind, {"message": message, **(data or {})})
