"""Demo clock: starts at 5:30 AM on the demo date and runs in real time."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from src import config

_origin: datetime | None = None
_started: float = 0.0


def start(on: date | None = None) -> None:
    global _origin, _started
    day = on or config.demo_date()
    hh, mm = (int(x) for x in config.DEMO_START_TIME.split(":"))
    _origin = datetime(day.year, day.month, day.day, hh, mm)
    _started = time.monotonic()


def now() -> datetime:
    if _origin is None:
        start()
    return _origin + timedelta(seconds=time.monotonic() - _started)  # type: ignore[operator]


def today() -> date:
    return now().date()


def stamp() -> str:
    return now().isoformat(timespec="seconds")
