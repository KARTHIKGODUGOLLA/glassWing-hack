"""End to end through the HTTP API, offline mode: the demo script must always work."""
import time

from fastapi.testclient import TestClient

from src import db
from src.server import app


def wait_for(predicate, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_sick_call_is_backfilled_autonomously():
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        demo = state["demo"]
        icu_day = next(s for s in state["today"] if s["unit_id"] == "icu" and s["kind"] == "day")
        assert (icu_day["staffed"], icu_day["required"]) == (3, 3)

        call_id = client.post("/api/calls/inbound", json={"staff_id": demo["callout_staff_id"]}).json()["call_id"]
        client.post(f"/api/calls/{call_id}/say",
                    json={"text": "Hi, it's Maria. I woke up with a fever and I can't make my shift today."})

        def filled():
            gap = db.one("SELECT status FROM gaps ORDER BY id DESC LIMIT 1")
            return gap is not None and gap["status"] == "filled"

        assert wait_for(filled), client.get("/api/state").json()["events"][:10]
        state = client.get("/api/state").json()
        gap = state["gaps"][0]
        statuses = {c["name"]: c["status"] for c in gap["candidates"]}
        assert statuses["Jordan Blake"] == "declined"
        assert statuses["Priya Patel"] == "accepted"
        assert gap["filled_by"] == "Priya Patel"
        icu_day = next(s for s in state["today"] if s["unit_id"] == "icu" and s["kind"] == "day")
        assert (icu_day["staffed"], icu_day["required"]) == (3, 3)
        assert any("Denise Howard" in n["recipient"] and "Priya Patel" in n["message"] for n in state["notifications"])
        inbound = next(c for c in state["calls"] if c["id"] == call_id)
        assert inbound["status"] == "ended" and "logged out sick" in inbound["transcript"][-1]["text"]


def test_planned_leave_conversation_offers_then_takes_alternative():
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        scripts = {s["label"]: s for s in state["scripts"]}
        call_id = client.post("/api/calls/inbound", json={"staff_id": state["demo"]["leave_staff_id"]}).json()["call_id"]
        client.post(f"/api/calls/{call_id}/say", json={"text": scripts["Thin-day leave"]["text"]})
        call = next(c for c in client.get("/api/state").json()["calls"] if c["id"] == call_id)
        assert call["stage"] == "offered" and call["status"] == "active"
        client.post(f"/api/calls/{call_id}/say", json={"text": scripts["Take alternate"]["text"]})
        state = client.get("/api/state").json()
        statuses = [(l["start_date"], l["status"]) for l in state["leave"] if l["staff_id"] == state["demo"]["leave_staff_id"]]
        assert (state["demo"]["alt_days"][0], "approved") in statuses
        assert (state["demo"]["thin_day"], "withdrawn") in statuses
