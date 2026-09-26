"""Inbound calls: a nurse phones the staffing line.

Division of labor on every turn:
1. The LLM (or the offline extractor) turns speech into structured slots.
2. Deterministic code decides whether enough is known and calls the rules engine.
3. The LLM (or a template) explains the engine's decision in plain language.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta

from src import clock, config, db, events
from src.agent import extract, llm, prompts, tools
from src.optimization import leave_policy
from src.voice import session

KIND_PHRASE = {
    "sick": "sick", "emergency": "for a family emergency", "bereavement": "on bereavement leave",
    "unspecified": "",
}


def _first(staff: dict) -> str:
    return staff["name"].split()[0]


def _fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%A, %B ") + str(d.day)


def _fmt_range(start: str, end: str) -> str:
    return _fmt_day(start) if start == end else f"{_fmt_day(start)} through {_fmt_day(end)}"


def greeting(staff: dict) -> str:
    return (f"ShiftVoice staffing line for {config.HOSPITAL_NAME}. Hi {_first(staff)}, I've got you from your number on file. "
            "Are you calling out of a shift, or asking for time off?")


async def start_inbound(staff_id: int, channel: str = "voice") -> int:
    staff = tools.staff(staff_id)
    call_id = session.create("inbound", staff_id, "staff line")
    session.save_state(call_id, {"stage": "open", "slots": {}, "channel": channel})
    events.log("call.inbound", f"Incoming call from {staff['name']}", {"call_id": call_id, "staff_id": staff_id})
    session.add_line(call_id, "agent", greeting(staff))
    return call_id


# --- Understanding -----------------------------------------------------------------

def _valid_iso(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return None
    today = clock.today()
    return d.isoformat() if today - timedelta(days=1) <= d <= today + timedelta(days=400) else None


def understand(call: dict, staff: dict, text: str) -> tuple[dict, str | None]:
    """Returns (updated slots, suggested reply)."""
    slots = dict(call["state"].get("slots", {}))
    if llm.available():
        today = clock.today()
        calendar = ", ".join(f"{(today + timedelta(days=i)).strftime('%a')} {(today + timedelta(days=i)).isoformat()}"
                             for i in range(0, 45))
        offer = ""
        if call["state"].get("alternatives"):
            offer = "You already offered these alternative dates: " + "; ".join(
                f"{a['start']} to {a['end']}" for a in call["state"]["alternatives"])
        system = prompts.INBOUND_SYSTEM.format(hospital=config.HOSPITAL_NAME, rules=prompts.GROUND_RULES)
        context = prompts.INBOUND_CONTEXT.format(
            now=clock.now().strftime("%A %Y-%m-%d %H:%M"),
            name=staff["name"], role=f"{staff['pool'].replace('_', ' ')} RN, {staff['home_unit'] or 'float'}",
            shifts=tools.describe_shifts(tools.upcoming_shifts(staff["id"])),
            calendar=calendar, slots=db.dumps(slots), offer=offer,
            transcript="\n".join(f"{l['name']}: {l['text']}" for l in call["transcript"]),
        )
        out = llm.chat_json(system, context)
        if out:
            for key in ("intent", "leave_type", "shift"):
                if out.get(key):
                    slots[key] = out[key]
            for key in ("start_date", "end_date"):
                if _valid_iso(out.get(key)):
                    slots[key] = _valid_iso(out[key])
            if slots.get("start_date") and not slots.get("end_date"):
                slots["end_date"] = slots["start_date"]
            reply = out.get("reply") if isinstance(out.get("reply"), str) else None
            return slots, reply
    slots = extract.extract_inbound(text, clock.today(), slots)
    return slots, None


# --- Explaining ---------------------------------------------------------------------

def _template(decision: dict, staff: dict) -> str:
    first = _first(staff)
    outcome = decision["outcome"]
    if outcome == "logged":
        kind = decision.get("kind", "unspecified")
        phrase = KIND_PHRASE.get(kind, "")
        shifts = " and ".join(s.get("spoken", s["label"]) for s in decision["shifts"])
        sorry = " I'm so sorry for your loss." if kind == "bereavement" else ""
        head = f"Thanks for letting me know, {first}.{sorry} You're logged out{(' ' + phrase) if phrase else ''} for your {shifts}."
        if decision["gaps"]:
            return f"{head} You don't need to do anything else; I'm finding coverage now." + (" Feel better." if kind == "sick" else "")
        return f"{head} The unit is still covered, so you're all set." + (" Feel better." if kind == "sick" else "")
    dates = _fmt_range(decision["start"], decision["end"])
    if outcome == "approved":
        return f"Good news, {first}: your {decision['kind']} time on {dates} is approved and on the schedule."
    if outcome == "pending_supervisor":
        spoken = {
            "Notice": f"it's less than {config.PLANNED_LEAVE_MIN_NOTICE_DAYS} days' notice",
            "Length": f"it's longer than {config.PLANNED_LEAVE_MAX_AUTO_DAYS} days",
            "Coverage and skill mix": "the unit would be at or below its minimum on some shifts",
        }
        why = " and ".join(spoken[c["rule"]] for c in decision["checks"] if c["result"] != "pass" and c["rule"] in spoken)
        return (f"I've sent your request for {dates} to your nurse manager, because {why or 'it needs a review'}. "
                "You'll hear back from them.")
    alts = decision.get("alternatives", [])
    blackout = next((c for c in decision["checks"] if c["rule"] == "Blackout dates" and c["result"] == "fail"), None)
    reason = (f"{dates} falls on the {blackout['detail']} blackout" if blackout
              else f"{dates} is too thin to approve automatically; the unit would drop below its minimum")
    offer = ""
    if alts:
        names = [a["label"] for a in alts[:2]]
        offer = f" I can approve {' or '.join(names)} right now."
    swaps = decision.get("swap_partners", [])
    if swaps:
        offer += f" Or {swaps[0]['name'].split()[0]} is free that day if you want to arrange a swap."
    if not offer:
        return f"{reason}, and I couldn't find nearby dates I can approve automatically. Want me to send it to your nurse manager as an exception?"
    return f"{reason}.{offer} Would one of those work? I can also send the original date to your manager as an exception."


def explain(decision: dict, staff: dict) -> str:
    template = _template(decision, staff)
    if not llm.available():
        return template
    facts = {k: v for k, v in decision.items() if k not in ("affected_shifts",)}
    text = llm.chat([
        {"role": "system", "content": prompts.EXPLAIN_SYSTEM.format(rules=prompts.GROUND_RULES)},
        {"role": "user", "content": f"Nurse: {staff['name']}\nDecision (authoritative): {db.dumps(facts)}\n"
                                    f"Reference phrasing: {template}"},
    ], temperature=0.3, max_tokens=160)
    return text.strip().strip('"') if text else template


# --- Turn handling ------------------------------------------------------------------

def _missing_prompt(slots: dict) -> str:
    if slots.get("intent") == "planned_leave":
        return "Sure. What date or dates would you like off?"
    if slots.get("intent") == "callout":
        return "Okay. Which shift are you calling out of?"
    return "Are you calling out of a shift, or asking for time off?"


async def handle(call_id: int, text: str) -> None:
    call = session.get(call_id)
    if call["status"] != "active":
        return
    staff = tools.staff(call["staff_id"])
    session.add_line(call_id, "staff", text)
    call = session.get(call_id)
    state = call["state"]

    wants_exception = re.search(r"\b(manager|supervisor|exception|escalate)\b", text.lower()) or (
        not state.get("alternatives") and re.search(r"\b(yes|yeah|sure|please|ok(ay)?)\b", text.lower()))
    if state.get("stage") == "offered" and wants_exception:
        leave_id = state["leave_id"]
        db.execute("UPDATE leave_requests SET status='pending_supervisor' WHERE id=?", (leave_id,))
        req = db.one("SELECT * FROM leave_requests WHERE id=?", (leave_id,))
        leave_policy.notify("Nurse Manager", "approval",
                            f"{staff['name']} asks for an exception: {req['kind']} {req['start_date']} to {req['end_date']} (coverage too thin to auto-approve).")
        events.log("leave.escalated", f"{staff['name']} sent request to the nurse manager as an exception", {"leave_id": leave_id})
        await _finish(call_id, "Done. I've sent it to your nurse manager as an exception request. You'll hear back from them.")
        return

    slots, llm_reply = await asyncio.to_thread(understand, call, staff, text)
    state["slots"] = slots
    intent = slots.get("intent")

    if intent == "callout":
        shifts = tools.resolve_callout_shifts(staff["id"], slots.get("start_date"), slots.get("end_date"), slots.get("shift"))
        if shifts:
            kind = slots.get("leave_type") if slots.get("leave_type") in config.PROTECTED_LEAVE else "unspecified"
            decision = await asyncio.to_thread(tools.report_callout, staff["id"], [s["id"] for s in shifts], kind, state.get("channel", "voice"))
            decision["kind"] = kind
            reply = await asyncio.to_thread(explain, decision, staff)
            session.save_state(call_id, {**state, "stage": "done"})
            await _finish(call_id, reply)
            return
        if slots.get("start_date"):
            reply = f"I don't see you on the schedule for {_fmt_day(slots['start_date'])}. Which shift did you mean?"
            session.save_state(call_id, state)
            session.add_line(call_id, "agent", reply)
            return

    if intent == "planned_leave" and slots.get("start_date"):
        start = date.fromisoformat(slots["start_date"])
        end = date.fromisoformat(slots.get("end_date") or slots["start_date"])
        if end < start:
            start, end = end, start
        kind = slots.get("leave_type") if slots.get("leave_type") in config.DISCRETIONARY_LEAVE else "personal"
        decision = await asyncio.to_thread(tools.request_planned_leave, staff["id"], start, end, kind, state.get("channel", "voice"))
        reply = await asyncio.to_thread(explain, decision, staff)
        if decision["outcome"] == "alternatives_offered":
            session.save_state(call_id, {"stage": "offered", "slots": {"intent": "planned_leave", "leave_type": kind},
                                         "leave_id": decision["leave_id"], "alternatives": decision["alternatives"],
                                         "channel": state.get("channel", "voice")})
            session.add_line(call_id, "agent", reply)
            return
        if state.get("leave_id"):  # took an alternative: retire the original request
            db.execute("UPDATE leave_requests SET status='withdrawn' WHERE id=? AND status='alternatives_offered'", (state["leave_id"],))
        session.save_state(call_id, {**state, "stage": "done"})
        await _finish(call_id, reply)
        return

    if state.get("stage") == "offered" and re.search(r"\b(no|nah|that's ok|never ?mind|forget it)\b", text.lower()):
        await _finish(call_id, "No problem. The alternatives stay on file if you change your mind. Take care.")
        return

    session.save_state(call_id, state)
    session.add_line(call_id, "agent", llm_reply or _missing_prompt(slots))


async def _finish(call_id: int, reply: str) -> None:
    session.add_line(call_id, "agent", reply)
    session.end(call_id, "completed")


async def hang_up(call_id: int) -> None:
    session.end(call_id, "caller hung up")
