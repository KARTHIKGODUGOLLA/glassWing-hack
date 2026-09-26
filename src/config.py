"""Settings and staffing policy.

Every number the rules engine uses lives here so a supervisor can read (and
change) the policy in one place.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- Inference (Sciforium, OpenAI-compatible) --------------------------------
SCIFORIUM_API_KEY = os.getenv("SCIFORIUM_API_KEY", "").strip()
SCIFORIUM_BASE_URL = os.getenv("SCIFORIUM_BASE_URL", "https://api.sciforium.com/v1")
SCIFORIUM_MODEL = os.getenv("SCIFORIUM_MODEL", "").strip()
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "20"))
# Simulated nurses use canned, scripted lines unless this is on.
SIM_PERSONA_LLM = os.getenv("SIM_PERSONA_LLM", "0") == "1"

# --- Telephony (optional) ----------------------------------------------------
VAPI_API_KEY = os.getenv("VAPI_API_KEY", "").strip()
VAPI_PHONE_NUMBER_ID = os.getenv("VAPI_PHONE_NUMBER_ID", "").strip()

# --- Demo ------------------------------------------------------------------
DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "shiftvoice.db")))
HOSPITAL_NAME = os.getenv("HOSPITAL_NAME", "St. Brigid Medical Center")
DEMO_START_TIME = "05:30"  # the call-out happens at 5:30 AM


def demo_date() -> date:
    raw = os.getenv("DEMO_DATE", "").strip()
    return date.fromisoformat(raw) if raw else date.today()


# --- Shifts --------------------------------------------------------------------
SHIFT_KINDS = {
    "day": ("07:00", 12),    # start, hours
    "night": ("19:00", 12),
}

# --- Policy ------------------------------------------------------------------
MIN_REST_HOURS = 10            # between the end of one shift and the start of the next
MAX_CONSECUTIVE_DAYS = 3       # 12-hour shifts in a row
WEEKLY_HOURS_CAP = 60          # hard fatigue cap, rolling 7 days
OVERTIME_THRESHOLD_HOURS = 40  # above this, pay is time and a half
OVERTIME_MULTIPLIER = 1.5

PLANNED_LEAVE_MIN_NOTICE_DAYS = 14
PLANNED_LEAVE_MAX_AUTO_DAYS = 5  # longer requests always go to the supervisor
BACKFILL_AUTOSTART_HOURS = 48    # gaps sooner than this start outreach immediately

# Escalation ladder. Tier 4 is never used to revoke leave: it is a voluntary ask.
TIERS = {
    1: "Float pool / per diem",
    2: "Volunteered for extra shifts",
    3: "Agency",
    4: "Voluntary ask (on discretionary leave)",
}
INCENTIVE_PER_HOUR = {1: 0.0, 2: 15.0, 3: 0.0, 4: 25.0}
AGENCY_HOURLY_RATE = 135.0

# Leave priority tiers. Protected leave is never questioned, never revoked.
PROTECTED_LEAVE = {"sick", "emergency", "bereavement", "unspecified"}
DISCRETIONARY_LEAVE = {"vacation", "personal"}

MAX_OUTBOUND_TURNS = 4
LIVE_REPLY_TIMEOUT_S = 120


def blackout_dates(anchor: date) -> dict[date, str]:
    """Holiday blackout dates in the scheduling horizon."""
    year = anchor.year
    days: dict[date, str] = {}
    # US Thanksgiving: fourth Thursday of November, and the day after.
    nov1 = date(year, 11, 1)
    thanksgiving = nov1 + timedelta(days=(3 - nov1.weekday()) % 7 + 21)
    days[thanksgiving] = "Thanksgiving"
    days[thanksgiving + timedelta(days=1)] = "Thanksgiving"
    days[date(year, 12, 24)] = "Christmas Eve"
    days[date(year, 12, 25)] = "Christmas"
    days[date(year, 12, 31)] = "New Year's Eve"
    days[date(year + 1, 1, 1)] = "New Year's Day"
    return days


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")
