import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, db, seed  # noqa: E402
from src.voice import session  # noqa: E402

DEMO_DAY = date(2026, 9, 27)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Tests never call the network: force the offline extractor and templates."""
    monkeypatch.setattr(config, "SCIFORIUM_API_KEY", "")
    monkeypatch.setattr(config, "DB_PATH", Path(":memory:"))
    monkeypatch.setattr(config, "demo_date", lambda: DEMO_DAY)
    session.SETTINGS.update(candidate_mode="simulated", pace=0.0)


@pytest.fixture
def demo():
    db.connect(":memory:")
    return seed.seed(DEMO_DAY)


def shift(unit: str, day: str, kind: str) -> dict:
    return db.one("SELECT * FROM shifts WHERE unit_id=? AND date=? AND kind=?", (unit, day, kind))


def staff_named(name: str) -> dict:
    return db.one("SELECT * FROM staff WHERE name=?", (name,))
