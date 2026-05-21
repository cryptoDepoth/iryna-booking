"""RED tests for /admin/booking/<id> detail page.

Features tested:
- GET page renders for admin auth
- Booking detail template contains action buttons
- Invoice generation returns PDF
- Invoice email sends with PDF attachment
- Wfolio URL update + gallery email
- Google Review email request
- Non-admin = 403
- Invalid booking id = 404
"""
import os
import subprocess
import sqlite3
import tempfile
import pytest

import app as booking_app


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    """Flask test client with temp DB (same pattern as test_booking_flow.py)."""
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
    monkeypatch.setattr(booking_app, "_send_client_email", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(booking_app, "_send_email_with_attachment", lambda *a, **k: True, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()

    booking_app.app.config["TESTING"] = True
    booking_app.init_db()
    with booking_app.app.test_client() as client:
        yield client


@pytest.fixture()
def admin_client(client, monkeypatch):
    """Logged-in admin client."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    yield client


# ── helpers ─────────────────────────────────────────────────────────

def _reserve_test_booking(monkeypatch, client):
    """Create a confirmed booking directly via SQL and return its id."""
    db_path = booking_app.DB_PATH
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''
        INSERT INTO bookings (date, time, name, email, phone, instagram, session_type, status, paid, confirmed, event_id, deposit_amount, paid_amount, review_email_sent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        "2026-06-07", "15:00", "Test Detail",
        "test_detail@example.com", "403-555-1234", "", "mini",
        "confirmed", 1, 1, "lilac-jun7", 250.0, 250.0, None
    ))
    booking_id = c.lastrowid
    conn.commit()
    conn.close()
    return booking_id, "dummy-token"


# ── RED tests ─────────────────────────────────────────────────────────

# 1. Booking detail page exists and renders
def test_admin_booking_detail_page_exists(admin_client, monkeypatch):
    """RED: /admin/booking/<id> should render with 200OK and show client info."""
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *a, **k: True, raising=False)
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)

    resp = admin_client.get(f"/admin/booking/{booking_id}")
    assert resp.status_code == 200

    html = resp.data.decode()
    assert "Invoice" in html or "Gallery" in html or "Review" in html


# 2. Non-admin gets 302/403/401
def test_admin_booking_detail_requires_auth(client, monkeypatch):
    """RED: Without admin session should get 302/403/401."""
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *a, **k: True, raising=False)
    booking_id, _ = _reserve_test_booking(monkeypatch, client)
    resp = client.get(f"/admin/booking/{booking_id}")
    assert resp.status_code in (302, 403, 401)


# 3. Invalid booking id = 404
def test_admin_booking_detail_invalid_id(admin_client):
    """RED: Nonexistent booking should return 404."""
    resp = admin_client.get("/admin/booking/999999")
    assert resp.status_code == 404


# 4. Invoice generation endpoint returns PDF
def test_admin_invoice_generate_returns_pdf(admin_client, monkeypatch):
    """RED: POST /admin/booking/<id>/invoice should generate PDF."""
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *a, **k: True, raising=False)
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)

    resp = admin_client.post(f"/admin/booking/{booking_id}/invoice", json={})
    assert resp.status_code == 200
    assert resp.content_type == "application/pdf"
    assert resp.data[:4] == b"%PDF"


# 5. Invoice email sends with PDF attachment
def test_admin_invoice_send_email(admin_client, monkeypatch):
    """RED: POST /admin/booking/<id>/send-invoice should trigger email."""
    captured = {}
    def mock_email(*a, **k):
        captured["called"] = True
        return True
    monkeypatch.setattr(booking_app, "_send_email_with_attachment", mock_email, raising=False)
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *a, **k: True, raising=False)

    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)

    resp = admin_client.post(f"/admin/booking/{booking_id}/send-invoice", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("success") is True
    assert captured.get("called") is True


# 6. Wfolio URL update + gallery email
def test_admin_wfolio_update_and_email(admin_client, monkeypatch):
    """RED: POST /admin/booking/<id>/wfolio should save URL and send gallery email."""
    captured = {}
    def mock_email(*a, **k):
        captured["called"] = True
        return True
    monkeypatch.setattr(booking_app, "_send_email_with_attachment", mock_email, raising=False)
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *a, **k: True, raising=False)

    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)
    wfolio_url = "https://pashynska.wfolio.com/gallery/test123"

    resp = admin_client.post(
        f"/admin/booking/{booking_id}/wfolio",
        json={"wfolio_url": wfolio_url}
    )
    assert resp.status_code == 200
    # Verify DB was updated
    conn = sqlite3.connect(booking_app.DB_PATH)
    row = conn.execute("SELECT wfolio_url FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    assert row and row[0] == wfolio_url
    assert captured.get("called") is True


# 7. Google Review email
def test_admin_review_email(admin_client, monkeypatch):
    """RED: POST /admin/booking/<id>/send-review should trigger review email."""
    captured = {}
    def mock_email(*a, **k):
        captured["called"] = True
        return True
    monkeypatch.setattr(booking_app, "_send_email_with_attachment", mock_email, raising=False)
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *a, **k: True, raising=False)

    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)

    resp = admin_client.post(f"/admin/booking/{booking_id}/send-review", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("success") is True
    assert captured.get("called") is True
