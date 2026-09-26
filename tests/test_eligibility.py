from datetime import date, timedelta

from src import db
from src.optimization import eligibility
from tests.conftest import shift, staff_named


def icu_day(demo):
    return shift("icu", demo["date"], "day")


def test_rest_rule_blocks_nurse_coming_off_a_night(demo):
    result = eligibility.check(staff_named("Alex Kim"), icu_day(demo))
    assert not result.eligible
    assert any("rest" in v for v in result.violations)


def test_consecutive_day_limit(demo):
    result = eligibility.check(staff_named("Marcus Johnson"), icu_day(demo))
    assert not result.eligible
    assert any("in a row" in v for v in result.violations)


def test_protected_leave_is_never_contacted(demo):
    result = eligibility.check(staff_named("Grace Liu"), icu_day(demo), voluntary_leave_ask=True)
    assert not result.eligible
    assert any("protected" in v for v in result.violations)


def test_discretionary_leave_only_for_voluntary_ask(demo):
    nora = staff_named("Nora Walsh")
    assert not eligibility.check(nora, icu_day(demo)).eligible
    assert eligibility.check(nora, icu_day(demo), voluntary_leave_ask=True).eligible


def test_icu_certification_required(demo):
    result = eligibility.check(staff_named("Sam Rivera"), icu_day(demo))
    assert "not ICU-certified" in result.violations


def test_weekly_hours_cap(demo):
    jordan = staff_named("Jordan Blake")  # float pool: starts the week at 0h
    day = date.fromisoformat(demo["date"])
    for k in (-6, -5, -4, -2, -1):  # five day shifts, never more than 3 in a row
        s = shift("medsurg", (day + timedelta(days=k)).isoformat(), "day")
        db.execute("INSERT INTO assignments (shift_id, staff_id, status) VALUES (?, ?, 'scheduled')", (s["id"], jordan["id"]))
    result = eligibility.check(jordan, icu_day(demo))
    assert not result.eligible
    assert any("cap 60h" in v for v in result.violations)
