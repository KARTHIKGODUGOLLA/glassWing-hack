"""Coverage evaluation and backfill candidate ranking (the escalation ladder)."""
from __future__ import annotations

from datetime import date, datetime

from src import clock, config, db
from src.forecasting.census_forecast import required_nurses
from src.optimization import cost_model, eligibility


def get_shift(shift_id: int) -> dict:
    shift = db.one("SELECT * FROM shifts WHERE id=?", (shift_id,))
    if not shift:
        raise KeyError(f"shift {shift_id} not found")
    return shift


def shift_label(shift: dict) -> str:
    unit = db.one("SELECT name FROM units WHERE id=?", (shift["unit_id"],))
    day = date.fromisoformat(shift["date"])
    hours = "7a–7p" if shift["kind"] == "day" else "7p–7a"
    return f"{unit['name'] if unit else shift['unit_id']} {shift['kind']} shift, {day.strftime('%a %b')} {day.day} ({hours})"


def spoken_label(shift: dict) -> str:
    """How the voice agent says a shift: 'ICU day shift today, 7 AM to 7 PM'."""
    unit = db.one("SELECT name FROM units WHERE id=?", (shift["unit_id"],))
    day = date.fromisoformat(shift["date"])
    delta = (day - clock.today()).days
    when = {0: "today", 1: "tomorrow", -1: "last night"}.get(delta) or day.strftime("on %A, %B ") + str(day.day)
    if delta == 0 and shift["kind"] == "night":
        when = "tonight"
    hours = "7 AM to 7 PM" if shift["kind"] == "day" else "7 PM to 7 AM"
    return f"{unit['name'] if unit else shift['unit_id']} {shift['kind']} shift {when}, {hours}"


def coverage(shift: dict, *, without_staff: int | None = None) -> dict:
    """Required vs staffed for one shift, with the skill-mix check.

    `without_staff` answers "what if this person weren't there", which is how
    planned leave is evaluated.
    """
    unit = db.one("SELECT * FROM units WHERE id=?", (shift["unit_id"],))
    rows = db.query(
        """SELECT a.id AS assignment_id, a.status, a.role, a.source, a.tier, a.note,
                  st.id AS staff_id, st.name, st.pool, st.charge_certified, st.icu_certified
           FROM assignments a JOIN staff st ON st.id = a.staff_id
           WHERE a.shift_id=? ORDER BY a.role='charge' DESC, a.source, st.name""",
        (shift["id"],),
    )
    active = [r for r in rows if r["status"] == "scheduled" and r["staff_id"] != without_staff]
    required = required_nurses(shift["census"], unit["patients_per_nurse"])
    has_charge = any(r["charge_certified"] for r in active)
    cert_ok = unit["requires_cert"] != "icu" or all(r["icu_certified"] for r in active)
    margin = len(active) - required
    if margin < 0 or not has_charge or not cert_ok:
        status = "short"
    elif margin == 0:
        status = "at_min"
    else:
        status = "ok"
    return {
        "shift_id": shift["id"],
        "unit_id": shift["unit_id"],
        "unit": unit["name"],
        "date": shift["date"],
        "kind": shift["kind"],
        "census": shift["census"],
        "ratio": f"1:{unit['patients_per_nurse']}",
        "required": required,
        "staffed": len(active),
        "margin": margin,
        "has_charge": has_charge,
        "cert_ok": cert_ok,
        "status": status,
        "roster": rows,
    }


def _seniority_years(staff: dict) -> int:
    if not staff.get("hire_date"):
        return 0
    return max(0, (clock.today() - date.fromisoformat(staff["hire_date"])).days // 365)


def _days_since_asked(staff: dict) -> int | None:
    if not staff.get("last_asked_at"):
        return None
    return max(0, (clock.now() - datetime.fromisoformat(staff["last_asked_at"])).days)


def rank_candidates(shift: dict, *, needs_charge: bool = False) -> tuple[list[dict], list[dict]]:
    """Rank everyone who could fill `shift`, following the escalation ladder.

    Tier 1 float pool and per diem, tier 2 staff who volunteered for extra
    shifts (incentive pay), tier 3 agency, tier 4 a voluntary ask to someone on
    discretionary leave. Within a tier: no overtime first, then rotation (asked
    least recently), then seniority. Returns (ranked, excluded).
    """
    unit = db.one("SELECT * FROM units WHERE id=?", (shift["unit_id"],))
    hours = eligibility.shift_hours(shift)
    ranked: list[dict] = []
    excluded: list[dict] = []

    for staff in db.query("SELECT * FROM staff ORDER BY id"):
        other_unit_core = staff["pool"] == "core" and staff["home_unit"] != shift["unit_id"]
        result = eligibility.check(staff, shift, needs_charge=needs_charge)
        if result.on_shift:
            continue
        tier: int | None = None
        if result.on_discretionary_leave and staff["pool"] != "agency":
            voluntary = eligibility.check(staff, shift, needs_charge=needs_charge, voluntary_leave_ask=True)
            if voluntary.eligible:
                result, tier = voluntary, 4
        elif result.eligible:
            if staff["pool"] in ("float", "per_diem"):
                tier = 1
            elif staff["pool"] == "agency":
                tier = 3
            elif staff["wants_extra_shifts"]:
                tier = 2
            else:
                result.violations.append("has not opted in to extra shifts")

        if tier is None:
            if other_unit_core and unit["requires_cert"] and not staff["icu_certified"]:
                continue  # other-unit staff without the certification: noise, not a decision
            excluded.append({"staff": staff, "reasons": result.violations or ["not eligible"]})
            continue

        cost = cost_model.shift_cost(staff, hours, result.overtime_hours, tier)
        asked = _days_since_asked(staff)
        reasons = [config.TIERS[tier]]
        if unit["requires_cert"] == "icu":
            reasons.append("ICU-certified")
        if needs_charge:
            reasons.append("charge-certified")
        if staff["pool"] != "agency":
            reasons.append(
                f"{result.overtime_hours:.0f}h overtime" if result.overtime_hours
                else f"no overtime ({result.hours_7d_after - hours:.0f}h this week)"
            )
            reasons.append("never asked to cover" if asked is None else f"last asked {asked}d ago")
            reasons.append(f"{_seniority_years(staff)} yrs seniority")
        if cost["incentive"]:
            reasons.append(f"+${cost['incentive']:.0f}/h incentive")
        if tier == 4:
            reasons.append("on approved leave: voluntary, free to say no")

        ranked.append({
            "staff": staff,
            "tier": tier,
            "eligibility": result,
            "cost": cost,
            "reasons": reasons,
            "_key": (
                tier,
                result.overtime_hours > 0,
                -(asked if asked is not None else 10_000),
                -_seniority_years(staff),
                staff["id"],
            ),
        })

    ranked.sort(key=lambda c: c["_key"])
    return ranked, excluded
