"""Hard constraints: can this person legally and safely work this shift?

Deterministic and explainable. Every failed check returns a plain-language
reason a supervisor can read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src import config, db


@dataclass
class Eligibility:
    staff_id: int
    eligible: bool = True
    on_shift: bool = False
    violations: list[str] = field(default_factory=list)
    on_discretionary_leave: bool = False
    hours_7d_after: float = 0.0
    overtime_hours: float = 0.0
    consecutive_after: int = 1

    def fail(self, reason: str) -> None:
        self.eligible = False
        self.violations.append(reason)


def shift_times(shift: dict) -> tuple[datetime, datetime]:
    return datetime.fromisoformat(shift["start"]), datetime.fromisoformat(shift["end"])


def shift_hours(shift: dict) -> float:
    start, end = shift_times(shift)
    return (end - start).total_seconds() / 3600


def working_shifts(staff_id: int, first: date, last: date) -> list[dict]:
    """Shifts this person is actively working with a start date in [first, last]."""
    return db.query(
        """SELECT s.* FROM assignments a JOIN shifts s ON s.id = a.shift_id
           WHERE a.staff_id=? AND a.status='scheduled' AND s.date BETWEEN ? AND ?
           ORDER BY s.start""",
        (staff_id, first.isoformat(), last.isoformat()),
    )


def leave_on(staff_id: int, day: date) -> dict | None:
    return db.one(
        """SELECT * FROM leave_requests
           WHERE staff_id=? AND status IN ('approved','logged') AND ? BETWEEN start_date AND end_date
           ORDER BY category='unplanned' DESC LIMIT 1""",
        (staff_id, day.isoformat()),
    )


def _hours_in(shifts: list[dict], first: date, last: date) -> float:
    return sum(shift_hours(s) for s in shifts if first.isoformat() <= s["date"] <= last.isoformat())


def check(staff: dict, shift: dict, *, needs_charge: bool = False, voluntary_leave_ask: bool = False) -> Eligibility:
    """Evaluate every hard rule for putting `staff` on `shift`."""
    result = Eligibility(staff_id=staff["id"])
    day = date.fromisoformat(shift["date"])
    start, end = shift_times(shift)
    unit = db.one("SELECT * FROM units WHERE id=?", (shift["unit_id"],))

    # Already on this shift (including having called out of it).
    mine = db.one("SELECT status FROM assignments WHERE shift_id=? AND staff_id=?", (shift["id"], staff["id"]))
    if mine:
        result.on_shift = True
        result.fail("called out of this shift" if mine["status"] == "called_out" else "already on this shift")
        return result

    # Leave: protected leave is never touched. Discretionary leave only for a voluntary ask.
    leave = leave_on(staff["id"], day)
    if leave:
        if leave["kind"] in config.PROTECTED_LEAVE:
            result.fail("on protected leave (never contacted)")
        elif voluntary_leave_ask:
            result.on_discretionary_leave = True
        else:
            result.on_discretionary_leave = True
            result.fail(f"on approved {leave['kind']} (last-resort voluntary ask only)")

    # Skills and unit competency.
    if unit and unit["requires_cert"] == "icu" and not staff["icu_certified"]:
        result.fail("not ICU-certified")
    if needs_charge and not staff["charge_certified"]:
        result.fail("shift needs a charge nurse; not charge-certified")

    # Nearby shifts: overlap and minimum rest.
    nearby = working_shifts(staff["id"], day - timedelta(days=8), day + timedelta(days=8))
    rest = timedelta(hours=config.MIN_REST_HOURS)
    for other in nearby:
        o_start, o_end = shift_times(other)
        if o_start < end and start < o_end:
            result.fail(f"already working {other['kind']} shift on {other['date']}")
        elif o_end <= start and start - o_end < rest:
            gap_h = (start - o_end).total_seconds() / 3600
            result.fail(f"only {gap_h:.0f}h rest after previous shift (min {config.MIN_REST_HOURS}h)")
        elif end <= o_start and o_start - end < rest:
            gap_h = (o_start - end).total_seconds() / 3600
            result.fail(f"only {gap_h:.0f}h rest before next shift (min {config.MIN_REST_HOURS}h)")

    # Consecutive days worked, counting the run through this date.
    worked = {date.fromisoformat(s["date"]) for s in nearby} | {day}
    run, d = 1, day - timedelta(days=1)
    while d in worked:
        run, d = run + 1, d - timedelta(days=1)
    d = day + timedelta(days=1)
    while d in worked:
        run, d = run + 1, d + timedelta(days=1)
    result.consecutive_after = run
    if run > config.MAX_CONSECUTIVE_DAYS:
        result.fail(f"would be {run} days in a row (max {config.MAX_CONSECUTIVE_DAYS})")

    # Weekly hours: worst rolling 7-day window that contains this shift.
    hours = shift_hours(shift)
    worst = max(
        _hours_in(nearby, day - timedelta(days=6 - k), day + timedelta(days=k)) + hours for k in range(7)
    )
    trailing = _hours_in(nearby, day - timedelta(days=6), day) + hours
    result.hours_7d_after = trailing
    result.overtime_hours = min(hours, max(0.0, trailing - config.OVERTIME_THRESHOLD_HOURS))
    if worst > config.WEEKLY_HOURS_CAP:
        result.fail(f"would reach {worst:.0f}h in 7 days (cap {config.WEEKLY_HOURS_CAP}h)")

    return result
