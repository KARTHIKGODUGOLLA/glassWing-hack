"""Outbound shift-offer calls: the agent phones a ranked candidate."""
from __future__ import annotations

import asyncio

from src import config, db, events
from src.agent import extract, llm, prompts, tools
from src.optimization.staffing_solver import coverage, spoken_label
from src.voice import session, simulated_nurse


def offer_facts(outreach: dict) -> dict:
    gap = db.one("SELECT * FROM gaps WHERE id=?", (outreach["gap_id"],))
    shift = db.one("SELECT * FROM shifts WHERE id=?", (gap["shift_id"],))
    staff = tools.staff(outreach["staff_id"])
    cov = coverage(shift)
    return {
        "hospital": config.HOSPITAL_NAME,
        "first_name": staff["name"].split()[0],
        "shift": spoken_label(shift),
        "hours": "7 AM to 7 PM" if shift["kind"] == "day" else "7 PM to 7 AM",
        "unit": cov["unit"],
        "incentive_per_hour": outreach["incentive"] or 0,
        "overtime": bool(outreach["overtime"]),
        "tier": outreach["tier"],
        "voluntary_leave_ask": outreach["tier"] == 4,
        "needs_charge": bool(gap["needs_charge"]),
    }


def opening_line(f: dict) -> str:
    pay = (f" There's a ${f['incentive_per_hour']:.0f}-an-hour incentive on it." if f["incentive_per_hour"] else "")
    if f["voluntary_leave_ask"]:
        return (f"Hi {f['first_name']}, it's ShiftVoice at {f['hospital']}. I know you're on approved time off, and saying no "
                f"is completely fine. Your leave isn't affected either way. We're short on the {f['shift']}.{pay} "
                "Would you want to pick it up?")
    return f"Hi {f['first_name']}, it's ShiftVoice at {f['hospital']}. We have an open {f['shift']}.{pay} Could you pick it up?"


def _fallback_reply(f: dict, heard: str) -> dict:
    kind = extract.classify_offer_response(heard)
    if kind == "accepted":
        return {"reply": "", "outcome": "accepted"}
    if kind == "declined":
        msg = "Totally understood. Enjoy your time off." if f["voluntary_leave_ask"] else "No problem at all, thanks for picking up. Have a good one."
        return {"reply": msg, "outcome": "declined"}
    t = heard.lower()
    if any(w in t for w in ("incentive", "pay", "rate", "money", "bonus")):
        ans = (f"Yes, it's your regular rate plus ${f['incentive_per_hour']:.0f} an hour." if f["incentive_per_hour"]
               else "It's your regular rate.")
    elif any(w in t for w in ("time", "when", "hours", "start")):
        ans = f"It's {f['hours']} on {f['unit']}."
    else:
        ans = "Sorry, I didn't catch that."
    return {"reply": f"{ans} Would you be able to take it?", "outcome": "pending"}


def agent_turn(f: dict, transcript: list[dict], heard: str) -> dict:
    """Decide what to say next. The LLM phrases; a keyword check guards the outcome."""
    fallback = _fallback_reply(f, heard)
    if not llm.available():
        return fallback
    voluntary = ("This person is on approved leave. Make it explicit that saying no is completely fine and their leave is not affected."
                 if f["voluntary_leave_ask"] else "Be brief and friendly.")
    system = prompts.OUTBOUND_SYSTEM.format(hospital=f["hospital"], rules=prompts.GROUND_RULES, voluntary=voluntary,
                                            facts=db.dumps({k: v for k, v in f.items() if k != "hospital"}))
    convo = "\n".join(f"{l['name']}: {l['text']}" for l in transcript)
    out = llm.chat_json(system, convo)
    if not out or out.get("outcome") not in ("accepted", "declined", "pending") or not isinstance(out.get("reply"), str):
        return fallback
    # Never book someone on an ambiguous yes: if the keyword check hears a no, confirm first.
    if out["outcome"] == "accepted" and fallback["outcome"] == "declined":
        return {"reply": "Just to be sure, is that a yes, you can take the shift?", "outcome": "pending"}
    if out["outcome"] == "accepted":
        out["reply"] = ""  # the confirmation line is generated from the booking result
    return out


async def _hear(call_id: int, staff: dict, turn: int) -> str | None:
    if session.SETTINGS["candidate_mode"] == "live":
        try:
            return await asyncio.wait_for(session.inbox(call_id).get(), timeout=config.LIVE_REPLY_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None
    await session.pause(1.0)
    return await simulated_nurse.respond(staff, turn, session.get(call_id)["transcript"])


async def run_offer_call(outreach: dict) -> str:
    """Returns accepted | declined | no_answer | not_needed."""
    staff = tools.staff(outreach["staff_id"])
    f = offer_facts(outreach)
    call_id = session.create("outbound", staff["id"], "shift offer", outreach["id"])
    db.execute("UPDATE outreach SET call_id=? WHERE id=?", (call_id, outreach["id"]))
    events.log("call.outbound", f"Calling {staff['name']} ({config.TIERS[outreach['tier']]})",
               {"call_id": call_id, "staff_id": staff["id"], "gap_id": outreach["gap_id"]})
    if session.SETTINGS["candidate_mode"] == "live":
        session.inbox(call_id)
    await session.pause(1.5)  # ringing

    outcome = "no_answer"
    await session.say(call_id, "agent", opening_line(f))
    for turn in range(config.MAX_OUTBOUND_TURNS):
        heard = await _hear(call_id, staff, turn)
        if not heard:
            break
        await session.say(call_id, "staff", heard)
        result = await asyncio.to_thread(agent_turn, f, session.get(call_id)["transcript"], heard)
        if result["outcome"] == "accepted":
            ok, why = tools.fill_gap(outreach["gap_id"], staff["id"], outreach["tier"], outreach["incentive"] or 0)
            if ok:
                cov = coverage(db.one("SELECT s.* FROM shifts s JOIN gaps g ON g.shift_id=s.id WHERE g.id=?", (outreach["gap_id"],)))
                charge = next((r["name"].split()[0] for r in cov["roster"] if r["role"] == "charge" and r["status"] == "scheduled"), "the charge nurse")
                await session.say(call_id, "agent", f"Perfect, you're confirmed for {f['shift']}. I've let {charge} know. Thank you, {f['first_name']}!")
                outcome = "accepted"
            else:
                await session.say(call_id, "agent", "Thank you! It looks like that shift was just covered, so you're all set. Sorry for the early call.")
                outcome = "not_needed"
            break
        await session.say(call_id, "agent", result["reply"])
        if result["outcome"] == "declined":
            outcome = "declined"
            break
    session.end(call_id, outcome)
    return outcome
