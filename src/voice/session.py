"""Call sessions: transcripts, live transcript events, and speech pacing.

In the browser, each line is spoken with the Web Speech API. Pacing keeps the
server from racing ahead of the voices.
"""
from __future__ import annotations

import asyncio

from src import clock, db, events

SETTINGS = {
    "candidate_mode": "simulated",  # simulated | live (a person answers as the called nurse)
    "pace": 1.0,                    # 0 in tests; 1.0 = roughly real speaking speed
}

_inboxes: dict[int, asyncio.Queue] = {}


def create(direction: str, staff_id: int, purpose: str, outreach_id: int | None = None) -> int:
    call_id = db.execute(
        "INSERT INTO calls (direction, staff_id, status, purpose, outreach_id, started_at) VALUES (?, ?, 'active', ?, ?, ?)",
        (direction, staff_id, purpose, outreach_id, clock.stamp()),
    )
    staff = db.one("SELECT name FROM staff WHERE id=?", (staff_id,))
    events.push("call.started", {"call_id": call_id, "direction": direction, "name": staff["name"], "purpose": purpose})
    return call_id


def get(call_id: int) -> dict:
    call = db.one("SELECT * FROM calls WHERE id=?", (call_id,))
    if not call:
        raise KeyError(f"call {call_id} not found")
    call["transcript"] = db.loads(call["transcript"], [])
    call["state"] = db.loads(call["state"], {})
    return call


def save_state(call_id: int, state: dict) -> None:
    db.execute("UPDATE calls SET state=? WHERE id=?", (db.dumps(state), call_id))


def add_line(call_id: int, speaker: str, text: str) -> None:
    """speaker: 'agent' or 'staff'."""
    call = get(call_id)
    name = "ShiftVoice" if speaker == "agent" else db.one("SELECT name FROM staff WHERE id=?", (call["staff_id"],))["name"]
    line = {"speaker": speaker, "name": name, "text": text, "ts": clock.stamp()}
    call["transcript"].append(line)
    db.execute("UPDATE calls SET transcript=? WHERE id=?", (db.dumps(call["transcript"]), call_id))
    events.push("call.line", {"call_id": call_id, "direction": call["direction"], **line})


def end(call_id: int, outcome: str | None = None) -> None:
    db.execute("UPDATE calls SET status='ended', ended_at=? WHERE id=? AND status='active'", (clock.stamp(), call_id))
    _inboxes.pop(call_id, None)
    events.push("call.ended", {"call_id": call_id, "outcome": outcome})


def speech_seconds(text: str) -> float:
    return (0.6 + len(text.split()) * 0.36) * SETTINGS["pace"]


async def say(call_id: int, speaker: str, text: str) -> None:
    add_line(call_id, speaker, text)
    await asyncio.sleep(speech_seconds(text))


async def pause(seconds: float) -> None:
    await asyncio.sleep(seconds * SETTINGS["pace"])


# --- Live mode: a person answers as the called nurse from the dashboard ---------

def inbox(call_id: int) -> asyncio.Queue:
    return _inboxes.setdefault(call_id, asyncio.Queue())


def deliver(call_id: int, text: str) -> bool:
    q = _inboxes.get(call_id)
    if q is None:
        return False
    q.put_nowait(text)
    return True
