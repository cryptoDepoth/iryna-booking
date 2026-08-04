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
import re
import subprocess
import sqlite3
import tempfile
import pytest

import app as booking_app
import check_etransfer_v2 as checker


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


def test_admin_invoice_pdf_stays_one_page_with_verbose_booking(admin_client, monkeypatch):
    """A verbose booking with Cyrillic client data should still render as one PDF page."""
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *a, **k: True, raising=False)
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)
    conn = sqlite3.connect(booking_app.DB_PATH)
    conn.execute(
        """UPDATE bookings
           SET name=?, phone=?, session_type=?, full_price=?, paid_amount=?, deposit_amount=?
           WHERE id=?""",
        (
            "Олександра Довгопрізвище-Перевірка",
            "+1 368 997 7903 ext. 12345",
            "Дуже довга сімейна фотосесія з розширеним описом",
            875.0,
            250.0,
            250.0,
            booking_id,
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        booking_app,
        "get_event_by_id",
        lambda _event_id: {
            "title": "Дуже довга сімейна фотосесія з розширеним описом пакету",
            "location": "Fish Creek Provincial Park, Calgary — точка зустрічі біля головного входу",
            "full_price": 875.0,
            "included": [
                "Pre-session planning and styling guidance with location recommendations",
                "Up to 60 minutes of relaxed guided photography for the whole family",
                "Private online gallery with professionally edited high-resolution images",
                "Print release for personal use and simple sharing with relatives",
                "Delivery within five to seven business days after the session",
                "Optional short preview selection before final gallery delivery",
            ],
        },
        raising=False,
    )

    resp = admin_client.post(f"/admin/booking/{booking_id}/invoice", json={})
    assert resp.status_code == 200
    assert resp.data[:4] == b"%PDF"
    page_count = len(re.findall(rb"/Type\s*/Page\b", resp.data))
    assert page_count == 1


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
    """POST /admin/booking/<id>/wfolio should save URL and send gallery email."""
    captured = {}
    def mock_email(*a, **k):
        captured["called"] = True
        captured["booking"] = k.get("booking") or (a[0] if a else None)
        captured["url"] = k.get("wfolio_url") or (a[1] if len(a) > 1 else None)
        return True
    monkeypatch.setattr(booking_app, "_send_gallery_email", mock_email, raising=False)

    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)
    wfolio_url = "https://pashynska.wfolio.com/gallery/test123"

    resp = admin_client.post(
        f"/admin/booking/{booking_id}/wfolio",
        json={"wfolio_url": wfolio_url}
    )
    assert resp.status_code == 200
    # Verify DB was updated
    conn = sqlite3.connect(booking_app.DB_PATH)
    row = conn.execute(
        "SELECT wfolio_url, gallery_email_sent_at FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    conn.close()
    assert row and row[0] == wfolio_url
    assert row[1], "successful gallery delivery must be timestamped"
    assert captured.get("called") is True
    assert captured.get("url") == wfolio_url
    assert captured["booking"].get("id") == booking_id


def test_admin_wfolio_failed_email_does_not_mark_gallery_delivered(admin_client, monkeypatch):
    """A saved URL is not delivery proof when the gallery email fails."""
    monkeypatch.setattr(booking_app, "_send_gallery_email", lambda *a, **k: False, raising=False)
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)

    resp = admin_client.post(
        f"/admin/booking/{booking_id}/wfolio",
        json={"wfolio_url": "https://pashynska.wfolio.com/gallery/retry123"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["success"] is False
    conn = sqlite3.connect(booking_app.DB_PATH)
    row = conn.execute(
        "SELECT wfolio_url, gallery_email_sent_at FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    conn.close()
    assert row[0].endswith("/gallery/retry123")
    assert row[1] is None


def test_admin_can_mark_manually_emailed_gallery_delivered(admin_client, monkeypatch):
    """Manual Gmail delivery can be recorded without sending a duplicate email."""
    monkeypatch.setattr(
        booking_app,
        "_send_gallery_email",
        lambda *a, **k: pytest.fail("mark-delivered must not send another email"),
        raising=False,
    )
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)
    wfolio_url = "https://pashynska.wfolio.com/gallery/already-emailed"

    resp = admin_client.post(
        f"/admin/booking/{booking_id}/mark-gallery-delivered",
        json={"wfolio_url": wfolio_url},
    )

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    conn = sqlite3.connect(booking_app.DB_PATH)
    row = conn.execute(
        "SELECT wfolio_url, gallery_email_sent_at FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    conn.close()
    assert row[0] == wfolio_url
    assert row[1]


def test_mark_gallery_delivered_requires_valid_url(admin_client, monkeypatch):
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)

    resp = admin_client.post(
        f"/admin/booking/{booking_id}/mark-gallery-delivered",
        json={"wfolio_url": ""},
    )

    assert resp.status_code == 400
    conn = sqlite3.connect(booking_app.DB_PATH)
    row = conn.execute(
        "SELECT gallery_email_sent_at FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    conn.close()
    assert row[0] is None


# 7. Google Review email
def test_admin_review_email(admin_client, monkeypatch):
    """RED: POST /admin/booking/<id>/send-review should trigger review email."""
    captured = {}
    def mock_email(*a, **k):
        captured["called"] = True
        captured["args"] = a
        return True
    monkeypatch.setattr(booking_app, "_send_email_raw", mock_email, raising=False)

    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)

    resp = admin_client.post(f"/admin/booking/{booking_id}/send-review", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("success") is True
    assert captured.get("called") is True
    assert "https://review.pashynskaphoto.com" in captured["args"][4]
    assert "Thank you for choosing Pashynska Photography. Review link" not in captured["args"][4]


# 8. Detail page exposes one-page invoice/balance generator fields
def test_admin_booking_detail_has_inline_invoice_generator(admin_client, monkeypatch):
    """RED: detail page should let admin edit total/paid and request balance from one page."""
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)
    conn = sqlite3.connect(booking_app.DB_PATH)
    conn.execute("UPDATE bookings SET full_price=?, paid_amount=?, deposit_amount=? WHERE id=?", (500.0, 200.0, 200.0, booking_id))
    conn.commit()
    conn.close()

    resp = admin_client.get(f"/admin/booking/{booking_id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Invoice generator" in html
    assert "invoiceFullPrice" in html
    assert "invoicePaidAmount" in html
    assert "invoiceBalanceDue" in html
    assert "Request balance" in html
    assert "Recheck Interac" in html
    assert "300.00" in html


def test_admin_recheck_payment_updates_paid_amount_from_interac(admin_client, monkeypatch):
    """Admin can trigger the same safe Interac reconciliation from booking detail."""
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)
    conn = sqlite3.connect(booking_app.DB_PATH)
    conn.execute(
        "UPDATE bookings SET event_id=?, date=?, time=?, name=?, full_price=?, paid_amount=?, deposit_amount=? WHERE id=?",
        ("canoe-mini-session-2026-07-04", "2026-07-04", "13:30", "Yulia Levitskaya", 220.50, 110.25, 110.25, booking_id),
    )
    conn.commit()
    conn.close()

    def fake_check_single_email(email, pending, reconciliation):
        assert pending == []
        assert reconciliation and reconciliation[0]["id"] == booking_id
        conn = sqlite3.connect(booking_app.DB_PATH)
        conn.execute("UPDATE bookings SET paid_amount=? WHERE id=?", (120.50, booking_id))
        conn.commit()
        conn.close()
        return None, None

    monkeypatch.setattr(checker, "get_emails", lambda page_size=None: [{"id": "interac-194", "subject": "Interac e-Transfer"}])
    monkeypatch.setattr(checker, "is_etransfer_email", lambda email: True)
    monkeypatch.setattr(checker, "check_single_email", fake_check_single_email)

    resp = admin_client.post(f"/admin/booking/{booking_id}/recheck-payment", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["updated"] is True
    assert data["paid_amount"] == 120.50
    assert data["balance_due"] == 100.0


# 9. Admin can edit invoice amounts from detail page without prompts
def test_admin_invoice_patch_updates_amounts(admin_client, monkeypatch):
    """RED: PATCH /admin/booking/<id>/invoice should persist full/paid/deposit values."""
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)

    resp = admin_client.patch(
        f"/admin/booking/{booking_id}/invoice",
        json={"full_price": 640.0, "paid_amount": 250.0, "deposit_amount": 250.0},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["full_price"] == 640.0
    assert data["paid_amount"] == 250.0
    assert data["balance_due"] == 390.0

    conn = sqlite3.connect(booking_app.DB_PATH)
    row = conn.execute("SELECT full_price, paid_amount, deposit_amount FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    assert row == (640.0, 250.0, 250.0)


# 10. Balance request uses booking.full_price and returns copyable link if email succeeds
def test_admin_request_balance_uses_booking_full_price_and_returns_link(admin_client, monkeypatch):
    """RED: remaining balance = booking.full_price - paid_amount, not stale event defaults."""
    booking_id, _ = _reserve_test_booking(monkeypatch, admin_client)
    conn = sqlite3.connect(booking_app.DB_PATH)
    conn.execute("UPDATE bookings SET full_price=?, paid_amount=?, deposit_amount=? WHERE id=?", (700.0, 250.0, 250.0, booking_id))
    conn.commit()
    conn.close()

    captured = {}
    def fake_balance_email(**kwargs):
        captured.update(kwargs)
        return True
    monkeypatch.setattr(booking_app, "_send_balance_request_email", fake_balance_email, raising=False)
    monkeypatch.setattr(booking_app, "_create_balance_checkout_url", lambda booking, event, balance_due: "https://buy.stripe.com/test_balance", raising=False)
    monkeypatch.setattr(booking_app, "_notify_admin", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(booking_app, "_emit_n8n_event", lambda *a, **k: True, raising=False)

    resp = admin_client.post("/admin/request-balance", json={"booking_id": booking_id})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["total_price"] == 700.0
    assert data["paid_amount"] == 250.0
    assert data["balance_due"] == 450.0
    assert data["stripe_url"] == "https://buy.stripe.com/test_balance"
    assert captured["total_price"] == 700.0
    assert captured["paid_amount"] == 250.0
    assert captured["balance_due"] == 450.0
