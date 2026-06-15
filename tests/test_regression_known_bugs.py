"""Regression tests for all known bugs fixed in the booking system.

Each test guards against a specific bug that was found and fixed.
If any of these tests fail, the bug has reappeared.

Bug history:
  BUG-001: Email trailing punctuation causes SMTP 553 rejection
  BUG-002: _send_client_email doesn't return success/failure
  BUG-003: client_notes column 'note' vs 'text' mismatch crashes API
  BUG-004: Maps card missing when location_url not set
  BUG-005: Duplicate maps card code block overwrites auto-generated URL
  BUG-006: Hardcoded $95 in confirmBooking button
  BUG-007: Server /admin/confirm falls back to SESSION_PRICE ignoring deposit_amount
  BUG-008: Admin client detail shows white screen on API error
  BUG-009: admin_confirm Telegram message always says "sent" even on failure
"""

import json, os, re, sqlite3, tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import app as booking_app
import assistant_engine


# ── Same fixture as test_booking_flow.py ──────────────────────────────

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
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "send_confirmation_email", lambda booking_id: True, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app._assistant_attempts.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _headers():
    return {"X-Admin-Key": "test-admin-key"}


def _first_event():
    active = [e for e in booking_app.EVENTS if e.get("status") in ("active", "upcoming") and not e.get("hidden")]
    assert active, "No active events configured"
    return active[0]


def _first_slot(client_tuple):
    c, _ = client_tuple
    ev = _first_event()
    resp = c.get(f"/slots/{ev['date']}?event_id={ev['id']}")
    assert resp.status_code == 200
    slots = resp.get_json().get("slots", [])
    assert slots, f"No slots for event {ev['id']}"
    return slots[0]["time"], ev["date"], ev["id"]


def _reserve(c, slot_time, event_id, *, name="Regression Test", email="regression@test.com"):
    return c.post("/reserve", json={
        "event_id": event_id,
        "time": slot_time,
        "name": name,
        "email": email,
        "phone": "4035550000",
        "instagram": "@test",
        "terms_accepted": True,
        "agreement_name": name,
        "marketing_consent": "no",
    })


# ═══════════════════════════════════════════════════════════════════════
# BUG-001: Email trailing punctuation causes SMTP rejection
# Real incident: delavin.juliana@gmail.com! → SMTP 553
# Fix: strip ! . , ; : ? < > from email on /reserve
# ═══════════════════════════════════════════════════════════════════════

class TestBug001EmailTrailingPunctuation:

    def test_reserve_strips_trailing_exclamation(self, client):
        c, db_path = client
        slot_time, date, event_id = _first_slot(client)
        res = _reserve(c, slot_time, event_id, email="test@gmail.com!")
        assert res.status_code == 200
        bid = res.get_json()["booking_id"]
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT email FROM bookings WHERE id=?", (bid,)).fetchone()
        conn.close()
        assert row[0] == "test@gmail.com"

    def test_reserve_strips_trailing_period(self, client):
        c, db_path = client
        slot_time, date, event_id = _first_slot(client)
        res = _reserve(c, slot_time, event_id, email="period@example.com.")
        assert res.status_code == 200
        bid = res.get_json()["booking_id"]
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT email FROM bookings WHERE id=?", (bid,)).fetchone()
        conn.close()
        assert row[0] == "period@example.com"

    def test_reserve_strips_angle_brackets(self, client):
        c, db_path = client
        slot_time, date, event_id = _first_slot(client)
        res = _reserve(c, slot_time, event_id, email="<angled@gmail.com>")
        assert res.status_code == 200
        bid = res.get_json()["booking_id"]
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT email FROM bookings WHERE id=?", (bid,)).fetchone()
        conn.close()
        assert row[0] == "angled@gmail.com"

    def test_reserve_strips_multiple_trailing_chars(self, client):
        c, db_path = client
        slot_time, date, event_id = _first_slot(client)
        res = _reserve(c, slot_time, event_id, email="multi@test.com!!!...,,,")
        assert res.status_code == 200
        bid = res.get_json()["booking_id"]
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT email FROM bookings WHERE id=?", (bid,)).fetchone()
        conn.close()
        assert row[0] == "multi@test.com"


# ═══════════════════════════════════════════════════════════════════════
# BUG-002: _send_client_email must return True/False
# ═══════════════════════════════════════════════════════════════════════

class TestBug002SendClientEmailReturnsStatus:

    def test_returns_true_on_success(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0, stderr=""))
        result = booking_app._send_client_email(
            to_email="test@example.com", client_name="Test",
            event_date="2026-07-04", slot_time="10:00",
            event_title="Mini", booking_id=1,
        )
        assert result is True

    def test_returns_false_on_smtp_failure(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=1, stderr="SMTP 553"))
        result = booking_app._send_client_email(
            to_email="bad@example.com", client_name="Test",
            event_date="2026-07-04", slot_time="10:00",
            event_title="Mini", booking_id=1,
        )
        assert result is False


