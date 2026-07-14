"""Booking type behavior tests.

Admin chooses the event booking type; clients follow the event's configured flow.
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

    today = date.today()
    events = [
        {
            "id": "qa-mini",
            "title": "QA Mini Session",
            "subtitle": "Fixed slots",
            "date": (today + timedelta(days=7)).isoformat(),
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "break_length": 10,
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
            "date": today.isoformat(),
            "start_time": "10:00",
            "end_time": "12:00",
            "session_length": 60,
            "break_length": 0,
            "slot_interval": 60,
            "deposit": 150,
            "full_price": 300,
            "location": "Calgary",
            "session_type": "individual",
            "booking_type": "rolling_availability",
            "availability_horizon_days": 60,
            "blackout_dates": [(today + timedelta(days=3)).isoformat()],
            "status": "upcoming",
            "photos": ["/static/images/placeholder.jpg"],
        },
        {
            "id": "qa-wedding",
            "title": "QA Wedding Photography",
            "subtitle": "Custom packages",
            "date": today.isoformat(),
            "start_time": "10:00",
            "end_time": "12:00",
            "session_length": 60,
            "break_length": 0,
            "slot_interval": 60,
            "deposit": 0,
            "full_price": 0,
            "location": "Calgary",
            "session_type": "wedding",
            "booking_type": "inquiry_only",
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


def test_mini_session_keeps_fixed_event_date_slots(client):
    c, _, events = client
    mini = events[0]

    slots = c.get(f"/slots/{mini['date']}?event_id={mini['id']}")

    assert slots.status_code == 200
    data = slots.get_json()
    assert data["booking_type"] == "fixed_slots"
    assert data["date"] == mini["date"]
    assert [s["time"] for s in data["slots"]] == ["10:00", "10:30"]


def test_paid_mini_landing_matches_summer_ad_and_shows_bookable_date_first(client):
    c, _, _ = client

    response = c.get(
        "/book?type=mini&utm_source=meta&utm_medium=paid"
        "&utm_campaign=summer_minis_2026&utm_content=cr_summer_heart_v6"
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Hold on to" in html
    assert "this summer" in html
    assert "July &amp; August dates from $180 + GST" in html
    assert 'id="featured-session"' in html
    assert "QA Mini Session" in html
    assert html.index('id="featured-session"') < html.index('class="how"')
    assert "150+ client reviews" in html
    assert '<meta name="description"' in html
    assert 'rel="preload" as="image"' in html
    assert 'class="featured-photo"' in html
    assert 'fetchpriority="high"' in html
    assert 'class="photo-img js-lazy-photo"' in html
    assert 'data-src="/static/images/placeholder.jpg"' in html
    assert 'loading="lazy"' in html
    assert "IntersectionObserver" in html
    assert '<main id="main-content">' in html
    assert "Ask in WhatsApp" in html
    assert "trackLandingEvent('whatsapp_click'" in html
    assert "trackLandingEvent('page_view'" in html
    assert "fetch('/track'" in html


def test_individual_uses_requested_calendar_date_and_one_booking_does_not_sell_out_event(client):
    c, _, events = client
    individual = events[1]
    day_one = (date.today() + timedelta(days=1)).isoformat()
    day_two = (date.today() + timedelta(days=2)).isoformat()

    reserve = _reserve(c, individual["id"], day_one, "10:00", "individual-one@test.com")
    assert reserve.status_code == 200, reserve.get_json()

    same_day_slots = c.get(f"/slots/{day_one}?event_id={individual['id']}").get_json()
    assert [s["time"] for s in same_day_slots["slots"]] == ["11:00"]

    another_day_slots = c.get(f"/slots/{day_two}?event_id={individual['id']}").get_json()
    assert [s["time"] for s in another_day_slots["slots"]] == ["10:00", "11:00"]

    events_payload = c.get("/events").get_json()["events"]
    individual_card = next(e for e in events_payload if e["id"] == individual["id"])
    assert individual_card["booking_type"] == "rolling_availability"
    assert individual_card["spots_left"] > 0


def test_individual_rejects_blackout_and_too_far_dates(client):
    c, _, events = client
    individual = events[1]
    blackout = individual["blackout_dates"][0]
    too_far = (date.today() + timedelta(days=90)).isoformat()

    blackout_slots = c.get(f"/slots/{blackout}?event_id={individual['id']}").get_json()
    assert blackout_slots["slots"] == []
    assert blackout_slots["unavailable_reason"] == "blackout"

    blackout_reserve = _reserve(c, individual["id"], blackout, "10:00", "blackout@test.com")
    assert blackout_reserve.status_code == 400
    assert "not available" in blackout_reserve.get_json()["error"]

    far_slots = c.get(f"/slots/{too_far}?event_id={individual['id']}").get_json()
    assert far_slots["slots"] == []
    assert far_slots["unavailable_reason"] == "outside_horizon"


def test_wedding_is_inquiry_only_not_reservable(client):
    c, _, events = client
    wedding = events[2]

    slots = c.get(f"/slots/{date.today().isoformat()}?event_id={wedding['id']}").get_json()
    assert slots["booking_type"] == "inquiry_only"
    assert slots["slots"] == []
    assert slots["inquiry_only"] is True

    reserve = _reserve(c, wedding["id"], date.today().isoformat(), "10:00", "wedding@test.com")
    assert reserve.status_code == 400
    assert "inquiry" in reserve.get_json()["error"].lower()


def test_admin_create_and_update_preserve_admin_selected_booking_type(client, tmp_path, monkeypatch):
    c, _, events = client
    yaml_path = tmp_path / "events.yaml"
    yaml_path.write_text("settings: {}\nevents: []\n", encoding="utf-8")
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(yaml_path))
    monkeypatch.setattr(booking_app, "_EVENTS_PATH", str(yaml_path))

    create = c.post("/admin/events/create", headers={"X-Admin-Key": "test-admin-key"}, json={
        "title": "Admin Individual",
        "date": date.today().isoformat(),
        "booking_type": "rolling_availability",
        "session_type": "individual",
        "availability_horizon_days": 75,
        "blackout_dates": ["2030-01-01", "2030-01-02"],
        "start_time": "09:00",
        "end_time": "12:00",
        "session_length": 60,
        "break_length": 0,
        "deposit": 150,
        "full_price": 300,
        "status": "upcoming",
    })
    assert create.status_code == 200, create.get_json()
    event_id = create.get_json()["event_id"]
    created = booking_app.get_event_by_id(event_id)
    assert created["booking_type"] == "rolling_availability"
    assert created["session_type"] == "individual"
    assert created["availability_horizon_days"] == 75
    assert created["blackout_dates"] == ["2030-01-01", "2030-01-02"]

    update = c.post(f"/admin/events/{event_id}/update", headers={"X-Admin-Key": "test-admin-key"}, json={
        "booking_type": "inquiry_only",
        "session_type": "wedding",
        "availability_horizon_days": 30,
        "blackout_dates": ["2030-02-01"],
    })
    assert update.status_code == 200, update.get_json()
    updated = booking_app.get_event_by_id(event_id)
    assert updated["booking_type"] == "inquiry_only"
    assert updated["session_type"] == "wedding"
    assert updated["availability_horizon_days"] == 30
    assert updated["blackout_dates"] == ["2030-02-01"]
