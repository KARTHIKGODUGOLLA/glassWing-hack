"""Leave decisions. Two different flows, both deterministic:

* Unplanned call-outs (sick, emergency, bereavement) are notifications, not
  requests. They are always logged and accepted, the reason is never judged,
  and any coverage hole becomes a gap for the backfill loop.
* Planned leave (vacation, personal) is checked against notice, length,
  blackout dates and coverage. The outcome is approve, send to the supervisor,
  or offer alternate dates / swap partners. The engine never issues a flat no.
"""
from __future__ import annotations

from datetime import date, timedelta

from src import clock, config, db, events
from src.optimization import eligibility
from src.optimization.staffing_solver import coverage, shift_label, spoken_label


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def scheduled_shifts(staff_id: int, start: date, end: date) -> list[dict]:
    return eligibility.working_shifts(staff_id, start, end)


# --- Unplanned -----------------------------------------------------------------

def log_callout(staff_id: int, shift_ids: list[int], kind: str, channel: str = "voice") -> dict:
    """Record a call-out. Always accepted. Returns the created gaps."""
    kind = kind if kind in config.PROTECTED_LEAVE else "unspecified"
    staff = db.one("SELECT * FROM staff WHERE id=?", (staff_id,))
    shifts = [db.one("SELECT * FROM shifts WHERE id=?", (sid,)) for sid in shift_ids]
    shifts = sorted([s for s in shifts if s], key=lambda s: s["start"])
    if not shifts:
        raise ValueError("no shifts to call out of")

    decision = {
        "outcome": "logged",
        "policy": "Unplanned leave is protected: always accepted, reason not evaluated.",
        "shifts": [],
        "gaps": [],
    }
    leave_id = db.execute(
        """INSERT INTO leave_requests (staff_id, kind, category, start_date, end_date, status, requested_at, channel, decided_by, decided_at)
           VALUES (?, ?, 'unplanned', ?, ?, 'logged', ?, ?, 'policy', ?)""",
        (staff_id, kind, shifts[0]["date"], shifts[-1]["date"], clock.stamp(), channel, clock.stamp()),
    )
    for shift in shifts:
        db.execute(
            "UPDATE assignments SET status='called_out', note=? WHERE shift_id=? AND staff_id=?",
            (f"{kind} call-out {config.fmt_time(clock.now())}", shift["id"], staff_id),
        )
        cov = coverage(shift)
        label = shift_label(shift)
        decision["shifts"].append({"shift_id": shift["id"], "label": label, "spoken": spoken_label(shift),
                                   "required": cov["required"], "staffed": cov["staffed"]})
        if cov["status"] == "short":
            gap_id = db.execute(
                """INSERT INTO gaps (shift_id, leave_id, vacated_staff_id, needs_charge, status, created_at)
                   VALUES (?, ?, ?, ?, 'open', ?)""",
                (shift["id"], leave_id, staff_id, 0 if cov["has_charge"] else 1, clock.stamp()),
            )
            short_by = max(1, -cov["margin"])
            decision["gaps"].append({"gap_id": gap_id, "shift_id": shift["id"], "label": label, "short_by": short_by,
                                     "needs_charge": not cov["has_charge"]})
            events.log("gap.opened", f"{label}: {cov['staffed']}/{cov['required']} RNs, {short_by} short",
                       {"gap_id": gap_id, "shift_id": shift["id"]})
        else:
            events.log("coverage.ok", f"{label}: still at {cov['staffed']}/{cov['required']} RNs, no backfill needed",
                       {"shift_id": shift["id"]})

    db.execute("UPDATE leave_requests SET decision=? WHERE id=?", (db.dumps(decision), leave_id))
    events.log("callout.logged", f"{staff['name']} called out ({kind}) for {len(shifts)} shift(s). Accepted and logged.",
               {"leave_id": leave_id, "staff_id": staff_id})
    decision["leave_id"] = leave_id
    return decision


# --- Planned ---------------------------------------------------------------------

def _impact(staff_id: int, shift: dict) -> dict:
    cov = coverage(shift, without_staff=staff_id)
    if cov["status"] == "short":
        verdict = "thin"
    elif cov["margin"] == 0:
        verdict = "borderline"
    else:
        verdict = "safe"
    return {
        "shift_id": shift["id"],
        "label": shift_label(shift),
        "required": cov["required"],
        "staffed_after": cov["staffed"],
        "margin_after": cov["margin"],
        "charge_after": cov["has_charge"],
        "verdict": verdict,
    }


