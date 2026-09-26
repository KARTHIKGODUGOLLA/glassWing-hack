"""Required staffing from patient census and nurse-to-patient ratios."""
from __future__ import annotations

import math
from datetime import date

UNITS = [
    # id, name, beds, patients per RN, certification every RN must hold
    ("icu", "ICU", 8, 2, "icu"),
    ("medsurg", "Med-Surg 4 West", 20, 5, None),
]


def required_nurses(census: int, patients_per_nurse: int) -> int:
    """Minimum RNs for a shift. Never below 2: no unit runs with one nurse."""
    return max(2, math.ceil(census / patients_per_nurse))


def projected_census(unit_id: str, beds: int, day: date, kind: str) -> int:
    """Deterministic synthetic census forecast.

    A real deployment would read the ADT feed and a census model; here we vary
    occupancy by weekday so some days run thin and others have slack.
    """
    weekday_load = {0: 0.95, 1: 1.0, 2: 1.0, 3: 0.95, 4: 0.9, 5: 0.8, 6: 0.8}[day.weekday()]
    wobble = ((day.toordinal() * 7 + (0 if kind == "day" else 3)) % 5 - 2) * 0.03
    occupancy = min(1.0, max(0.5, weekday_load + wobble))
    census = round(beds * occupancy)
    if kind == "night":
        census = max(1, census - (1 if unit_id == "medsurg" else 0))
    return census
