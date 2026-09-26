"""Actions the conversational agent can trigger. Each one calls the rules engine;
none of them lets the LLM make a decision."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src import clock, config, db, events
from src.optimization import eligibility, leave_policy
from src.optimization.staffing_solver import coverage, shift_label


def staff(staff_id: int) -> dict:
    row = db.one("SELECT * FROM staff WHERE id=?", (staff_id,))
    if not row:
        raise KeyError(f"staff {staff_id} not found")
    return row


def upcoming_shifts(staff_id: int, days: int = 21) -> list[dict]:
    now = clock.now()
    rows = eligibility.working_shifts(staff_id, now.date() - timedelta(days=1), now.date() + timedelta(days=days))
    return [s for s in rows if datetime.fromisoformat(s["end"]) > now]


def describe_shifts(shifts: list[dict]) -> str:
    if not shifts:
        return "none scheduled"
    return "; ".join(f"{s['date']} ({date.fromisoformat(s['date']).strftime('%A')}) {s['kind']} shift, {s['unit_id'].upper()}"
                     for s in shifts[:10])


def resolve_callout_shifts(staff_id: int, start: str | None, end: str | None, kind: str | None) -> list[dict]:
    """Which scheduled shifts is the caller calling out of?"""
    upcoming = upcoming_shifts(staff_id)
    if not start:
        soon = [s for s in upcoming if datetime.fromisoformat(s["start"]) - clock.now() < timedelta(hours=24)]
        return soon[:1]
    end = end or start
    matches = [s for s in upcoming if start <= s["date"] <= end]
    # "tonight" after a day shift, or a night shift that started yesterday
    if kind:
        matches = [s for s in matches if s["kind"] == kind] or matches
    return matches


def report_callout(staff_id: int, shift_ids: list[int], kind: str, channel: str = "voice") -> dict:
    decision = leave_policy.log_callout(staff_id, shift_ids, kind, channel)
    from src.agent import backfill  # local import: backfill depends on tools

    for gap in decision["gaps"]:
        shift = db.one("SELECT * FROM shifts WHERE id=?", (gap["shift_id"],))
        if datetime.fromisoformat(shift["start"]) - clock.now() < timedelta(hours=config.BACKFILL_AUTOSTART_HOURS):
            backfill.schedule(gap["gap_id"])
    return decision


def request_planned_leave(staff_id: int, start: date, end: date, kind: str, channel: str = "voice") -> dict:
    kind = kind if kind in config.DISCRETIONARY_LEAVE else "personal"
    decision = leave_policy.evaluate_planned(staff_id, start, end, kind)
    decision["leave_id"] = leave_policy.apply_planned(staff_id, start, end, kind, decision, channel)
    return decision


def accept_alternative(leave_id: int, index: int) -> dict:
    req = db.one("SELECT * FROM leave_requests WHERE id=?", (leave_id,))
    decision = db.loads(req["decision"], {})
    alt = decision["alternatives"][index]
    db.execute("UPDATE leave_requests SET status='withdrawn', decided_at=? WHERE id=?", (clock.stamp(), leave_id))
    return request_planned_leave(req["staff_id"], date.fromisoformat(alt["start"]), date.fromisoformat(alt["end"]),
                                 req["kind"], channel="dashboard")


def fill_gap(gap_id: int, staff_id: int, tier: int, incentive: float) -> tuple[bool, str]:
    """Put an accepting nurse on the shift, after re-checking every rule."""
    gap = db.one("SELECT * FROM gaps WHERE id=?", (gap_id,))
    if gap["status"] == "filled":
        return False, "already filled"
    shift = db.one("SELECT * FROM shifts WHERE id=?", (gap["shift_id"],))
    person = staff(staff_id)
    check = eligibility.check(person, shift, needs_charge=bool(gap["needs_charge"]), voluntary_leave_ask=tier == 4)
    if not check.eligible:
        return False, "; ".join(check.violations)

    role = "charge" if gap["needs_charge"] else "rn"
    db.execute(
        """INSERT INTO assignments (shift_id, staff_id, status, role, source, tier, incentive, created_at, note)
           VALUES (?, ?, 'scheduled', ?, 'backfill', ?, ?, ?, ?)""",
        (shift["id"], staff_id, role, tier, incentive, clock.stamp(), f"backfill via ShiftVoice ({config.TIERS[tier]})"),
    )
    db.execute("UPDATE gaps SET status='filled', filled_at=?, filled_by=? WHERE id=?", (clock.stamp(), staff_id, gap_id))

    cov = coverage(shift)
    vacated = staff(gap["vacated_staff_id"]) if gap["vacated_staff_id"] else None
    charge = next((r["name"] for r in cov["roster"] if r["role"] == "charge" and r["status"] == "scheduled"), "Charge nurse")
    label = shift_label(shift)
    events.log("gap.filled", f"{person['name']} confirmed for {label}. Coverage {cov['staffed']}/{cov['required']}.",
               {"gap_id": gap_id, "shift_id": shift["id"], "staff_id": staff_id})
    leave_policy.notify(
        f"{charge} (charge nurse)", "coverage",
        f"{person['name']} is confirmed for {label}"
        + (f", replacing {vacated['name']}" if vacated else "")
        + f". You're back to {cov['staffed']}/{cov['required']} RNs. No action needed.",
    )
    return True, "filled"
