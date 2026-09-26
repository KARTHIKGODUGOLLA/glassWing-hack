from datetime import date, timedelta

import pytest

from src import db, seed
from src.optimization import leave_policy
from src.optimization.staffing_solver import coverage, rank_candidates
from tests.conftest import DEMO_DAY, shift, staff_named


def test_demo_shift_starts_exactly_at_minimum(demo):
    cov = coverage(shift("icu", demo["date"], "day"))
    assert (cov["staffed"], cov["required"], cov["status"]) == (3, 3, "at_min")
    assert cov["has_charge"]


def test_every_seeded_shift_meets_ratio_and_skill_mix(demo):
    short = [s for s in db.query("SELECT * FROM shifts") if coverage(s)["status"] == "short"]
    assert short == []


def test_escalation_ladder_order(demo):
    s = shift("icu", demo["date"], "day")
    leave_policy.log_callout(staff_named("Maria Chen")["id"], [s["id"]], "sick")
    ranked, excluded = rank_candidates(s)
    names = [c["staff"]["name"] for c in ranked]
    tiers = [c["tier"] for c in ranked]
    assert names[:2] == ["Jordan Blake", "Priya Patel"]
    assert tiers == sorted(tiers), "tiers must be called in ladder order"
    assert ranked[-1]["staff"]["name"] == "Nora Walsh" and ranked[-1]["tier"] == 4
    assert any(c["staff"]["pool"] == "agency" and c["tier"] == 3 for c in ranked)
    ruled_out = {e["staff"]["name"]: " ".join(e["reasons"]) for e in excluded}
    assert "rest" in ruled_out["Alex Kim"]
    assert "in a row" in ruled_out["Marcus Johnson"]
    assert "protected" in ruled_out["Grace Liu"]
    assert "Maria Chen" not in ruled_out  # people on the shift are not candidates at all


def test_rotation_breaks_ties_within_a_tier(demo):
    ranked, _ = rank_candidates(shift("icu", demo["date"], "day"))
    tier2 = [c["staff"]["name"] for c in ranked if c["tier"] == 2 and not c["eligibility"].overtime_hours]
    assert tier2.index("Priya Patel") < tier2.index("Lena Okafor")  # asked 21d ago vs 2d ago


def test_charge_callout_requires_charge_certified_backfill(demo):
    s = shift("icu", demo["date"], "day")
    decision = leave_policy.log_callout(staff_named("Denise Howard")["id"], [s["id"]], "sick")
    assert decision["gaps"][0]["needs_charge"]
    ranked, _ = rank_candidates(s, needs_charge=True)
    assert ranked and all(c["staff"]["charge_certified"] for c in ranked)


@pytest.mark.parametrize("offset", range(7))
def test_demo_story_holds_on_any_weekday(offset):
    """The seed must tell the same story whatever day the demo runs."""
    day = DEMO_DAY + timedelta(days=offset)
    db.connect(":memory:")
    info = seed.seed(day)
    s = shift("icu", info["date"], "day")
    assert coverage(s)["status"] == "at_min"
    leave_policy.log_callout(info["callout_staff_id"], [s["id"]], "sick")
    ranked, _ = rank_candidates(s)
    assert [c["staff"]["name"] for c in ranked[:3]] == ["Jordan Blake", "Priya Patel", "Lena Okafor"]
    thin = date.fromisoformat(info["thin_day"])
    decision = leave_policy.evaluate_planned(info["leave_staff_id"], thin, thin, "personal", day)
    assert decision["outcome"] == "alternatives_offered"
    assert info["alt_days"][0] in {a["start"] for a in decision["alternatives"]}
