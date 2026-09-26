"""HTTP API, live event stream and static dashboard."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import db, events, seed
from src.agent import backfill, orchestrator, tools
from src.optimization import leave_policy
from src.ui import dashboard
from src.voice import session, vapi

STATIC = Path(__file__).resolve().parent / "ui" / "static"


def reset_demo() -> dict:
    backfill.cancel_all()
    info = seed.seed()
    events.log("demo.reset", "Demo data reset: synthetic hospital seeded")
    return info


@asynccontextmanager
async def lifespan(_: FastAPI):
    events.bind_loop(asyncio.get_running_loop())
    db.connect()
    reset_demo()
    yield
    backfill.cancel_all()


app = FastAPI(title="ShiftVoice", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Say(BaseModel):
    text: str


class StartCall(BaseModel):
    staff_id: int


class Settings(BaseModel):
    candidate_mode: str | None = None
    pace: float | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    return dashboard.snapshot()


@app.get("/api/schedule")
def schedule(days: int = 28):
    return dashboard.schedule_grid(min(max(days, 7), 60))


@app.get("/api/shifts/{shift_id}")
def shift(shift_id: int):
    return dashboard.shift_detail(shift_id)


@app.get("/api/events")
async def stream():
    q = events.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/reset")
def reset():
    return reset_demo()


@app.post("/api/settings")
def settings(body: Settings):
    if body.candidate_mode in ("simulated", "live"):
        session.SETTINGS["candidate_mode"] = body.candidate_mode
    if body.pace is not None:
        session.SETTINGS["pace"] = max(0.0, min(body.pace, 3.0))
    events.push("settings", session.SETTINGS)
    return session.SETTINGS


@app.post("/api/calls/inbound")
async def start_call(body: StartCall):
    try:
        call_id = await orchestrator.start_inbound(body.staff_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return {"call_id": call_id}


@app.post("/api/calls/{call_id}/say")
async def say(call_id: int, body: Say):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    try:
        call = session.get(call_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    if call["status"] != "active":
        raise HTTPException(409, "call has ended")
    if call["direction"] == "outbound":
        if not session.deliver(call_id, text):
            raise HTTPException(409, "this call is not waiting for a live answer")
        return {"ok": True}
    await orchestrator.handle(call_id, text)
    return {"ok": True}


@app.post("/api/calls/{call_id}/hangup")
async def hangup(call_id: int):
    await orchestrator.hang_up(call_id)
    return {"ok": True}


@app.post("/api/leave/{leave_id}/decide")
def decide(leave_id: int, approve: bool):
    try:
        result = leave_policy.supervisor_decide(leave_id, approve)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result


@app.post("/api/leave/{leave_id}/alternative/{index}")
def take_alternative(leave_id: int, index: int):
    try:
        return tools.accept_alternative(leave_id, index)
    except (IndexError, KeyError, TypeError) as exc:
        raise HTTPException(400, f"no such alternative: {exc}")


@app.post("/api/gaps/{gap_id}/backfill")
def start_backfill(gap_id: int):
    if not db.one("SELECT 1 FROM gaps WHERE id=?", (gap_id,)):
        raise HTTPException(404, "gap not found")
    backfill.schedule(gap_id)
    return {"ok": True}


@app.post("/api/vapi/webhook")
def vapi_webhook(payload: dict):
    return vapi.handle_webhook(payload)
