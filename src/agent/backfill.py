"""The autonomous backfill loop: rank, call down the ladder, fill, notify."""
from __future__ import annotations

import asyncio
import logging

from src import clock, db, events
from src.optimization import eligibility, leave_policy
from src.optimization.staffing_solver import rank_candidates, shift_label
from src.voice import call_agent, session

log = logging.getLogger("shiftvoice.backfill")
_tasks: dict[int, asyncio.Task] = {}


def schedule(gap_id: int) -> None:
    """Start the loop for a gap. Safe to call from any thread."""
    loop = events._loop
    if loop is None or loop.is_closed():
        return
    loop.call_soon_threadsafe(_start, gap_id)


def _start(gap_id: int) -> None:
    task = _tasks.get(gap_id)
    if task and not task.done():
        return
    _tasks[gap_id] = asyncio.get_running_loop().create_task(run(gap_id))


def cancel_all() -> None:
    for task in _tasks.values():
        task.cancel()
    _tasks.clear()


def rank_and_queue(gap_id: int) -> list[dict]:
    gap = db.one("SELECT * FROM gaps WHERE id=?", (gap_id,))
    shift = db.one("SELECT * FROM shifts WHERE id=?", (gap["shift_id"],))
    ranked, excluded = rank_candidates(shift, needs_charge=bool(gap["needs_charge"]))
    db.execute("DELETE FROM outreach WHERE gap_id=? AND status='queued'", (gap_id,))
    for i, c in enumerate(ranked, start=1):
        db.execute(
            """INSERT INTO outreach (gap_id, staff_id, rank, tier, status, incentive, est_cost, overtime, reasons)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (gap_id, c["staff"]["id"], i, c["tier"], c["cost"]["incentive"], c["cost"]["total"],
             int(c["eligibility"].overtime_hours > 0), db.dumps(c["reasons"])),
        )
    db.set_meta(f"excluded:{gap_id}", db.dumps([
        {"staff_id": e["staff"]["id"], "name": e["staff"]["name"], "reasons": e["reasons"]} for e in excluded]))
    top = ranked[0]["staff"]["name"] if ranked else "nobody"
    events.log("backfill.ranked",
               f"{shift_label(shift)}: {len(ranked)} eligible, {len(excluded)} ruled out by policy. First call: {top}.",
               {"gap_id": gap_id})
    return db.query("SELECT * FROM outreach WHERE gap_id=? ORDER BY rank", (gap_id,))


async def run(gap_id: int) -> str:
    try:
        db.execute("UPDATE gaps SET status='filling' WHERE id=?", (gap_id,))
        queue = rank_and_queue(gap_id)
        await session.pause(1.5)
        for o in queue:
            gap = db.one("SELECT * FROM gaps WHERE id=?", (gap_id,))
            if gap["status"] == "filled":
                break
            person = db.one("SELECT * FROM staff WHERE id=?", (o["staff_id"],))
            shift = db.one("SELECT * FROM shifts WHERE id=?", (gap["shift_id"],))
            recheck = eligibility.check(person, shift, needs_charge=bool(gap["needs_charge"]), voluntary_leave_ask=o["tier"] == 4)
            if not recheck.eligible:
                db.execute("UPDATE outreach SET status='skipped' WHERE id=?", (o["id"],))
                continue
            db.execute("UPDATE outreach SET status='calling', started_at=? WHERE id=?", (clock.stamp(), o["id"]))
            events.push("outreach", {"gap_id": gap_id})
            outcome = await call_agent.run_offer_call(o)
            db.execute("UPDATE outreach SET status=?, ended_at=? WHERE id=?", (outcome, clock.stamp(), o["id"]))
            db.execute("UPDATE staff SET last_asked_at=? WHERE id=?", (clock.stamp(), person["id"]))
            events.log(f"outreach.{outcome}", f"{person['name']}: {outcome.replace('_', ' ')}", {"gap_id": gap_id})
            if outcome == "accepted":
                break
            await session.pause(1.0)

        gap = db.one("SELECT * FROM gaps WHERE id=?", (gap_id,))
        db.execute("UPDATE outreach SET status='not_needed' WHERE gap_id=? AND status='queued'", (gap_id,))
        if gap["status"] != "filled":
            db.execute("UPDATE gaps SET status='escalated' WHERE id=?", (gap_id,))
            shift = db.one("SELECT * FROM shifts WHERE id=?", (gap["shift_id"],))
            leave_policy.notify("Nurse Manager", "escalation",
                                f"Could not fill {shift_label(shift)} after working the full ladder. Needs a human decision.")
            return "escalated"
        return "filled"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # keep the dashboard alive; surface the problem
        log.exception("backfill failed")
        events.log("error", f"Backfill for gap {gap_id} stopped: {exc}", {"gap_id": gap_id})
        return "error"
    finally:
        events.push("outreach", {"gap_id": gap_id})
