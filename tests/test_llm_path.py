"""The LLM path with a stubbed model: output is parsed, and a bad answer can't book anyone."""
import asyncio

from src import config, db
from src.agent import llm, orchestrator
from src.voice import call_agent


def online(monkeypatch, reply):
    monkeypatch.setattr(config, "SCIFORIUM_API_KEY", "test")
    monkeypatch.setattr(config, "SCIFORIUM_MODEL", "stub-model")
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: reply(messages))


def test_parse_json_tolerates_reasoning_and_fences():
    text = '<think>hmm</think>Sure!\n```json\n{"reply": "ok", "intent": "callout"}\n```'
    assert llm.parse_json(text) == {"reply": "ok", "intent": "callout"}
    assert llm.parse_json("no json here") is None


def test_llm_extraction_drives_the_rules_engine(demo, monkeypatch):
    def reply(messages):
        if "policy engine has made a decision" in messages[0]["content"]:
            return "You're logged out sick, Maria. Feel better."
        return ('{"reply": "Sorry to hear that.", "intent": "callout", "leave_type": "sick", '
                f'"start_date": "{demo["date"]}", "end_date": null, "shift": "day"}}')
    online(monkeypatch, reply)
    monkeypatch.setattr("src.agent.backfill.schedule", lambda gap_id: None)

    async def run():
        call_id = await orchestrator.start_inbound(demo["callout_staff_id"])
        await orchestrator.handle(call_id, "ugh, fever, not coming in")
        return call_id

    call_id = asyncio.run(run())
    req = db.one("SELECT * FROM leave_requests WHERE staff_id=? AND category='unplanned'", (demo["callout_staff_id"],))
    assert req["status"] == "logged" and req["kind"] == "sick"
    assert db.one("SELECT COUNT(*) n FROM gaps")["n"] == 1
    last = db.loads(db.one("SELECT transcript FROM calls WHERE id=?", (call_id,))["transcript"])[-1]
    assert last["text"] == "You're logged out sick, Maria. Feel better."


def test_garbage_llm_output_falls_back_to_rules(demo, monkeypatch):
    online(monkeypatch, lambda messages: "I am not JSON")
    slots, reply = orchestrator.understand(
        {"state": {}, "transcript": []}, db.one("SELECT * FROM staff WHERE id=?", (demo["callout_staff_id"],)),
        "I'm sick and can't make my shift today")
    assert slots["intent"] == "callout" and reply is None


def test_llm_cannot_book_someone_who_said_no(monkeypatch):
    online(monkeypatch, lambda messages: '{"reply": "Great, booked!", "outcome": "accepted"}')
    facts = {"hospital": "H", "first_name": "J", "shift": "ICU day shift today", "hours": "7 AM to 7 PM", "unit": "ICU",
             "incentive_per_hour": 0, "overtime": False, "tier": 1, "voluntary_leave_ask": False, "needs_charge": False}
    out = call_agent.agent_turn(facts, [], "No, sorry, I can't today.")
    assert out["outcome"] == "pending"
