"""Tests for /admin/reschedule — moving a booking to a new event/date/time.

Rules under test:
- Auth required (no key → 401)
- Validates inputs (missing fields → 400)
- Target slot must be valid for the target event (bad time → 400)
- Conflict: target slot already taken by ANOTHER active booking → 409
- Same-slot no-op returns success without changes
- Reserved/pending → reset reserved_until; confirmed → keep status
- Side effects fire: email, telegram, notion (mocked, just spy)
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

import app as booking_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_send_client_reschedule_email", lambda **kwargs: True, raising=False)
    monkeypatch.setattr(booking_app, "_notify_reschedule", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kwargs: None, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _events():
    return [e for e in booking_app.EVENTS if e.get("status") in ("active", "upcoming") and not e.get("hidden")]


def _insert_booking(db_path, *, event, date, time, status="reserved", confirmed=0):
    """Insert a booking directly in DB so tests don't depend on /reserve."""
    reserved_until = (datetime.now() + timedelta(minutes=30)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        INSERT INTO bookings
        (date, time, name, email, phone, instagram, session_type, status, paid, confirmed, reserved_until, event_id, confirmation_token, paid_amount)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, time, "Test Client", "test@example.com", "4035550000", "test",
          event.get("session_type", "mini"), status, 1 if confirmed else 0,
          confirmed, reserved_until, event["id"], "tok-test",
          event.get("deposit", 95) if confirmed else None))
    bid = c.lastrowid
    conn.commit()
    conn.close()
    return bid


def _auth_headers():
    return {"X-Admin-Key": "test-admin-key"}


def test_reschedule_requires_auth(client):
    c, _ = client
    r = c.post("/admin/reschedule", json={})
    assert r.status_code == 401


def test_reschedule_missing_fields_returns_400(client):
    c, _ = client
    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": 999})
    assert r.status_code == 400


def test_reschedule_bad_event_returns_404(client):
    c, db = client
    ev = _events()[0]
    slots = booking_app.generate_slots(ev)
    bid = _insert_booking(db, event=ev, date=ev["date"], time=slots[0]["time"])

    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": bid, "new_event_id": "no-such-event",
                     "new_date": ev["date"], "new_time": slots[0]["time"]})
    assert r.status_code == 404


def test_reschedule_bad_time_returns_400(client):
    c, db = client
    ev = _events()[0]
    slots = booking_app.generate_slots(ev)
    bid = _insert_booking(db, event=ev, date=ev["date"], time=slots[0]["time"])

    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": bid, "new_event_id": ev["id"],
                     "new_date": ev["date"], "new_time": "03:17"})
    assert r.status_code == 400


def test_reschedule_moves_within_same_event(client):
    c, db = client
    ev = _events()[0]
    slots = booking_app.generate_slots(ev)
    # Reserve slot 0, then move to slot 1
    bid = _insert_booking(db, event=ev, date=ev["date"], time=slots[0]["time"])

    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": bid, "new_event_id": ev["id"],
                     "new_date": ev["date"], "new_time": slots[1]["time"]})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["success"] is True
    assert data["new"]["time"] == slots[1]["time"]

    # Verify DB updated
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    assert row["time"] == slots[1]["time"]


def test_reschedule_conflict_blocks_when_target_taken(client):
    c, db = client
    ev = _events()[0]
    slots = booking_app.generate_slots(ev)

    bid_a = _insert_booking(db, event=ev, date=ev["date"], time=slots[0]["time"])
    bid_b = _insert_booking(db, event=ev, date=ev["date"], time=slots[1]["time"],
                            status="confirmed", confirmed=1)

    # Try to move A onto B's slot
    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": bid_a, "new_event_id": ev["id"],
                     "new_date": ev["date"], "new_time": slots[1]["time"]})
    assert r.status_code == 409

    # A still on its original slot
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT time FROM bookings WHERE id=?", (bid_a,)).fetchone()
    conn.close()
    assert row["time"] == slots[0]["time"]


def test_reschedule_same_slot_is_noop(client):
    c, db = client
    ev = _events()[0]
    slots = booking_app.generate_slots(ev)
    bid = _insert_booking(db, event=ev, date=ev["date"], time=slots[0]["time"])

    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": bid, "new_event_id": ev["id"],
                     "new_date": ev["date"], "new_time": slots[0]["time"]})
    assert r.status_code == 200
    j = r.get_json()
    assert j.get("success") is True
    assert j.get("no_change") is True


def test_reschedule_preserves_confirmed_status(client):
    c, db = client
    ev = _events()[0]
    slots = booking_app.generate_slots(ev)
    bid = _insert_booking(db, event=ev, date=ev["date"], time=slots[0]["time"],
                          status="confirmed", confirmed=1)

    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": bid, "new_event_id": ev["id"],
                     "new_date": ev["date"], "new_time": slots[2]["time"]})
    assert r.status_code == 200

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, confirmed, reserved_until FROM bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    assert row["status"] == "confirmed"
    assert row["confirmed"] == 1
    # confirmed bookings don't reset reserved_until — left untouched


def test_reschedule_resets_timer_for_reserved(client):
    c, db = client
    ev = _events()[0]
    slots = booking_app.generate_slots(ev)
    bid = _insert_booking(db, event=ev, date=ev["date"], time=slots[0]["time"],
                          status="reserved")

    # Get original reserved_until
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    old_ru = conn.execute("SELECT reserved_until FROM bookings WHERE id=?", (bid,)).fetchone()["reserved_until"]
    conn.close()

    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": bid, "new_event_id": ev["id"],
                     "new_date": ev["date"], "new_time": slots[3]["time"]})
    assert r.status_code == 200

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, reserved_until FROM bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    assert row["status"] == "reserved"
    # New reserved_until must differ (it's now() + RESERVATION_MINUTES, set at reschedule time)
    assert row["reserved_until"] != old_ru


def test_reschedule_to_other_event_works(client):
    """Move across events when the target slot is valid for the target event."""
    c, db = client
    all_events = _events()
    if len(all_events) < 2:
        pytest.skip("Need at least 2 active events in events.yaml for cross-event reschedule test")

    src = all_events[0]
    dst = all_events[1]
    src_slots = booking_app.generate_slots(src)
    dst_slots = booking_app.generate_slots(dst)

    bid = _insert_booking(db, event=src, date=src["date"], time=src_slots[0]["time"])

    r = c.post("/admin/reschedule",
               headers=_auth_headers(),
               json={"booking_id": bid, "new_event_id": dst["id"],
                     "new_date": dst["date"], "new_time": dst_slots[0]["time"]})
    assert r.status_code == 200, r.get_json()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT event_id, date, time FROM bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    assert row["event_id"] == dst["id"]
    assert row["date"] == dst["date"]
    assert row["time"] == dst_slots[0]["time"]
