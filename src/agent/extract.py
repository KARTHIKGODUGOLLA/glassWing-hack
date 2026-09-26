"""Offline fallback: keyword and date extraction used when the LLM is unavailable.

Deliberately simple. It covers the phrasings used in the demo and common
variants; the LLM handles everything else when it is online.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from src import config

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "a": 1}

SICK = r"\b(sick|ill|fever|flu|vomit\w*|throwing up|covid|migraine|stomach|unwell|not feeling well|doctor)\b"
EMERGENCY = r"\b(emergency|accident|kid|child|daughter|son|babysitter|daycare|car broke|flat tire|family)\b"
BEREAVEMENT = r"\b(funeral|passed away|passed|died|death|bereavement|lost my)\b"
CALLOUT = r"\b(call(ing)? out|can'?t (come|make|work)|cannot (come|make|work)|won'?t (be able|make)|not (coming|going to make)|out today)\b"
PLANNED = r"\b(vacation|time off|day off|days off|personal day|pto|holiday|leave|request|take off|off on)\b"


def _ordinal(n: str) -> int:
    return int(re.sub(r"(st|nd|rd|th)$", "", n))


def find_dates(text: str, today: date) -> list[date]:
    t = text.lower()
    found: list[tuple[int, date]] = []

    def add(pos: int, d: date) -> None:
        found.append((pos, d))

    for m in re.finditer(r"\b(today|this morning|tonight|this evening)\b", t):
        add(m.start(), today)
    for m in re.finditer(r"\btomorrow\b", t):
        add(m.start(), today + timedelta(days=1))
    for m in re.finditer(r"\b(next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t):
        if re.match(r",?\s*(the\s+)?(\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", t[m.end():]):
            continue  # "Tuesday, October 13": the explicit date wins
        ahead = (WEEKDAYS.index(m.group(2)) - today.weekday()) % 7 or 7
        add(m.start(), today + timedelta(days=ahead + (7 if m.group(1) and ahead < 7 else 0)))
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(st|nd|rd|th)?\b", t):
        month, day = MONTHS[m.group(1)], int(m.group(2))
        year = today.year + (1 if month < today.month - 1 else 0)
        try:
            add(m.start(), date(year, month, day))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})\b", t):
        try:
            month, day = int(m.group(1)), int(m.group(2))
            add(m.start(), date(today.year + (1 if month < today.month - 1 else 0), month, day))
        except ValueError:
            pass
    for m in re.finditer(r"\bthe (\d{1,2})(st|nd|rd|th)\b", t):
        if any(abs(p - m.start()) < 12 for p, _ in found):
            continue
        n = _ordinal(m.group(1) + m.group(2))
        month, year = today.month, today.year
        if n < today.day:
            month, year = (1, year + 1) if month == 12 else (month + 1, year)
        try:
            add(m.start(), date(year, month, n))
        except ValueError:
            pass
    holidays = {name.lower(): d for d, name in config.blackout_dates(today).items()}
    for name, d in sorted(holidays.items(), key=lambda kv: kv[1]):
        idx = t.find(name)
        if idx != -1 and not any(p == idx for p, _ in found):
            add(idx, d)
    found.sort(key=lambda x: x[0])
    ordered: list[date] = []
    for _, d in found:
        if d not in ordered:
            ordered.append(d)
    return ordered


def extract_inbound(text: str, today: date, slots: dict) -> dict:
    """Update slots from one caller utterance."""
    t = text.lower()
    out = dict(slots)
    is_sick = re.search(SICK, t)
    is_emergency = re.search(EMERGENCY, t)
    is_bereavement = re.search(BEREAVEMENT, t)
    is_callout = re.search(CALLOUT, t)
    is_planned = re.search(PLANNED, t)

    dates = find_dates(text, today)
    if dates:
        span = re.search(r"\bfor (\w+) (days|nights|shifts)\b", t)
        start, end = min(dates), max(dates)
        if span and len(dates) == 1:
            n = NUMBER_WORDS.get(span.group(1)) or (int(span.group(1)) if span.group(1).isdigit() else 1)
            end = start + timedelta(days=n - 1)
        out["start_date"], out["end_date"] = start.isoformat(), end.isoformat()

    if "night" in t or "tonight" in t:
        out["shift"] = "night"
    elif re.search(r"\b(day shift|days|morning)\b", t):
        out["shift"] = out.get("shift") or "day"

    soon = bool(dates) and min(dates) <= today + timedelta(days=1)
    if is_sick or is_bereavement or is_callout or (is_emergency and not is_planned):
        if not (is_planned and dates and not soon and not (is_sick or is_bereavement)):
            out["intent"] = "callout"
    if out.get("intent") != "callout" and is_planned:
        out["intent"] = "planned_leave"
    if out.get("intent") == "callout" and not out.get("start_date"):
        out["start_date"] = out["end_date"] = today.isoformat() if re.search(r"\b(today|now|this morning|my shift)\b", t) else None

    if is_bereavement:
        out["leave_type"] = "bereavement"
    elif is_sick:
        out["leave_type"] = "sick"
    elif is_emergency and out.get("intent") == "callout":
        out["leave_type"] = "emergency"
    elif re.search(r"\b(vacation|trip|holiday)\b", t):
        out["leave_type"] = "vacation"
    elif re.search(r"\b(personal|day off|time off|pto)\b", t):
        out["leave_type"] = out.get("leave_type") or "personal"
    return out


def classify_offer_response(text: str) -> str:
    """accepted | declined | question | unclear"""
    t = text.lower()
    if re.search(r"\b(no|nope|can'?t|cannot|not able|unable|pass|sorry|not today|won'?t)\b", t) and not re.search(r"\bno problem\b", t):
        return "declined"
    if re.search(r"\b(yes|yeah|yep|sure|ok(ay)?|i can|i'?ll (take|do|be)|count me in|put me down|sounds good|absolutely)\b", t):
        return "accepted"
    if "?" in t or re.search(r"\b(what|how much|incentive|rate|pay|time|when|which)\b", t):
        return "question"
    return "unclear"
