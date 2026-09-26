"""Optional real-phone path via Vapi (experimental: not exercised in the demo).

Point a Vapi assistant's server URL at POST /api/vapi/webhook and give it the
two tools in docs/vapi_assistant.json. Vapi handles speech; its model can be
Sciforium through Vapi's custom-LLM option. Tool calls land here and go to the
same rules engine the browser demo uses, so decisions are identical.
"""
from __future__ import annotations

import re
from datetime import date

from src import db
from src.agent import orchestrator, tools


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def staff_by_phone(number: str | None) -> dict | None:
    target = _digits(number)
    if not target:
        return None
    return next((s for s in db.query("SELECT * FROM staff") if _digits(s["phone"]) == target), None)


def handle_webhook(payload: dict) -> dict:
    message = payload.get("message", {})
    if message.get("type") != "tool-calls":
        return {}
    caller = staff_by_phone(((message.get("call") or {}).get("customer") or {}).get("number"))
    results = []
    for call in message.get("toolCallList", []):
        fn = call.get("function", {})
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            args = db.loads(args, {})
        results.append({"toolCallId": call.get("id"), "result": _run_tool(fn.get("name"), args, caller)})
    return {"results": results}


def _run_tool(name: str | None, args: dict, caller: dict | None) -> str:
    if caller is None:
        return "I couldn't match this phone number to a staff member. Please call the staffing office."
    if name == "report_callout":
        shifts = tools.resolve_callout_shifts(caller["id"], args.get("date"), args.get("date"), args.get("shift"))
        if not shifts:
            return "No scheduled shift found for that date. Ask which shift they mean."
        decision = tools.report_callout(caller["id"], [s["id"] for s in shifts], args.get("reason_category") or "unspecified", "phone")
        decision["kind"] = args.get("reason_category") or "unspecified"
        return orchestrator._template(decision, caller)
    if name == "request_time_off":
        start = date.fromisoformat(args["start_date"])
        end = date.fromisoformat(args.get("end_date") or args["start_date"])
        decision = tools.request_planned_leave(caller["id"], start, end, args.get("leave_type") or "personal", "phone")
        return orchestrator._template(decision, caller)
    return f"Unknown tool {name}."
