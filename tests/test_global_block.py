"""Global cross-event slot blocking tests.

One photographer cannot be in two places at once. A booking in any event
blocks that date+time across all other events.
"""
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as booking_app


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    shared_date = (date.today() + timedelta(days=7)).isoformat()

    events = [
        {
            "id": "qa-mini",
            "title": "QA Mini Session",
            "subtitle": "Fixed slots",
            "date": shared_date,
            "start_time": "10:00",
            "end_time": "12:00",
            "session_length": 30,
            "break_length": 0,
            "slot_interval": 30,
            "deposit": 95,
            "full_price": 190,
            "location": "Calgary",
            "session_type": "mini",
            "booking_type": "fixed_slots",
            "status": "upcoming",
            "photos": ["/static/images/placeholder.jpg"],
        },
        {
            "id": "qa-individual",
            "title": "QA Individual Photoshoot",
            "subtitle": "Rolling availability",
            "date": shared_date,
            "start_time": "10:00",
            "end_time": "12:00",
            "session_length": 30,
            "break_length": 0,
            "slot_interval": 30,
            "deposit": 150,
            "full_price": 300,
            "location": "Calgary",
            "session_type": "individual",
            "booking_type": "rolling_availability",
            "availability_horizon_days": 60,
            "blackout_dates": [],
            "status": "upcoming",
            "photos": ["/static/images/placeholder.jpg"],
        },
        {
            "id": "qa-individual-2",
            "title": "QA Individual Photoshoot 2",
            "subtitle": "Another rolling event same day",
            "date": shared_date,
            "start_time": "10:00",
            "end_time": "12:00",
            "session_length": 30,
            "break_length": 0,
            "slot_interval": 30,
            "deposit": 150,
            "full_price": 300,
            "location": "Calgary",
            "session_type": "individual",
            "booking_type": "rolling_availability",
            "availability_horizon_days": 60,
            "blackout_dates": [],
            "status": "upcoming",
            "photos": ["/static/images/placeholder.jpg"],
        },
    ]

    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "EVENTS", events)
    monkeypatch.setattr(booking_app, "SETTINGS", {"photographer_instagram": "@pashynska.photo", "photographer_instagram_url": "https://instagram.com/pashynska.photo"})
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kwargs: None, raising=False)
    booking_app._rate_limits.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path, events

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _reserve(c, event_id, day, time="10:00", email="qa@example.com"):
    return c.post("/reserve", json={
        "event_id": event_id,
        "date": day,
        "time": time,
        "name": "QA Client",
        "email": email,
        "phone": "4035550000",
        "instagram": "@qa",
    })


def test_mini_booking_blocks_individual_same_time(client):
    """Booking a mini session slot should block that time in individual event."""
    c, _, events = client
    mini = events[0]
    individual = events[1]
    shared_date = mini["date"]

    # Book 10:00 in mini
    r = _reserve(c, mini["id"], shared_date, "10:00", "mini@test.com")
    assert r.status_code == 200, r.get_json()

    # Individual slots for same date should show 10:00 as booked
    slots = c.get(f"/slots/{shared_date}?event_id={individual['id']}").get_json()
    assert slots["booking_type"] == "rolling_availability"
    times = [s["time"] for s in slots["slots"]]
    assert "10:00" not in times
    assert "10:30" in times
    assert "11:00" in times
    assert "11:30" in times

    # Reserve 10:00 in individual should fail
    r2 = _reserve(c, individual["id"], shared_date, "10:00", "individual@test.com")
    assert r2.status_code in (400, 409, 200)  # backend currently returns 200 with error JSON
    assert r2.get_json()["success"] is False
    assert "reserved" in r2.get_json()["error"].lower() or "taken" in r2.get_json()["error"].lower()


def test_individual_booking_blocks_mini_same_time(client):
    """Booking an individual slot should block that time in mini event."""
    c, _, events = client
    mini = events[0]
    individual = events[1]
    shared_date = mini["date"]

    # Book 10:00 in individual
    r = _reserve(c, individual["id"], shared_date, "10:00", "individual@test.com")
    assert r.status_code == 200, r.get_json()

    # Mini slots for same date should show 10:00 as booked
    slots = c.get(f"/slots/{shared_date}?event_id={mini['id']}").get_json()
    times = [s["time"] for s in slots["slots"]]
    assert "10:00" not in times
    assert "10:30" in times
    assert "11:00" in times
    assert "11:30" in times

    # Reserve 10:00 in mini should fail
    r2 = _reserve(c, mini["id"], shared_date, "10:00", "mini@test.com")
    assert r2.get_json()["success"] is False
    assert "reserved" in r2.get_json()["error"].lower() or "taken" in r2.get_json()["error"].lower()


def test_individual_individual_conflict_same_time(client):
    """Two individual events on the same day block each other."""
    c, _, events = client
    ind1 = events[1]
    ind2 = events[2]
    shared_date = ind1["date"]

    # Book 11:00 in individual-1
    r = _reserve(c, ind1["id"], shared_date, "11:00", "ind1@test.com")
    assert r.status_code == 200, r.get_json()

    # Individual-2 should see 11:00 blocked
    slots = c.get(f"/slots/{shared_date}?event_id={ind2['id']}").get_json()
    times = [s["time"] for s in slots["slots"]]
    assert "11:00" not in times

    # Reserve 11:00 in individual-2 should fail
    r2 = _reserve(c, ind2["id"], shared_date, "11:00", "ind2@test.com")
    assert r2.get_json()["success"] is False


def test_different_times_no_conflict(client):
    """Different times on the same day should not block each other."""
    c, _, events = client
    mini = events[0]
    individual = events[1]
    shared_date = mini["date"]

    # Book 10:00 in mini
    r = _reserve(c, mini["id"], shared_date, "10:00", "mini@test.com")
    assert r.status_code == 200, r.get_json()

    # Individual should still be able to book 10:30
    slots = c.get(f"/slots/{shared_date}?event_id={individual['id']}").get_json()
    assert "10:30" in [s["time"] for s in slots["slots"]]

    r2 = _reserve(c, individual["id"], shared_date, "10:30", "individual@test.com")
    assert r2.status_code == 200, r2.get_json()
    assert r2.get_json()["success"] is True


def test_slots_shows_sold_out_when_globally_blocked(client):
    """When every slot is blocked by another event, /slots should indicate global conflict."""
    c, _, events = client
    mini = events[0]
    individual = events[1]
    shared_date = mini["date"]

    # Book all mini slots (10:00, 10:30, 11:00, 11:30)
    for i, time in enumerate(["10:00", "10:30", "11:00", "11:30"]):
        r = _reserve(c, mini["id"], shared_date, time, f"mini{i}@test.com")
        assert r.status_code == 200, r.get_json()

    # Individual should see no slots, foreign_booked=true, and a sold-out message
    slots = c.get(f"/slots/{shared_date}?event_id={individual['id']}").get_json()
    assert slots["slots"] == []
    assert slots["available"] == 0
    assert slots["foreign_booked"] is True
    assert slots["sold_out_message"] is not None
    assert "DM" in slots["sold_out_message"] or "taken" in slots["sold_out_message"]