def test_new_reservation_telegram_escapes_addons_and_consent(monkeypatch):
    captured = {}

    def fake_notify(text, reply_markup=None):
        captured["text"] = text
        captured["reply_markup"] = reply_markup
        return True

    monkeypatch.setattr(booking_app, "_notify_admin", fake_notify, raising=False)
    booking_app._notify_new_reservation(
        booking_id=123,
        client_name="<script>Client</script>",
        client_email="client@example.com",
        event_date="2026-08-10",
        slot_time="10:00",
        event_title="<b>Portraits</b>",
        session_type="mini",
        client_ig="client",
        client_phone="<img src=x onerror=bad()>",
        selected_addons=[
            {"title": "<script>Bad</script>Short Vertical Behind-the-Scenes Reel", "price": 50.0},
        ],
        addons_total=50.0,
        marketing_consent="no",
    )

    text = captured["text"]
    assert "Selected add-ons" in text
    assert "Short Vertical Behind-the-Scenes Reel" in text
    assert "$50.00 CAD" in text
    assert "Marketing consent: no" in text
    assert "<script>" not in text
    assert "onerror=" not in text
    assert captured["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "confirm:123"

    def test_returns_false_on_exception(self, monkeypatch):
        def raise_error(*a, **kw):
            raise RuntimeError("Himalaya crashed")
        monkeypatch.setattr("subprocess.run", raise_error)
        result = booking_app._send_client_email(
            to_email="test@example.com", client_name="Test",
            event_date="2026-07-04", slot_time="10:00",
            event_title="Mini", booking_id=1,
        )
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# BUG-003: client_notes column name mismatch (note vs text)
# Schema had 'note', INSERT used 'text' → OperationalError
# Fix: auto-migration renames note→text; API tolerates both
# ═══════════════════════════════════════════════════════════════════════

class TestBug003ClientNotesColumnName:

    def test_init_db_creates_text_column(self, client):
        _, db_path = client
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(client_notes)").fetchall()]
        conn.close()
        assert "text" in cols, f"client_notes should have 'text' column, got: {cols}"

    def test_add_note_and_read_back(self, client):
        c, db_path = client
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO clients (name, email) VALUES (?, ?)", ("Note Client", "note@test.com"))
        conn.commit()
        client_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

        res = c.post(f"/admin/api/clients/{client_id}/note",
                     json={"text": "Regression test note"},
                     headers=_headers())
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["note"]["text"] == "Regression test note"

    def test_client_detail_returns_notes_with_text_field(self, client):
        c, db_path = client
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO clients (name, email) VALUES (?, ?)", ("Detail Client", "detail@test.com"))
        conn.commit()
        client_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO client_notes (client_id, text) VALUES (?, ?)", (client_id, "Hello world"))
        conn.commit()
        conn.close()

        res = c.get(f"/admin/api/clients/{client_id}", headers=_headers())
        assert res.status_code == 200
        data = res.get_json()
        notes = data.get("notes", [])
        assert len(notes) == 1
        assert notes[0]["text"] == "Hello world"


# ═══════════════════════════════════════════════════════════════════════
# BUG-004: Maps card missing when location_url not set
# Fix: auto-generate Google Maps URL from location text
# ═══════════════════════════════════════════════════════════════════════

class TestBug004MapsCardAutoGenerated:

    def test_email_has_maps_with_location_only(self, monkeypatch):
        captured = {}
        def fake_run(*a, **kw):
            captured["input"] = kw.get("input") or (a[1] if len(a) > 1 else None)
            return SimpleNamespace(returncode=0, stderr="")
        monkeypatch.setattr("subprocess.run", fake_run)
        booking_app._send_client_email(
            to_email="maps@test.com", client_name="Maps",
            event_date="2026-07-04", slot_time="15:00",
            event_title="Canoe Mini", booking_id=100,
            location="Carburn Park", location_url=None,
        )
        email = captured["input"]
        assert "google.com/maps" in email.lower()
        assert "Carburn Park" in email
        assert "Open in Google Maps" in email

    def test_email_has_maps_with_explicit_url(self, monkeypatch):
        captured = {}
        def fake_run(*a, **kw):
            captured["input"] = kw.get("input") or (a[1] if len(a) > 1 else None)
            return SimpleNamespace(returncode=0, stderr="")
        monkeypatch.setattr("subprocess.run", fake_run)
        booking_app._send_client_email(
            to_email="maps2@test.com", client_name="Maps2",
            event_date="2026-07-04", slot_time="15:00",
            event_title="Lilac Mini", booking_id=101,
            location="Baker Park",
            location_url="https://maps.google.com/?q=Baker+Park",
        )
        email = captured["input"]
        assert "Baker Park" in email
        assert "Open in Google Maps" in email

    def test_email_no_maps_without_location(self, monkeypatch):
        captured = {}
        def fake_run(*a, **kw):
            captured["input"] = kw.get("input") or (a[1] if len(a) > 1 else None)
            return SimpleNamespace(returncode=0, stderr="")
        monkeypatch.setattr("subprocess.run", fake_run)
        booking_app._send_client_email(
            to_email="nomaps@test.com", client_name="NoMaps",
            event_date="2026-07-04", slot_time="15:00",
            event_title="Mystery Mini", booking_id=102,
            location=None, location_url=None,
        )
        email = captured["input"]
        assert "Open in Google Maps" not in email


# ═══════════════════════════════════════════════════════════════════════
# BUG-006: Hardcoded $95 in confirmBooking button
# BUG-007: Server /admin/confirm ignores deposit_amount from booking
# Fix: button passes deposit_amount; server falls back to booking's deposit_amount
# ═══════════════════════════════════════════════════════════════════════

class TestBug006ConfirmUsesCorrectAmount:

    def test_confirm_uses_paid_amount_from_request(self, client):
        c, db_path = client
        slot_time, date, event_id = _first_slot(client)
        res = _reserve(c, slot_time, event_id, name="Amount Test", email="amount@test.com")
        assert res.status_code == 200, f"Reserve failed: {res.get_json()}"
        bid = res.get_json()["booking_id"]

        res = c.post("/admin/confirm", json={
            "booking_id": bid,
            "paid_amount": 110.25,
        }, headers=_headers())
        assert res.status_code == 200

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT paid_amount FROM bookings WHERE id=?", (bid,)).fetchone()
        conn.close()
        assert float(row[0]) == 110.25

    def test_confirm_falls_back_to_booking_deposit_amount(self, client):
        """If JS doesn't send paid_amount, server must use deposit_amount from DB."""
        c, db_path = client
        slot_time, date, event_id = _first_slot(client)
        res = _reserve(c, slot_time, event_id, name="Fallback Test", email="fallback@test.com")
        assert res.status_code == 200, f"Reserve failed: {res.get_json()}"
        bid = res.get_json()["booking_id"]

        # Check what deposit_amount the booking has
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT deposit_amount FROM bookings WHERE id=?", (bid,)).fetchone()
        deposit = float(row[0]) if row[0] else None
        conn.close()

        res = c.post("/admin/confirm", json={
            "booking_id": bid,
            # No paid_amount! Server should fall back
        }, headers=_headers())
        assert res.status_code == 200

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT paid_amount FROM bookings WHERE id=?", (bid,)).fetchone()
        conn.close()
        actual = float(row[0])
        # Must NOT be hardcoded 95
        if deposit:
            assert actual == deposit, f"Expected {deposit} from deposit_amount but got {actual}"
        # In any case, must never be 95 if event has different price
        ev = _first_event()
        if ev.get("deposit") and float(ev["deposit"]) != 95:
            assert actual != 95, "paid_amount should NOT be hardcoded 95"


# ═══════════════════════════════════════════════════════════════════════
# BUG-009: admin_confirm always says "sent" even when email fails
# Fix: Telegram message reflects actual email status
# ═══════════════════════════════════════════════════════════════════════

class TestBug009ConfirmReportsEmailStatus:

    def test_confirm_shows_email_sent_on_success(self, client, monkeypatch):
        c, db_path = client
        slot_time, date, event_id = _first_slot(client)
        res = _reserve(c, slot_time, event_id, name="Status OK", email="statusok@test.com")
        assert res.status_code == 200
        bid = res.get_json()["booking_id"]

        # Email succeeds
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0, stderr=""))
        res = c.post("/admin/confirm", json={"booking_id": bid, "paid_amount": 110.25}, headers=_headers())
        data = res.get_json()
        assert data["success"] is True
        # Should say "sent" not "FAILED"
        msg = data.get("message", "")
        assert "sent" in msg.lower() or "confirmation" in msg.lower()

    def test_confirm_shows_email_failed_on_error(self, client, monkeypatch):
        c, db_path = client
        slot_time, date, event_id = _first_slot(client)
        res = _reserve(c, slot_time, event_id, name="Status Fail", email="statusfail@test.com")
        assert res.status_code == 200
        bid = res.get_json()["booking_id"]

        # Email fails
        def fail_email(*a, **kw):
            raise RuntimeError("SMTP down")
        monkeypatch.setattr("subprocess.run", fail_email)
        res = c.post("/admin/confirm", json={"booking_id": bid, "paid_amount": 110.25}, headers=_headers())
        data = res.get_json()
        assert data["success"] is True  # Booking still confirmed
        msg = data.get("message", "")
        assert "FAILED" in msg or "failed" in msg.lower(), \
            f"Message should indicate email failure, got: {msg}"


# ═══════════════════════════════════════════════════════════════════════
# TEMPLATE REGRESSION: confirmBooking must not hardcode 95
# ═══════════════════════════════════════════════════════════════════════

class TestTemplateConfirmButton:

    def test_admin_html_no_hardcoded_paid_amount_95(self):
        template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "admin.html")
        with open(template_path) as f:
            html = f.read()
        assert "paid_amount: 95" not in html, \
            "BUG-006 regression: hardcoded paid_amount: 95 found in admin.html"

    def test_admin_html_confirm_passes_deposit_amount_arg(self):
        template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "admin.html")
        with open(template_path) as f:
            html = f.read()
        # confirmBooking should reference deposit_amount or paid_amount
        assert "deposit_amount" in html or "paid_amount" in html, \
            "confirmBooking must pass amount dynamically (deposit_amount or paid_amount)"
