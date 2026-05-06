"""Booking flow regression tests — v1.1 (auto e-Transfer checker).

Tests the full client flow: reserve → confirm → booking-status polling.
Uses a temp SQLite database and mocks external services.
"""
import os
import tempfile
import pytest

import app as booking_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    old_db = booking_app.DB_PATH
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _first_slot(client_tuple):
    """Get the first available slot time from /slots/<date>."""
    c, _ = client_tuple
    # Get date from the first active event
    active_events = [e for e in booking_app.EVENTS if e.get("status") in ("active", "upcoming")]
    if active_events:
        date = active_events[0]["date"]
    else:
        date = "2026-05-03"
    resp = c.get(f"/slots/{date}").get_json()
    # Available slots are in "slots" list (booked ones are excluded)
    slots = resp.get("slots", [])
    return slots[0]["time"] if slots else "10:00", date


def test_reserve_and_confirm_flow(client):
    c, db_path = client_tuple = client
    slot_time, date = _first_slot(client_tuple)

    # Step 1: Reserve slot
    reserve = c.post("/reserve", json={"time": slot_time})
    assert reserve.status_code == 200
    data = reserve.get_json()
    assert data["success"] is True
    booking_id = data["booking_id"]
    assert booking_id is not None

    # Step 2: Confirm with client details (like "I've Sent Payment")
    confirm = c.post("/confirm", json={
        "time": slot_time,
        "name": "Test Client",
        "email": "test@example.com",
        "phone": "4035550000",
        "instagram": "@test",
    })
    assert confirm.status_code == 200
    confirm_data = confirm.get_json()
    assert confirm_data["success"] is True

    # Step 3: Check booking-status API
    status = c.get(f"/booking-status?booking_id={booking_id}").get_json()
    assert status["status"] == "pending_payment"
    assert status["confirmed"] is False

    # Step 4: Admin confirms
    admin = c.post("/admin/confirm", json={
        "booking_id": booking_id,
        "paid_amount": 95.00,
    })
    assert admin.status_code == 200

    # Step 5: Booking should now be confirmed
    status2 = c.get(f"/booking-status?booking_id={booking_id}").get_json()
    assert status2["status"] == "confirmed"
    assert status2["confirmed"] is True
    assert status2["paid"] is True
    assert status2["paid_amount"] == 95.00


def test_reserve_hides_slot(client):
    c, db_path = client_tuple = client
    slot_time, date = _first_slot(client_tuple)

    # Reserve
    c.post("/reserve", json={"time": slot_time})

    # Slot should not be available
    slots = c.get(f"/slots/{date}").get_json()
    slot_times = [s["time"] for s in slots.get("slots", [])]
    assert slot_time not in slot_times


def test_cancel_frees_slot(client):
    c, db_path = client_tuple = client
    slot_time, date = _first_slot(client_tuple)

    # Reserve
    resp = c.post("/reserve", json={"time": slot_time})
    booking_id = resp.get_json()["booking_id"]

    # Cancel
    cancel = c.post("/admin/cancel", json={"booking_id": booking_id})
    assert cancel.status_code == 200

    # Slot should be available again
    slots = c.get(f"/slots/{date}").get_json()
    slot_times = [s["time"] for s in slots.get("slots", [])]
    assert slot_time in slot_times

    # Can reserve again — gets a new booking ID
    resp2 = c.post("/reserve", json={"time": slot_time})
    assert resp2.status_code == 200
    new_data = resp2.get_json()
    assert new_data["success"] is True
    assert new_data["booking_id"] != booking_id


def test_booking_status_not_found(client):
    c, _ = client
    resp = c.get("/booking-status?booking_id=99999")
    assert resp.status_code == 404


def test_booking_status_shows_paid_amount(client):
    c, db_path = client_tuple = client
    slot_time, date = _first_slot(client_tuple)

    # Full flow
    resp = c.post("/reserve", json={"time": slot_time})
    booking_id = resp.get_json()["booking_id"]

    c.post("/confirm", json={
        "time": slot_time,
        "name": "Amount Test",
        "email": "amount@test.com",
    })

    # Auto-confirm with specific amount (simulating e-Transfer)
    c.post("/admin/confirm", json={
        "booking_id": booking_id,
        "paid_amount": 97.50,
    })

    status = c.get(f"/booking-status?booking_id={booking_id}").get_json()
    assert status["paid_amount"] == 97.50
    assert status["status"] == "confirmed"
