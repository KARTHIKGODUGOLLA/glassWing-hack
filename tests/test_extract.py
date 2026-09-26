from datetime import date

from src.agent import extract

TODAY = date(2026, 9, 27)  # a Sunday


def test_sick_callout_today():
    slots = extract.extract_inbound("Hi, it's Maria. I woke up with a fever and I can't make my shift today.", TODAY, {})
    assert slots["intent"] == "callout" and slots["leave_type"] == "sick" and slots["start_date"] == "2026-09-27"


def test_planned_personal_day():
    slots = extract.extract_inbound("Could I take a personal day on Tuesday, October 13?", TODAY, {})
    assert slots["intent"] == "planned_leave" and slots["start_date"] == "2026-10-13" and slots["leave_type"] == "personal"


def test_vacation_range():
    slots = extract.extract_inbound("I'd like vacation from Tuesday, October 27 through Tuesday, November 3.", TODAY, {})
    assert (slots["start_date"], slots["end_date"]) == ("2026-10-27", "2026-11-03")
    assert slots["leave_type"] == "vacation"


def test_weekday_and_tomorrow():
    assert extract.find_dates("tomorrow", TODAY) == [date(2026, 9, 28)]
    assert extract.find_dates("on Friday", TODAY) == [date(2026, 10, 2)]


def test_offer_responses():
    assert extract.classify_offer_response("Sorry, I can't today.") == "declined"
    assert extract.classify_offer_response("Okay, yeah. I'll take it.") == "accepted"
    assert extract.classify_offer_response("Is there an incentive on that one?") == "question"
    assert extract.classify_offer_response("No problem, put me down.") == "accepted"
