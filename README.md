# ShiftVoice

A voice agent that takes nurse call-outs by phone at 5 AM, logs them, and fills the hole itself. It ranks replacements with auditable staffing rules, calls them one by one, and tells the charge nurse when the shift is covered.

Built at Test Flight, the Glasswing Ventures hackathon, September 26 and 27, 2026.

Team: @KARTHIKGODUGOLLA, @mmanikass, @joeymussalli

## The problem

When a nurse calls in sick before a 7 AM shift, the charge nurse or staffing office has to:
1. take the call
2. work out whether the unit is now below its nurse-to-patient ratio
3. phone down a list of float, per-diem and off-duty nurses until someone says yes
4. fall back to expensive agency staff if nobody does

That happens every day on every unit. It lands at the worst time, eats an hour or more of a clinical leader's morning, and ends in agency fill or an unsafe ratio when it goes badly.

Planned leave has a related problem. Approving time off without checking coverage produces thin days that later turn into the same scramble.

## Who pays

The buyer is the CNO or director of nursing, from the nursing operations or staffing office budget. That is the same budget that pays for agency nurses and overtime.

They would sign because of three outcomes:
- fewer agency shifts, because internal fills come first
- charge nurses back on the floor instead of on the phone
- an audit trail for every staffing decision, useful for labor relations and regulators

Incumbents such as UKG/API Healthcare, ShiftWizard and QGenda own the schedule itself. ShiftVoice doesn't replace the schedule. It adds the autonomous loop that runs from call-out to covered.

## How it works

```
 phone / browser voice ──► LLM (Sciforium: DeepSeek or GLM)
                           extracts intent, dates, shift, reason category
                           explains decisions in plain speech
                                   │ structured slots
                                   ▼
                        deterministic rules engine (Python)
   coverage from census and ratios · skill mix · fatigue · leave tiers · blackout · fairness
                                   │ decision + every rule it checked
                                   ▼
             SQLite schedule ◄── backfill loop ──► outbound calls down the ladder
                                   │
                                   ▼
           live dashboard (SSE) + charge nurse notification + audit log
```

**The rules decide and the LLM talks.** The LLM never approves or denies anything. It turns speech into slots and turns the engine's decision back into speech. That split keeps every decision reproducible and explainable to a supervisor. The rules live in [src/config.py](src/config.py), [src/optimization/](src/optimization/) and [src/forecasting/census_forecast.py](src/forecasting/census_forecast.py).

**Where the AI matters.** A nurse at 5 AM doesn't fill out a form. They say "I woke up with a fever, I can't make it today." A nurse being called for a shift asks "what time does it start?" before saying yes. Understanding that and answering naturally needs the LLM. The autonomous loop needs voice and language understanding on both ends:

> call-out → gap detected → ranked call list → call → answer questions → handle the decline → next call → book → notify

### Two flows, deliberately different

- **Unplanned call-outs (sick, emergency, bereavement)** are notifications, not requests. They are always accepted and never questioned, and the agent never asks for medical details (FMLA/ADA, union contracts). If the unit drops below its minimum, a gap opens and backfill starts on its own.
- **Planned leave (vacation, personal)** is checked for notice (14 days), length (over 5 days goes to the supervisor), blackout dates and coverage.
  - Safe requests are auto-approved.
  - Borderline ones go to the nurse manager.
  - Thin days get **alternate dates and swap partners** instead of a flat no, and the nurse can still ask for an exception.

### Escalation ladder for backfill

Approved leave is never revoked. Candidates are called in this order:
1. Float pool and per diem
2. Staff who volunteered for extra shifts, with an incentive of $15/h
3. Agency
4. Last resort: a **voluntary** ask to someone on discretionary leave, with an incentive of $25/h and an explicit "saying no is fine". Anyone on sick or bereavement leave is never contacted.

Within a tier the order is: no overtime first, then rotation (asked least recently), then seniority.

Hard rules applied to every candidate:
- ICU certification
- a charge-certified replacement when the charge nurse is the one out
- 10 hours' minimum rest between shifts
- at most 3 consecutive days
- at most 60 hours in any 7 days

Everyone ruled out is listed on the dashboard with the reason.

## What's real and what's mocked

| Real | Mocked or simulated |
|---|---|
| Rules engine, ranking, cost model, schedule database, audit log | Hospital, staff and schedule are **synthetic** (seeded fresh on start) |
| LLM conversation via Sciforium when a key is set | The nurses the agent *calls* are scripted stand-ins. Switch **Candidates → Live** and a person answers instead |
| Browser voice: Web Speech API speech-to-text (Chrome/Edge) and text-to-speech | No real phone calls in the demo. The Vapi webhook exists but is untested ([docs/vapi.md](docs/vapi.md)) |
| Live dashboard over server-sent events | Census is a deterministic forecast, not an ADT feed. Charge-nurse "notification" is an in-app inbox, not SMS |
| Offline fallback: keyword and date extraction plus templates, used whenever the LLM is down or slow | Business metrics use stated assumptions: ~6 min of phone time per manual call attempt, agency at $135/h |

What would break at real scale:
- SQLite with one connection, and in-memory call state.
- The "demo clock" starts at 5:30 AM.
- There's no EHR or scheduling-system integration yet. Production would read UKG or QGenda schedules and the ADT census.

## Running it

```bash
cp .env.example .env          # add SCIFORIUM_API_KEY and SCIFORIUM_MODEL; optional, it runs offline without them
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
python app.py                 # open http://localhost:8000 in Chrome
pytest                        # 37 tests, including the full demo scenario end to end
```

### Demo script (about 3 minutes)

1. **5:30 AM sick call.** Caller *Maria Chen* → **Call in** → press the mic and say *"I woke up with a fever and I can't make my shift today"*, or click the **Sick call** chip.
   - The agent logs it without questions.
   - The ICU day card turns red (2/3).
   - The backfill card shows the ranked ladder and who was ruled out and why.
2. The agent **calls Jordan Blake (float)**, who declines. Then it **calls Priya Patel (volunteer, +$15/h)**. She asks what time it starts, gets an answer, and accepts.
   - Coverage returns to 3/3.
   - Denise, the charge nurse, gets the notification.
   - The metrics show time to fill and agency spend avoided.
3. **Planned leave on a thin day.** Caller *Devon Wright* → **Thin-day leave** chip. The agent explains that the day is too thin and offers two auto-approvable dates and a swap partner. Click **Take alternate**: it's approved. The **Leave & approvals** tab shows every rule it checked.
4. Optional:
   - **Long vacation** lands in the supervisor queue.
   - **Holiday** hits the Thanksgiving blackout.
   - The **Schedule** tab shows the 28-day coverage heat map.

Switch **Candidates → Live** to have a teammate answer the agent's outbound calls by voice.

## Brought in from before the weekend

None. Everything was written during the event, starting from the empty project structure committed Saturday.
