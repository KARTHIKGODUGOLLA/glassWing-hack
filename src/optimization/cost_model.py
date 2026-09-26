"""What a fill costs: base pay, overtime, incentive pay and agency rates."""
from __future__ import annotations

from src import config


def shift_cost(staff: dict, hours: float, overtime_hours: float, tier: int) -> dict:
    """Estimated labor cost of putting `staff` on a shift of `hours`."""
    if staff["pool"] == "agency":
        rate = config.AGENCY_HOURLY_RATE
        return {"total": round(rate * hours), "rate": rate, "incentive": 0.0, "overtime_hours": 0.0}
    base = staff["hourly_rate"]
    incentive = config.INCENTIVE_PER_HOUR.get(tier, 0.0)
    straight = hours - overtime_hours
    total = straight * base + overtime_hours * base * config.OVERTIME_MULTIPLIER + hours * incentive
    return {"total": round(total), "rate": base, "incentive": incentive, "overtime_hours": overtime_hours}


def agency_baseline(hours: float) -> int:
    """What the same shift costs if the charge nurse goes straight to agency."""
    return round(config.AGENCY_HOURLY_RATE * hours)
