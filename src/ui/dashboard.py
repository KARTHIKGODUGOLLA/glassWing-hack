"""View model for the live dashboard: one JSON snapshot of everything on screen."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src import clock, config, db
from src.agent import llm
from src.optimization import cost_model
from src.optimization.staffing_solver import coverage, shift_label
from src.voice import session


def _day_phrase(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%A, %B ") + str(d.day)


def demo_scripts(demo: dict) -> list[dict]:
    thin, alts = demo["thin_day"], demo["alt_days"]
    far = date.fromisoformat(demo["date"]) + timedelta(days=30)
    return [
        {"caller": demo["callout_staff_id"], "label": "Sick call",
         "text": "Hi, it's Maria. I woke up with a fever and I can't make my shift today."},
        {"caller": demo["leave_staff_id"], "label": "Thin-day leave",
         "text": f"Hi, it's Devon. Could I take a personal day on {_day_phrase(thin)}?"},
        {"caller": demo["leave_staff_id"], "label": "Take alternate",
         "text": f"{_day_phrase(alts[0])} works for me."},
        {"caller": demo["leave_staff_id"], "label": "Safe day",
         "text": f"I'd like to take {_day_phrase(alts[1])} off as a personal day."},
        {"caller": demo["leave_staff_id"], "label": "Long vacation",
         "text": f"I'd like vacation from {_day_phrase(far.isoformat())} through {_day_phrase((far + timedelta(days=7)).isoformat())}."},
        {"caller": demo["leave_staff_id"], "label": "Holiday",
         "text": "Can I take Thanksgiving off?"},
    ]


def _roster(cov: dict) -> list[dict]:
    return [{
        "name": r["name"], "role": r["role"], "status": r["status"], "source": r["source"],
        "tier": r["tier"], "note": r["note"], "pool": r["pool"],
    } for r in cov["roster"]]


def _gap_view(gap: dict) -> dict:
    shift = db.one("SELECT * FROM shifts WHERE id=?", (gap["shift_id"],))
    vacated = db.one("SELECT name FROM staff WHERE id=?", (gap["vacated_staff_id"],)) if gap["vacated_staff_id"] else None
    outreach = db.query(
        """SELECT o.*, st.name, st.pool FROM outreach o JOIN staff st ON st.id=o.staff_id
           WHERE o.gap_id=? ORDER BY o.rank""", (gap["id"],))
    filled_by = db.one("SELECT name FROM staff WHERE id=?", (gap["filled_by"],)) if gap["filled_by"] else None
    hours = 12
    accepted = next((o for o in outreach if o["status"] == "accepted"), None)
    ttf = None
    if gap["filled_at"]:
        ttf = int((datetime.fromisoformat(gap["filled_at"]) - datetime.fromisoformat(gap["created_at"])).total_seconds())
    return {
        "id": gap["id"],
        "shift_id": gap["shift_id"],
        "label": shift_label(shift),
        "status": gap["status"],
        "needs_charge": bool(gap["needs_charge"]),
        "vacated": vacated["name"] if vacated else None,
        "created_at": gap["created_at"],
        "filled_at": gap["filled_at"],
        "filled_by": filled_by["name"] if filled_by else None,
        "time_to_fill_s": ttf,
        "calls_made": sum(1 for o in outreach if o["status"] in ("declined", "accepted", "no_answer", "not_needed")),
        "fill_cost": accepted["est_cost"] if accepted else None,
        "agency_cost": cost_model.agency_baseline(hours),
        "candidates": [{
            "rank": o["rank"], "name": o["name"], "tier": o["tier"], "tier_label": config.TIERS[o["tier"]],
            "status": o["status"], "reasons": db.loads(o["reasons"], []), "est_cost": o["est_cost"],
            "incentive": o["incentive"], "overtime": bool(o["overtime"]), "call_id": o["call_id"],
        } for o in outreach],
        "excluded": db.loads(db.get_meta(f"excluded:{gap['id']}"), []),
    }


def snapshot() -> dict:
    demo = db.loads(db.get_meta("demo"), {})
    now = clock.now()
    today = now.date()

    today_cov = []
    for s in db.query("SELECT * FROM shifts WHERE date=? ORDER BY unit_id='medsurg', kind", (today.isoformat(),)):
        cov = coverage(s)
        today_cov.append({**{k: v for k, v in cov.items() if k != "roster"}, "label": shift_label(s), "roster": _roster(cov),
                          "open_gap": bool(db.one("SELECT 1 FROM gaps WHERE shift_id=? AND status<>'filled'", (s["id"],)))})

    gaps = [_gap_view(g) for g in db.query("SELECT * FROM gaps ORDER BY id DESC LIMIT 10")]
    calls = []
    for c in db.query("SELECT c.*, st.name FROM calls c JOIN staff st ON st.id=c.staff_id ORDER BY c.id DESC LIMIT 12"):
        calls.append({"id": c["id"], "direction": c["direction"], "name": c["name"], "status": c["status"],
                      "purpose": c["purpose"], "started_at": c["started_at"], "transcript": db.loads(c["transcript"], []),
                      "stage": db.loads(c["state"], {}).get("stage")})

    leave = []
    for r in db.query("""SELECT l.*, st.name FROM leave_requests l JOIN staff st ON st.id=l.staff_id
                         ORDER BY l.requested_at DESC, l.id DESC LIMIT 25"""):
        leave.append({**{k: r[k] for k in ("id", "name", "staff_id", "kind", "category", "start_date", "end_date",
                                           "status", "requested_at", "channel", "decided_by", "decided_at")},
                      "decision": db.loads(r["decision"], {})})

    filled = [g for g in gaps if g["status"] == "filled"]
    calls_out = db.one("SELECT COUNT(*) n FROM calls WHERE direction='outbound'")["n"]
    callouts = db.one("SELECT COUNT(*) n FROM leave_requests WHERE category='unplanned' AND requested_at>=?", (today.isoformat(),))["n"]
    saved = sum(g["agency_cost"] - (g["fill_cost"] or 0) for g in filled if g["fill_cost"] is not None)

    return {
        "now": now.isoformat(timespec="seconds"),
        "hospital": config.HOSPITAL_NAME,
        "demo": demo,
        "llm": llm.status(),
        "telephony": {"vapi": bool(config.VAPI_API_KEY and config.VAPI_PHONE_NUMBER_ID)},
        "settings": session.SETTINGS,
        "today": today_cov,
        "gaps": gaps,
        "calls": calls,
        "leave": leave,
        "supervisor_queue": [l for l in leave if l["status"] == "pending_supervisor"],
        "notifications": db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 20"),
        "events": db.query("SELECT id, ts, kind, message FROM events ORDER BY id DESC LIMIT 80"),
        "staff": db.query("""SELECT id, name, pool, home_unit, icu_certified, charge_certified FROM staff
                             WHERE pool<>'agency' ORDER BY id=? DESC, id=? DESC, name""",
                          (demo.get("callout_staff_id", 0), demo.get("leave_staff_id", 0))),
        "scripts": demo_scripts(demo) if demo else [],
        "metrics": {
            "callouts_today": callouts,
            "gaps_filled": len(filled),
            "gaps_open": sum(1 for g in gaps if g["status"] != "filled"),
            "outbound_calls": calls_out,
            "avg_time_to_fill_s": round(sum(g["time_to_fill_s"] for g in filled) / len(filled)) if filled else None,
            # A charge nurse spends ~6 min per phone attempt and ~5 min logging a call-out.
            "charge_nurse_minutes_saved": calls_out * 6 + callouts * 5,
            "agency_spend_avoided": saved,
        },
    }


def schedule_grid(days: int = 28) -> dict:
    start = clock.today()
    end = start + timedelta(days=days - 1)
    blackouts = config.blackout_dates(start)
    rows: dict[tuple[str, str], list[dict]] = {}
    for s in db.query("SELECT * FROM shifts WHERE date BETWEEN ? AND ? ORDER BY date", (start.isoformat(), end.isoformat())):
        cov = coverage(s)
        rows.setdefault((cov["unit"], s["kind"]), []).append({
            "shift_id": s["id"], "date": s["date"], "required": cov["required"], "staffed": cov["staffed"],
            "status": cov["status"], "census": s["census"], "blackout": blackouts.get(date.fromisoformat(s["date"])),
        })
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    return {"dates": dates, "rows": [{"unit": u, "kind": k, "cells": cells} for (u, k), cells in rows.items()]}


def shift_detail(shift_id: int) -> dict:
    s = db.one("SELECT * FROM shifts WHERE id=?", (shift_id,))
    cov = coverage(s)
    return {**{k: v for k, v in cov.items() if k != "roster"}, "label": shift_label(s), "roster": _roster(cov)}
