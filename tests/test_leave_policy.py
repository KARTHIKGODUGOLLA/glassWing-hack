from datetime import date, timedelta

from src import config, db
from src.optimization import leave_policy
from tests.conftest import shift, staff_named


def today(demo) -> date:
    return date.fromisoformat(demo["date"])


def test_callout_is_always_accepted_without_a_reason(demo):
    s = shift("icu", demo["date"], "day")
    decision = leave_policy.log_callout(staff_named("Maria Chen")["id"], [s["id"]], "no reason given")
    req = db.one("SELECT * FROM leave_requests WHERE id=?", (decision["leave_id"],))
    assert req["status"] == "logged" and req["kind"] == "unspecified"
    assert decision["gaps"] and decision["gaps"][0]["short_by"] == 1


def test_callout_with_slack_creates_no_gap(demo):
    from src.optimization.staffing_solver import coverage
    s = next(s for s in db.query("SELECT * FROM shifts WHERE date>?", (demo["date"],)) if coverage(s)["margin"] >= 1)
    person = db.one("""SELECT a.staff_id FROM assignments a JOIN staff st ON st.id=a.staff_id
                       WHERE a.shift_id=? AND a.status='scheduled' AND a.role<>'charge' LIMIT 1""", (s["id"],))
    decision = leave_policy.log_callout(person["staff_id"], [s["id"]], "sick")
    assert decision["gaps"] == []


def test_thin_day_offers_alternatives_not_a_no(demo):
    thin = date.fromisoformat(demo["thin_day"])
    d = leave_policy.evaluate_planned(demo["leave_staff_id"], thin, thin, "personal", today(demo))
    assert d["outcome"] == "alternatives_offered"
    assert set(demo["alt_days"]) <= {a["start"] for a in d["alternatives"]}
    assert d["swap_partners"]


def test_safe_day_is_auto_approved_and_removed_from_schedule(demo):
    alt = date.fromisoformat(demo["alt_days"][0])
    d = leave_policy.evaluate_planned(demo["leave_staff_id"], alt, alt, "personal", today(demo))
    assert d["outcome"] == "approved"
    leave_policy.apply_planned(demo["leave_staff_id"], alt, alt, "personal", d)
    row = db.one("""SELECT a.status FROM assignments a JOIN shifts s ON s.id=a.shift_id
                    WHERE a.staff_id=? AND s.date=?""", (demo["leave_staff_id"], alt.isoformat()))
    assert row["status"] == "leave"


def test_short_notice_goes_to_supervisor(demo):
    alt = date.fromisoformat(demo["alt_days"][0])
    d = leave_policy.evaluate_planned(demo["leave_staff_id"], alt, alt, "personal", alt - timedelta(days=3))
    assert d["outcome"] == "pending_supervisor"
    assert any("notice" in s.lower() for s in d["summary"])


def test_blackout_offers_alternatives(demo):
    thanksgiving = next(d for d, n in config.blackout_dates(today(demo)).items() if n == "Thanksgiving")
    d = leave_policy.evaluate_planned(demo["leave_staff_id"], thanksgiving, thanksgiving, "vacation", today(demo))
    assert d["outcome"] == "alternatives_offered"
    assert any(c["rule"] == "Blackout dates" and c["result"] == "fail" for c in d["checks"])
    assert d["alternatives"]


def test_supervisor_approval_of_thin_day_opens_a_gap(demo):
    thin = date.fromisoformat(demo["thin_day"])
    d = leave_policy.evaluate_planned(demo["leave_staff_id"], thin, thin, "personal", today(demo))
    leave_id = leave_policy.apply_planned(demo["leave_staff_id"], thin, thin, "personal", d)
    db.execute("UPDATE leave_requests SET status='pending_supervisor' WHERE id=?", (leave_id,))
    result = leave_policy.supervisor_decide(leave_id, approve=True)
    assert result["status"] == "approved" and len(result["gaps"]) == 1


def test_long_leave_goes_to_supervisor_with_coverage_impact(demo):
    start = today(demo) + timedelta(days=30)
    d = leave_policy.evaluate_planned(demo["leave_staff_id"], start, start + timedelta(days=7), "vacation", today(demo))
    assert d["outcome"] == "pending_supervisor"
    assert any(c["rule"] == "Length" and c["result"] == "warn" for c in d["checks"])