def evaluate_planned(staff_id: int, start: date, end: date, kind: str, requested_on: date | None = None,
                     *, search_alternatives: bool = True) -> dict:
    """Run every planned-leave rule. Pure: changes nothing."""
    requested_on = requested_on or clock.today()
    checks: list[dict] = []
    days = (end - start).days + 1
    notice = (start - requested_on).days
    blackouts = config.blackout_dates(start)
    hit_blackout = sorted({name for d in _daterange(start, end) if (name := blackouts.get(d))})

    checks.append({
        "rule": "Notice",
        "result": "pass" if notice >= config.PLANNED_LEAVE_MIN_NOTICE_DAYS else "warn",
        "detail": f"{notice} days' notice (policy: {config.PLANNED_LEAVE_MIN_NOTICE_DAYS})",
    })
    checks.append({
        "rule": "Length",
        "result": "pass" if days <= config.PLANNED_LEAVE_MAX_AUTO_DAYS else "warn",
        "detail": f"{days} day(s) (auto-approve up to {config.PLANNED_LEAVE_MAX_AUTO_DAYS})",
    })
    checks.append({
        "rule": "Blackout dates",
        "result": "fail" if hit_blackout else "pass",
        "detail": ", ".join(hit_blackout) if hit_blackout else "none in range",
    })

    impacts = [_impact(staff_id, s) for s in scheduled_shifts(staff_id, start, end)]
    thin = [i for i in impacts if i["verdict"] == "thin"]
    borderline = [i for i in impacts if i["verdict"] == "borderline"]
    if not impacts:
        cov_detail, cov_result = "not scheduled in this range", "pass"
    elif thin:
        cov_detail = "; ".join(
            f"{i['label']}: {i['staffed_after']}/{i['required']}" + ("" if i["charge_after"] else ", no charge nurse")
            for i in thin)
        cov_result = "fail"
    elif borderline:
        cov_detail = "; ".join(f"{i['label']}: exactly at minimum ({i['staffed_after']}/{i['required']})" for i in borderline)
        cov_result = "warn"
    else:
        cov_detail, cov_result = f"{len(impacts)} shift(s) stay above minimum", "pass"
    checks.append({"rule": "Coverage and skill mix", "result": cov_result, "detail": cov_detail})

    decision = {
        "kind": kind,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "checks": checks,
        "affected_shifts": impacts,
        "alternatives": [],
        "swap_partners": [],
    }
    too_long = days > config.PLANNED_LEAVE_MAX_AUTO_DAYS
    if too_long and not hit_blackout:
        # Long leave always goes to a person; the coverage impact rides along for them to plan backfill.
        decision["outcome"] = "pending_supervisor"
    elif any(c["result"] == "fail" for c in checks):
        decision["outcome"] = "alternatives_offered"
        if search_alternatives:
            decision["alternatives"] = find_alternatives(staff_id, start, end, kind, requested_on)
            decision["swap_partners"] = find_swap_partners(staff_id, [i["shift_id"] for i in thin])
    elif any(c["result"] == "warn" for c in checks):
        decision["outcome"] = "pending_supervisor"
    else:
        decision["outcome"] = "approved"
    decision["summary"] = [f"{c['rule']}: {c['detail']}" for c in checks if c["result"] != "pass"]
    return decision


def find_alternatives(staff_id: int, start: date, end: date, kind: str, requested_on: date, limit: int = 3) -> list[dict]:
    """Nearest windows of the same length that would be auto-approved."""
    length = (end - start).days
    earliest = requested_on + timedelta(days=config.PLANNED_LEAVE_MIN_NOTICE_DAYS)
    found: list[dict] = []
    for step in range(1, 29):
        for offset in (step, -step):
            s = start + timedelta(days=offset)
            e = s + timedelta(days=length)
            if s < earliest or not scheduled_shifts(staff_id, s, e):
                continue
            check = evaluate_planned(staff_id, s, e, kind, requested_on, search_alternatives=False)
            if check["outcome"] == "approved":
                label = s.strftime("%A, %B ") + str(s.day) + ("" if length == 0 else e.strftime(" to %A, %B ") + str(e.day))
                found.append({"start": s.isoformat(), "end": e.isoformat(), "label": label})
                if len(found) >= limit:
                    return sorted(found, key=lambda a: a["start"])
    return sorted(found, key=lambda a: a["start"])


