"""Regression tests for the admin "🔒 Приватная фотосессия" feature.

Background: /admin/api/private-session was accidentally registered on TWO view
functions — api_generate_invoice (first, so it won) and api_private_session
(shadowed, dead code). Clicking "Создать" only made a second Stripe invoice and
returned event_id=undefined; no booking was ever created. Fixed 2026-06-10.

These tests pin down:
1. /admin/api/private-session and /admin/api/generate-invoice are DISTINCT
   endpoints (the duplicate-route regression must not come back).
2. POST /admin/api/private-session creates a real bookings row with the right
   date/time/email/price, persists the payment link, and reuses the atomic
   double-book guard.
3. Validation rejects bad input (missing fields, end<=start).
"""
import os
import tempfile
from pathlib import Path

import pytest
import yaml

import app as booking_app  # noqa: E402


@pytest.fixture()
def admin_client(monkeypatch, tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    # Redirect the events.yaml writer to a throwaway file so the real one is
    # never touched by the suite.
    events_file = tmp_path / "events.yaml"
    events_file.write_text("events: []\n")
    monkeypatch.setattr(booking_app, "_EVENTS_PATH", str(events_file))
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(events_file), raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    sync_client_calls = []

    def _sync_client(*args, **kwargs):
        sync_client_calls.append((args, kwargs))

    monkeypatch.setattr(booking_app, "sync_client", _sync_client, raising=False)
    booking_app._rate_limits.clear()
    booking_app.init_db()
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        c.sync_client_calls = sync_client_calls
        c.events_file = events_file
        yield c
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _hdrs():
    return {"X-Admin-Key": "test-admin-key"}


# ── 1. The duplicate-route regression ─────────────────────────────────────────

def test_private_session_and_generate_invoice_are_distinct_endpoints():
    rules = [r for r in booking_app.app.url_map.iter_rules()
             if r.rule == "/admin/api/private-session"]
    assert len(rules) == 1, "private-session must map to exactly one view function"
    assert rules[0].endpoint == "api_private_session"


# ── 2. Happy path ─────────────────────────────────────────────────────────────

def test_private_session_creates_booking(admin_client):
    payload = {
        "date": "2026-08-15",
        "start_time": "08:20",
        "end_time": "09:20",
        "client_name": "Jane Doe",
        "email": "jane@example.com",
        "instagram": "@jane.doe",
        "price": "275",
        "payment_link": "",  # no link => recorded as paid/confirmed
    }
    resp = admin_client.post("/admin/api/private-session", json=payload, headers=_hdrs())
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["success"] is True
    assert data["event_id"].startswith("private-")
    assert data["booking_id"] > 0
    assert data["booking_url"] == f"/admin/booking/{data['booking_id']}"

    conn = booking_app.db_conn()
    row = conn.execute(
        "SELECT date, time, name, email, instagram, full_price, paid, status, session_type "
        "FROM bookings WHERE id=?", (data["booking_id"],)
    ).fetchone()
    conn.close()
    assert row["date"] == "2026-08-15"
    assert row["time"] == "08:20"
    assert row["email"] == "jane@example.com"
    assert row["instagram"] == "jane.doe"
    assert row["full_price"] == 275.0
    assert row["session_type"] == "private"
    assert row["paid"] == 1 and row["status"] == "confirmed"
    assert admin_client.sync_client_calls[-1][0] == ("jane@example.com", "Jane Doe", "", "jane.doe")

    events_data = yaml.safe_load(Path(admin_client.events_file).read_text()) or {}
    private_event = [e for e in events_data["events"] if e["id"] == data["event_id"]][0]
    assert private_event["title"] == "Individual Photoshoot — Jane Doe"
    assert "individual photoshoot" in private_event["included"][0]
    assert "private session" not in private_event["included"][0].lower()


def test_private_session_with_payment_link_is_unpaid(admin_client):
    payload = {
        "date": "2026-08-16", "start_time": "10:00", "end_time": "11:00",
        "client_name": "Pay Later", "email": "later@example.com",
        "price": "300", "payment_link": "https://buy.stripe.com/test_abc",
    }
    resp = admin_client.post("/admin/api/private-session", json=payload, headers=_hdrs())
    data = resp.get_json()
    assert data["paid"] is False
    conn = booking_app.db_conn()
    row = conn.execute(
        "SELECT paid, status, payment_link FROM bookings WHERE id=?",
        (data["booking_id"],)).fetchone()
    conn.close()
    assert row["paid"] == 0
    assert row["status"] == "reserved"
    assert row["payment_link"] == "https://buy.stripe.com/test_abc"


# ── 3. Guards ─────────────────────────────────────────────────────────────────

def test_private_session_requires_auth(admin_client, monkeypatch):
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "")
    resp = admin_client.post("/admin/api/private-session",
                             json={"date": "2026-08-15"})
    assert resp.status_code == 401


def test_private_session_rejects_end_before_start(admin_client):
    payload = {
        "date": "2026-08-15", "start_time": "15:00", "end_time": "14:00",
        "client_name": "Jane Doe", "email": "jane@example.com", "price": "275",
    }
    resp = admin_client.post("/admin/api/private-session", json=payload, headers=_hdrs())
    assert resp.status_code == 400
    assert "позже" in resp.get_json()["error"]


def test_private_session_rejects_missing_fields(admin_client):
    resp = admin_client.post("/admin/api/private-session",
                             json={"date": "2026-08-15"}, headers=_hdrs())
    assert resp.status_code == 400


def test_private_session_blocks_double_book(admin_client):
    base = {
        "date": "2026-09-01", "start_time": "12:00", "end_time": "13:00",
        "client_name": "First Client", "email": "first@example.com", "price": "300",
    }
    r1 = admin_client.post("/admin/api/private-session", json=base, headers=_hdrs())
    assert r1.status_code == 200
    # Same date+time => UNIQUE(date,time) guard must reject.
    r2 = admin_client.post("/admin/api/private-session",
                           json={**base, "client_name": "Second Client",
                                 "email": "second@example.com"}, headers=_hdrs())
    assert r2.status_code == 409


def test_admin_private_session_modal_is_individual_and_minute_precise():
    html = Path(__file__).resolve().parents[1].joinpath("templates", "admin.html").read_text()
    assert "Индивидуальная фотосессия" in html
    assert "Приватная фотосессия" not in html
    assert 'id="private-start-time" step="60"' in html
    assert 'id="private-end-time" step="60"' in html
    assert 'id="private-instagram"' in html
    assert "private-start-hour" not in html
