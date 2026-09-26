"""SQLite storage. One shared connection guarded by a lock; the app is small."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from src import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    beds INTEGER NOT NULL,
    patients_per_nurse INTEGER NOT NULL,
    requires_cert TEXT              -- e.g. 'icu': every RN on the shift must hold it
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,
    home_unit TEXT,
    pool TEXT NOT NULL,             -- core | float | per_diem | agency
    icu_certified INTEGER NOT NULL DEFAULT 0,
    charge_certified INTEGER NOT NULL DEFAULT 0,
    hire_date TEXT,
    hourly_rate REAL NOT NULL,
    wants_extra_shifts INTEGER NOT NULL DEFAULT 0,
    last_asked_at TEXT,             -- rotation: who was asked to cover least recently
    sim_persona TEXT                -- JSON; only used by the call simulator
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    unit_id TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,             -- day | night
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    census INTEGER NOT NULL,
    UNIQUE (unit_id, date, kind)
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL,
    staff_id INTEGER NOT NULL,
    status TEXT NOT NULL,           -- scheduled | called_out | leave
    role TEXT NOT NULL DEFAULT 'rn',-- rn | charge
    source TEXT NOT NULL DEFAULT 'schedule', -- schedule | backfill
    tier INTEGER,
    incentive REAL,
    created_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_assign_staff ON assignments (staff_id);
CREATE INDEX IF NOT EXISTS ix_assign_shift ON assignments (shift_id);

CREATE TABLE IF NOT EXISTS leave_requests (
    id INTEGER PRIMARY KEY,
    staff_id INTEGER NOT NULL,
    kind TEXT NOT NULL,             -- sick | emergency | bereavement | unspecified | vacation | personal
    category TEXT NOT NULL,         -- unplanned | planned
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,           -- logged | approved | pending_supervisor | alternatives_offered | declined | withdrawn
    requested_at TEXT NOT NULL,
    decision TEXT,                  -- JSON: every rule checked and its result
    channel TEXT,
    decided_by TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY,
    shift_id INTEGER NOT NULL,
    leave_id INTEGER,
    vacated_staff_id INTEGER,
    needs_charge INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,           -- open | filling | filled | escalated
    created_at TEXT NOT NULL,
    filled_at TEXT,
    filled_by INTEGER
);

CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY,
    gap_id INTEGER NOT NULL,
    staff_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    tier INTEGER NOT NULL,
    status TEXT NOT NULL,           -- queued | calling | declined | accepted | no_answer | skipped | not_needed
    incentive REAL,
    est_cost REAL,
    overtime INTEGER,
    reasons TEXT,                   -- JSON list of plain-language ranking reasons
    call_id INTEGER,
    started_at TEXT,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    direction TEXT NOT NULL,        -- inbound | outbound
    staff_id INTEGER,
    status TEXT NOT NULL,           -- active | ended
    purpose TEXT,
    outreach_id INTEGER,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    transcript TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """(Re)open the database. Pass ':memory:' in tests."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(str(path or config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        return _conn


def conn() -> sqlite3.Connection:
    if _conn is None:
        connect()
    return _conn  # type: ignore[return-value]


def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, params).fetchall()]


def one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | list = ()) -> int:
    with _lock:
        cur = conn().execute(sql, params)
        conn().commit()
        return cur.lastrowid or 0


def executemany(sql: str, rows: list[tuple]) -> None:
    with _lock:
        conn().executemany(sql, rows)
        conn().commit()


def reset() -> None:
    with _lock:
        c = conn()
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            c.execute(f"DROP TABLE IF EXISTS {name}")
        c.commit()
        c.executescript(SCHEMA)


def get_meta(key: str, default: str | None = None) -> str | None:
    row = one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    return json.loads(raw)
