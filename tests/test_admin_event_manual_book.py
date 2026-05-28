"""Regression tests for the admin "block this slot manually" feature
(2026-05-21). Iryna asked for a way to open an event in admin and mark a slot
as already-taken/paid for walk-in clients who didn't go through the public
booking flow. Three things have to stay true:

1. The slot grid page (`/admin/event/<id>`) renders for admins, redirects to
   /admin/login for anyone else, and 404s for unknown event ids.
2. The slot-state JSON API returns every slot the event generates plus the
   booking row that covers each slot (free / pending / confirmed).
3. POST /admin/api/event/<id>/manual-book inserts a real bookings row with
   the right status and amounts, reuses the same atomic conflict guard as
   /reserve (so it can't double-book), bypasses reCAPTCHA + rate-limit
   (admin-only path), and rejects garbage input.
"""
import os
import tempfile

import pytest

import app as booking_app  # noqa: E402


@pytest.fixture()
def admin_client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kw: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kw: None, raising=False)
    monkeypatch.setattr(booking_app, "send_confirmation_email", lambda booking_id: True, raising=False)
    monkeypatch.setattr(booking_app, "_send_email_with_attachment", lambda *a, **k: True, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()

    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        yield c
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _hdrs():
    return {"X-Admin-Key": "test-admin-key"}


def _first_event():
    active = [e for e in booking_app.EVENTS
              if e.get("status") in ("active", "upcoming")
              and not e.get("hidden")]
    assert active, "events.yaml needs at least one active/upcoming event for these tests"
    return active[0]


# ── 1. Slot grid page ────────────────────────────────────────────────────────

def test_admin_event_page_renders_for_admin(admin_client):
    """GET /admin/event/<id> with admin auth must render the slot template
    (not an inline stub or the dashboard)."""
    ev = _first_event()
    resp = admin_client.get(f"/admin/event/{ev['id']}", headers=_hdrs())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Hallmarks of templates/admin_event.html, not the generic dashboard.
    assert "Block slot" in body
    assert "id=\"slotGrid\"" in body
    assert ev["title"] in body


def test_admin_event_page_returns_404_for_unknown_id(admin_client):
    resp = admin_client.get("/admin/event/nope-no-such-event", headers=_hdrs())
    assert resp.status_code == 404


def test_admin_event_page_requires_admin(admin_client, monkeypatch):
    """Without admin auth the HTML page bounces to /admin/login (consistent
    with /admin/clients, /admin/booking/<id> behaviour)."""
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key", raising=False)
    ev = _first_event()
    resp = admin_client.get(f"/admin/event/{ev['id']}")
    assert resp.status_code in (302, 303)
    assert "/admin/login" in resp.headers.get("Location", "")


# ── 2. Slot-state JSON API ───────────────────────────────────────────────────

def test_admin_event_slots_api_returns_all_generated_slots(admin_client):
    """Every slot generate_slots() would produce must appear in the API
    response, even if none are booked — that's what populates the UI grid."""
    ev = _first_event()
    expected = booking_app.generate_slots(ev)
    assert expected, "event must generate at least one slot"

    resp = admin_client.get(f"/admin/api/event/{ev['id']}/slots", headers=_hdrs())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["event"]["id"] == ev["id"]
    times = [s["time"] for s in body["slots"]]
    assert times == [s["time"] for s in expected]
    # Fresh DB → every slot is free.
    assert all(s["state"] == "free" for s in body["slots"])


def test_admin_event_slots_api_marks_taken_slots(admin_client):
    """After a manual block + a public reservation the API must reflect
    confirmed vs pending status accurately."""
    ev = _first_event()
    times = [s["time"] for s in booking_app.generate_slots(ev)]
    assert len(times) >= 2

    # Manual confirmed block on slot[0]
    r1 = admin_client.post(
        f"/admin/api/event/{ev['id']}/manual-book",
        headers=_hdrs(),
        json={"time": times[0], "name": "Walk-In One", "mark_paid": True},
    )
    assert r1.status_code == 200, r1.get_data(as_text=True)

    # Public reservation (pending) on slot[1]
    conn = booking_app.db_conn()
    from datetime import datetime, timedelta
    conn.execute(
        """INSERT INTO bookings (date,time,name,email,phone,instagram,session_type,
                                 status,reserved_until,event_id,deposit_amount,full_price,
                                 confirmed,paid)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ev["date"], times[1], "Public Pending", "p@example.com", "", "", "mini",
         "reserved", (datetime.now() + timedelta(minutes=15)).isoformat(),
         ev["id"], 0, 0, 0, 0),
    )
    conn.commit()
    conn.close()

    body = admin_client.get(
        f"/admin/api/event/{ev['id']}/slots", headers=_hdrs()
    ).get_json()
    state_for = {s["time"]: s["state"] for s in body["slots"]}
    assert state_for[times[0]] == "confirmed"
    assert state_for[times[1]] == "pending"


# ── 3. Manual booking POST ───────────────────────────────────────────────────

def test_manual_book_creates_confirmed_booking_when_mark_paid_true(admin_client):
    """The happy path: admin types a name, ticks 'mark as paid', and a
    confirmed booking lands in the DB with paid_amount = full_price."""
    ev = _first_event()
    slot_time = booking_app.generate_slots(ev)[0]["time"]
    resp = admin_client.post(
        f"/admin/api/event/{ev['id']}/manual-book",
        headers=_hdrs(),
        json={"time": slot_time, "name": "Walk-In Client", "mark_paid": True},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is True
    assert body["status"] == "confirmed"
    assert body["paid_amount"] > 0

    conn = booking_app.db_conn()
    row = conn.execute(
        "SELECT name, status, confirmed, paid, paid_amount, event_id FROM bookings WHERE id=?",
        (body["booking_id"],),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["name"] == "Walk-In Client"
    assert row["status"] == "confirmed"
    assert row["confirmed"] == 1
    assert row["paid"] == 1
    assert row["event_id"] == ev["id"]


def test_manual_book_creates_reserved_when_mark_paid_false(admin_client):
    """If admin doesn't tick 'paid', booking is just held — useful when a
    client said they'll pay later but Iryna wants to block the slot now."""
    ev = _first_event()
    slot_time = booking_app.generate_slots(ev)[0]["time"]
    resp = admin_client.post(
        f"/admin/api/event/{ev['id']}/manual-book",
        headers=_hdrs(),
        json={"time": slot_time, "name": "Tentative Walk-In", "mark_paid": False},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "reserved"
    assert body["paid_amount"] == 0


def test_manual_book_rejects_taken_slot_with_409(admin_client):
    """Same atomic guard as /reserve — admin can't accidentally overwrite
    an existing active booking on the same date/time."""
    ev = _first_event()
    slot_time = booking_app.generate_slots(ev)[0]["time"]
    # First block — should succeed.
    r1 = admin_client.post(
        f"/admin/api/event/{ev['id']}/manual-book",
        headers=_hdrs(),
        json={"time": slot_time, "name": "First", "mark_paid": True},
    )
    assert r1.status_code == 200
    # Second block on same slot — should 409.
    r2 = admin_client.post(
        f"/admin/api/event/{ev['id']}/manual-book",
        headers=_hdrs(),
        json={"time": slot_time, "name": "Second", "mark_paid": True},
    )
    assert r2.status_code == 409
    assert "taken" in r2.get_json().get("error", "").lower()


def test_manual_book_rejects_invalid_slot_time(admin_client):
    """The time must come from generate_slots() for this event — random
    strings get a 400, not a silently-orphaned row."""
    ev = _first_event()
    resp = admin_client.post(
        f"/admin/api/event/{ev['id']}/manual-book",
        headers=_hdrs(),
        json={"time": "99:99", "name": "Bad", "mark_paid": True},
    )
    assert resp.status_code == 400


def test_manual_book_rejects_empty_name(admin_client):
    ev = _first_event()
    slot_time = booking_app.generate_slots(ev)[0]["time"]
    resp = admin_client.post(
        f"/admin/api/event/{ev['id']}/manual-book",
        headers=_hdrs(),
        json={"time": slot_time, "name": "", "mark_paid": True},
    )
    assert resp.status_code == 400
    assert "name" in resp.get_json().get("error", "").lower()


def test_manual_book_requires_admin(admin_client, monkeypatch):
    """No admin auth → 401 JSON (XHR path, not redirect)."""
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key", raising=False)
    ev = _first_event()
    slot_time = booking_app.generate_slots(ev)[0]["time"]
    resp = admin_client.post(
        f"/admin/api/event/{ev['id']}/manual-book",
        json={"time": slot_time, "name": "Hack"},
    )
    assert resp.status_code == 401


def test_manual_book_unknown_event_returns_404(admin_client):
    resp = admin_client.post(
        "/admin/api/event/no-such-event-xyz/manual-book",
        headers=_hdrs(),
        json={"time": "10:00", "name": "X"},
    )
    assert resp.status_code == 404
