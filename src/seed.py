"""Synthetic hospital: two units, ~50 staff, an 11-week schedule.

The schedule is generated greedily with the same eligibility engine the
backfill loop uses, so the seeded data obeys the policy. A few shifts are
pinned so the live demo tells a clean story:

* Demo day, ICU day shift: exactly at minimum (Maria Chen, Denise Howard, Kevin O'Brien).
* Jordan Blake (float, ICU) is free but declines. Priya Patel volunteered for
  extra shifts, asks about pay, then accepts.
* Alex Kim worked last night (rest rule), Marcus Johnson would hit 4 days in a
  row, Grace Liu is out sick (protected), Nora Walsh is on vacation (last resort).
* Devon Wright has a thin ICU day ~2.5 weeks out for the planned-leave demo.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from src import clock, config, db
from src.forecasting.census_forecast import UNITS, projected_census, required_nurses
from src.optimization import eligibility

FIRST = ["Ava", "Ben", "Carla", "Dana", "Eli", "Fatima", "Gabe", "Hana", "Isaac", "Jade", "Kofi", "Laura",
         "Mateo", "Nia", "Omar", "Paula", "Quinn", "Rosa", "Sean", "Tara", "Uma", "Victor", "Wendy", "Xavier",
         "Yara", "Zach", "Aisha", "Brian", "Chloe", "Diego", "Erin", "Felix", "Gina", "Hector", "Ivy", "Joel",
         "Kira", "Liam", "Maya", "Noah"]
LAST = ["Alvarez", "Brooks", "Castillo", "Duarte", "Ellis", "Foster", "Garcia", "Hughes", "Ibrahim", "James",
        "Keller", "Lopez", "Murphy", "Nguyen", "Ortiz", "Park", "Quintero", "Reyes", "Silva", "Turner",
        "Usman", "Vargas", "Walker", "Xu", "Young", "Zimmer", "Adams", "Bell", "Cruz", "Diaz", "Evans", "Flynn",
        "Grant", "Hale", "Irwin", "Jensen", "Kim", "Lam", "Moss", "Nash"]

DECLINE = {"disposition": "decline", "lines": [
    "Oh, hi. Today? I'm really sorry, I can't. I'm driving my mom to a procedure this morning."]}
ASK_THEN_ACCEPT = {"disposition": "ask_then_accept", "lines": [
    "Hey, I'm up. What time does it start?",
    "Okay, yeah. I'll take it. I can be there by seven."]}
ACCEPT = {"disposition": "accept", "lines": ["Sure, I can do that. Put me down."]}
AWAY = {"disposition": "decline", "lines": ["I'm actually out of town this week, so I'll have to pass. Sorry!"]}


def _h(*parts: object) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _shift_bounds(day: date, kind: str) -> tuple[datetime, datetime]:
    start_s, hours = config.SHIFT_KINDS[kind]
    hh, mm = (int(x) for x in start_s.split(":"))
    start = datetime(day.year, day.month, day.day, hh, mm)
    return start, start + timedelta(hours=hours)


def seed(demo_day: date | None = None) -> dict:
    D = demo_day or config.demo_date()
    clock.start(D)
    db.reset()
    first_day, last_day = D - timedelta(days=7), D + timedelta(days=75)

    for unit in UNITS:
        db.execute("INSERT INTO units (id, name, beds, patients_per_nurse, requires_cert) VALUES (?, ?, ?, ?, ?)", unit)

    # --- Staff ---------------------------------------------------------------
    people: list[dict] = []

    def person(name, unit, pool, *, icu=False, charge=False, rate=None, extra=False, asked=None, years=None, persona=None):
        hire = D - timedelta(days=365 * (years if years is not None else 1 + _h(name) % 18) + _h(name, "d") % 300)
        people.append({
            "name": name, "phone": f"(617) 555-{len(people) + 100:04d}", "home_unit": unit, "pool": pool,
            "icu_certified": int(icu), "charge_certified": int(charge),
            "hire_date": hire.isoformat(), "hourly_rate": rate or 52 + _h(name, "r") % 18,
            "wants_extra_shifts": int(extra),
            "last_asked_at": (datetime.combine(D, datetime.min.time()) - timedelta(days=asked)).isoformat() if asked is not None else None,
            "sim_persona": db.dumps(persona or ACCEPT),
        })

    # Named demo cast (ICU)
    person("Maria Chen", "icu", "core", icu=True, years=6)
    person("Denise Howard", "icu", "core", icu=True, charge=True, years=19)
    person("Kevin O'Brien", "icu", "core", icu=True, years=4)
    person("Priya Patel", "icu", "core", icu=True, extra=True, asked=21, years=8, persona=ASK_THEN_ACCEPT)
    person("Lena Okafor", "icu", "core", icu=True, extra=True, asked=2, years=5)
    person("Marcus Johnson", "icu", "core", icu=True, extra=True, asked=15, years=11)
    person("Nora Walsh", "icu", "core", icu=True, asked=30, years=14, persona=AWAY)
    person("Grace Liu", "icu", "core", icu=True, years=9)
    person("Devon Wright", "icu", "core", icu=True, years=3)
    person("Jordan Blake", None, "float", icu=True, charge=True, rate=62, asked=9, years=7, persona=DECLINE)
    person("Sam Rivera", None, "float", rate=60, asked=12, years=5)
    person("Alex Kim", "icu", "per_diem", icu=True, rate=68, asked=20, years=10)
    person("Chris Morgan", "medsurg", "per_diem", rate=64, asked=6, years=6)
    person("Taylor Reed", None, "agency", icu=True, rate=config.AGENCY_HOURLY_RATE, years=0)
    person("Jamie Fox", None, "agency", rate=config.AGENCY_HOURLY_RATE, years=0)

    # Filler staff so both units run realistically
    names = iter(f"{f} {LAST[(i * 7 + 3) % len(LAST)]}" for i, f in enumerate(FIRST))
    for i in range(14):
        # i == 7 volunteers for extras too, but was asked yesterday, so rotation puts her after Priya and Lena
        person(next(names), "icu", "core", icu=True, charge=i % 4 == 0, extra=i == 7, asked=1 if i == 7 else _h(i, "a") % 40)
    for i in range(22):
        person(next(names), "medsurg", "core", icu=False, charge=i % 4 == 0, extra=i % 6 == 1, asked=_h(i, "m") % 40)

    cols = list(people[0].keys())
    db.executemany(
        f"INSERT INTO staff ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(p[c] for c in cols) for p in people],
    )
    by_name = {r["name"]: r for r in db.query("SELECT * FROM staff")}
    sid = lambda name: by_name[name]["id"]  # noqa: E731

    # --- Shifts --------------------------------------------------------------
    rows = []
    d = first_day
    while d <= last_day:
        for kind in config.SHIFT_KINDS:
            for unit_id, _, beds, _, _ in UNITS:
                start, end = _shift_bounds(d, kind)
                rows.append((unit_id, d.isoformat(), kind, start.isoformat(), end.isoformat(),
                             projected_census(unit_id, beds, d, kind)))
        d += timedelta(days=1)
    db.executemany("INSERT INTO shifts (unit_id, date, kind, start, end, census) VALUES (?, ?, ?, ?, ?, ?)", rows)

    def shift_of(unit_id: str, day: date, kind: str) -> dict:
        return db.one("SELECT * FROM shifts WHERE unit_id=? AND date=? AND kind=?", (unit_id, day.isoformat(), kind))

    # Demo-day ICU day shift: census 6 at 1:2 -> 3 RNs required, exactly 3 scheduled.
    db.execute("UPDATE shifts SET census=6 WHERE unit_id='icu' AND date=? AND kind='day'", (D.isoformat(),))
    thin_day, alt_days = D + timedelta(days=17), [D + timedelta(days=15), D + timedelta(days=20)]

    # Pinned shifts: (unit, day, kind) -> staff names, and how many total ("min" = exactly required).
    pins: dict[tuple[str, date, str], tuple[list[str], str | int]] = {
        ("icu", D, "day"): (["Denise Howard", "Maria Chen", "Kevin O'Brien"], "min"),
        ("icu", D - timedelta(days=1), "night"): (["Alex Kim"], "auto"),
        ("icu", D - timedelta(days=1), "day"): (["Marcus Johnson"], "auto"),
        ("icu", D - timedelta(days=2), "day"): (["Marcus Johnson"], "auto"),
        ("icu", D - timedelta(days=3), "day"): (["Marcus Johnson"], "auto"),
        ("icu", thin_day, "day"): (["Devon Wright"], "min"),
        **{("icu", a, "day"): (["Devon Wright"], "plus2") for a in alt_days},
    }
    blocked: dict[str, set[date]] = {
        "Priya Patel": {D + timedelta(days=k) for k in range(-4, 2)},
        "Lena Okafor": {D + timedelta(days=k) for k in range(-4, 2)},
        "Nora Walsh": {D + timedelta(days=k) for k in range(-3, 5)},
        "Grace Liu": {D + timedelta(days=k) for k in range(-2, 3)},
        "Devon Wright": {thin_day - timedelta(days=1), thin_day + timedelta(days=1)} | {a + timedelta(days=s) for a in alt_days for s in (-1, 1)},
    }
    for name in ("Maria Chen", "Denise Howard", "Kevin O'Brien"):
        blocked[name] = {D - timedelta(days=1), D + timedelta(days=1)}

    def assign(shift: dict, staff: dict, role: str = "rn") -> None:
        db.execute(
            "INSERT INTO assignments (shift_id, staff_id, status, role, source, created_at) VALUES (?, ?, 'scheduled', ?, 'schedule', ?)",
            (shift["id"], staff["id"], role, (datetime.combine(D, datetime.min.time()) - timedelta(days=30)).isoformat()),
        )

    # Place pins first so the generator plans around them.
    for (unit_id, day, kind), (names_, _) in pins.items():
        shift = shift_of(unit_id, day, kind)
        for n in names_:
            assign(shift, by_name[n], "charge" if n == "Denise Howard" and day == D else "rn")

    core = {u: [p for p in by_name.values() if p["pool"] == "core" and p["home_unit"] == u] for u, *_ in UNITS}
    load: dict[int, int] = {p["id"]: 0 for p in by_name.values()}

    d = first_day
    while d <= last_day:
        for kind in config.SHIFT_KINDS:
            for unit_id, _, _, ratio, _ in UNITS:
                shift = shift_of(unit_id, d, kind)
                required = required_nurses(shift["census"], ratio)
                mode = pins.get((unit_id, d, kind), ([], "auto"))[1]
                buffer = {"min": 0, "plus2": 2}.get(mode, [0, 1, 1, 1, 1, 2, 2, 1][_h(unit_id, d, kind) % 8])
                target = required + buffer
                on = db.query("SELECT a.staff_id, st.charge_certified FROM assignments a JOIN staff st ON st.id=a.staff_id WHERE shift_id=?", (shift["id"],))
                have = {r["staff_id"] for r in on}
                has_charge = any(r["charge_certified"] for r in on)
                pool = sorted(core[unit_id], key=lambda p: (load[p["id"]], _h(p["id"], d, kind)))
                for want_charge in (True, False):
                    for p in pool:
                        if len(have) >= target or (want_charge and has_charge):
                            break
                        if p["id"] in have or (want_charge and not p["charge_certified"]):
                            continue
                        if d in blocked.get(p["name"], set()):
                            continue
                        check = eligibility.check(p, shift)
                        if not check.eligible or check.hours_7d_after > 48:
                            continue
                        assign(shift, p, "charge" if want_charge else "rn")
                        have.add(p["id"])
                        load[p["id"]] += 1
                        has_charge = has_charge or bool(p["charge_certified"])
        d += timedelta(days=1)

    # Mark one charge nurse per shift where the generator did not.
    for shift in db.query("SELECT id FROM shifts"):
        if not db.one("SELECT 1 FROM assignments WHERE shift_id=? AND role='charge'", (shift["id"],)):
            first = db.one("""SELECT a.id FROM assignments a JOIN staff st ON st.id=a.staff_id
                              WHERE a.shift_id=? AND st.charge_certified=1 ORDER BY st.hire_date LIMIT 1""", (shift["id"],))
            if first:
                db.execute("UPDATE assignments SET role='charge' WHERE id=?", (first["id"],))

    # --- Existing leave --------------------------------------------------------
    past = (datetime.combine(D, datetime.min.time()) - timedelta(days=40)).isoformat()
    db.execute(
        """INSERT INTO leave_requests (staff_id, kind, category, start_date, end_date, status, requested_at, decision, channel, decided_by, decided_at)
           VALUES (?, 'vacation', 'planned', ?, ?, 'approved', ?, ?, 'app', 'policy', ?)""",
        (sid("Nora Walsh"), (D - timedelta(days=3)).isoformat(), (D + timedelta(days=4)).isoformat(), past,
         db.dumps({"outcome": "approved", "checks": [], "summary": []}), past),
    )
    db.execute(
        """INSERT INTO leave_requests (staff_id, kind, category, start_date, end_date, status, requested_at, decision, channel, decided_by, decided_at)
           VALUES (?, 'sick', 'unplanned', ?, ?, 'logged', ?, ?, 'voice', 'policy', ?)""",
        (sid("Grace Liu"), (D - timedelta(days=1)).isoformat(), (D + timedelta(days=1)).isoformat(),
         (datetime.combine(D, datetime.min.time()) - timedelta(hours=30)).isoformat(),
         db.dumps({"outcome": "logged", "policy": "Unplanned leave is protected."}),
         (datetime.combine(D, datetime.min.time()) - timedelta(hours=30)).isoformat()),
    )

    demo = {
        "date": D.isoformat(),
        "thin_day": thin_day.isoformat(),
        "alt_days": [a.isoformat() for a in alt_days],
        "callout_staff_id": sid("Maria Chen"),
        "leave_staff_id": sid("Devon Wright"),
        "charge_nurse": "Denise Howard",
    }
    db.set_meta("demo", db.dumps(demo))
    db.execute("DELETE FROM events")
    db.execute("DELETE FROM notifications")
    return demo


if __name__ == "__main__":
    db.connect()
    info = seed()
    print("Seeded", info)