def find_swap_partners(staff_id: int, shift_ids: list[int], limit: int = 3) -> list[dict]:
    """Colleagues off that day who could take the shift without overtime."""
    partners: list[dict] = []
    for sid in shift_ids:
        shift = db.one("SELECT * FROM shifts WHERE id=?", (sid,))
        me = db.one("SELECT home_unit FROM staff WHERE id=?", (staff_id,))
        pool = db.query("SELECT * FROM staff WHERE pool='core' AND home_unit=? AND id<>? ORDER BY last_asked_at", (me["home_unit"], staff_id))
        for person in pool:
            result = eligibility.check(person, shift)
            if result.eligible and not result.overtime_hours:
                partners.append({"staff_id": person["id"], "name": person["name"], "shift_label": shift_label(shift)})
            if len(partners) >= limit:
                return partners
    return partners


def apply_planned(staff_id: int, start: date, end: date, kind: str, decision: dict, channel: str = "voice") -> int:
    """Persist a planned-leave request and its decision."""
    status = decision["outcome"]
    leave_id = db.execute(
        """INSERT INTO leave_requests (staff_id, kind, category, start_date, end_date, status, requested_at, decision, channel, decided_by, decided_at)
           VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (staff_id, kind, start.isoformat(), end.isoformat(), status, clock.stamp(), db.dumps(decision), channel,
         "policy" if status == "approved" else None, clock.stamp() if status == "approved" else None),
    )
    if status == "approved":
        _take_off_schedule(staff_id, start, end, kind)
    staff = db.one("SELECT name FROM staff WHERE id=?", (staff_id,))
    words = {"approved": "auto-approved", "pending_supervisor": "sent to supervisor",
             "alternatives_offered": "coverage too thin; alternatives offered"}[status]
    events.log("leave.decided", f"{staff['name']}: {kind} {start.isoformat()}–{end.isoformat()} {words}",
               {"leave_id": leave_id, "outcome": status})
    if status == "pending_supervisor":
        notify("Nurse Manager", "approval", f"{staff['name']} requests {kind} {start.isoformat()} to {end.isoformat()}: "
               + "; ".join(decision["summary"]))
    return leave_id


def _take_off_schedule(staff_id: int, start: date, end: date, kind: str) -> None:
    db.execute(
        """UPDATE assignments SET status='leave', note=?
           WHERE staff_id=? AND status='scheduled'
             AND shift_id IN (SELECT id FROM shifts WHERE date BETWEEN ? AND ?)""",
        (f"approved {kind}", staff_id, start.isoformat(), end.isoformat()),
    )


def supervisor_decide(leave_id: int, approve: bool, by: str = "Nurse Manager") -> dict:
    """Human decision on a request the engine escalated. Returns any gaps created."""
    req = db.one("SELECT * FROM leave_requests WHERE id=?", (leave_id,))
    if not req or req["category"] != "planned":
        raise ValueError("not a planned leave request")
    status = "approved" if approve else "declined"
    db.execute("UPDATE leave_requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
               (status, by, clock.stamp(), leave_id))
    gaps: list[int] = []
    if approve:
        start, end = date.fromisoformat(req["start_date"]), date.fromisoformat(req["end_date"])
        affected = scheduled_shifts(req["staff_id"], start, end)
        _take_off_schedule(req["staff_id"], start, end, req["kind"])
        for shift in affected:
            cov = coverage(shift)
            if cov["status"] == "short":
                gaps.append(db.execute(
                    """INSERT INTO gaps (shift_id, leave_id, vacated_staff_id, needs_charge, status, created_at)
                       VALUES (?, ?, ?, ?, 'open', ?)""",
                    (shift["id"], leave_id, req["staff_id"], 0 if cov["has_charge"] else 1, clock.stamp())))
    staff = db.one("SELECT name FROM staff WHERE id=?", (req["staff_id"],))
    events.log("leave.supervisor", f"{by} {status} {staff['name']}'s {req['kind']} request", {"leave_id": leave_id, "gaps": gaps})
    return {"status": status, "gaps": gaps}


def notify(recipient: str, kind: str, message: str) -> None:
    db.execute("INSERT INTO notifications (ts, recipient, kind, message) VALUES (?, ?, ?, ?)",
               (clock.stamp(), recipient, kind, message))
    events.log("notify", f"To {recipient}: {message}", {"recipient": recipient})
