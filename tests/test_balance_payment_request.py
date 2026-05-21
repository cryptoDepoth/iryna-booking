"""Admin balance-payment request regression tests.

Feature: after a client has paid the deposit and the booking is confirmed, admin
can request the remaining balance by email with Interac instructions and an
optional Stripe Checkout link.
"""
import os
import sqlite3
import tempfile

import pytest

import app as booking_app


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "BASE_URL", "https://book.test")
    monkeypatch.setattr(booking_app, "_notify_admin", lambda *a, **kw: None, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _insert_confirmed_booking(db_path, *, paid_amount=95.0, status="confirmed", event_id=None):
    ev = booking_app.EVENTS[0]
    event_id = event_id or ev["id"]
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bookings (
            event_id, date, time, name, email, phone, instagram,
            confirmed, paid, status, paid_amount, deposit_amount, confirmation_token
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            ev.get("date") or "2026-06-01",
            "10:00",
            "Balance Client",
            "balance@example.com",
            "4035550000",
            "@balance",
            1 if status == "confirmed" else 0,
            1 if status == "confirmed" else 0,
            status,
            paid_amount,
            ev.get("deposit", 95.0),
            "balance-token",
        ),
    )
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()
    return booking_id, ev


def test_balance_due_uses_event_total_minus_paid_amount():
    ev = {"full_price": 275, "deposit": 95}
    booking = {"paid_amount": 95, "deposit_amount": 95}
    assert booking_app._booking_balance_due(booking, ev) == 180.0


def test_balance_due_falls_back_to_deposit_when_paid_amount_missing():
    ev = {"full_price": 300, "deposit": 100}
    booking = {"paid_amount": None, "deposit_amount": 100}
    assert booking_app._booking_balance_due(booking, ev) == 200.0


def test_admin_request_balance_requires_auth(client):
    c, db_path = client
    booking_id, _ev = _insert_confirmed_booking(db_path)
    response = c.post("/admin/request-balance", json={"booking_id": booking_id})
    assert response.status_code in (302, 401, 403)


def test_admin_request_balance_sends_email_with_interac_and_stripe_link(client, monkeypatch):
    c, db_path = client
    ev_deposit = booking_app.EVENTS[0].get("deposit", 95.0)
    booking_id, ev = _insert_confirmed_booking(db_path, paid_amount=ev_deposit)
    expected_balance = float(ev.get("full_price", 190.0)) - float(ev_deposit)

    sent = {}
    monkeypatch.setattr(
        booking_app,
        "_create_balance_checkout_url",
        lambda booking, event, balance_due: "https://checkout.stripe.test/balance",
        raising=False,
    )
    monkeypatch.setattr(
        booking_app,
        "_send_balance_request_email",
        lambda **kwargs: sent.update(kwargs) or True,
        raising=False,
    )

    response = c.post(
        "/admin/request-balance",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"booking_id": booking_id},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["balance_due"] == round(expected_balance, 2)
    assert data["stripe_url"] == "https://checkout.stripe.test/balance"
    assert sent["to_email"] == "balance@example.com"
    assert sent["balance_due"] == round(expected_balance, 2)
    assert sent["stripe_url"] == "https://checkout.stripe.test/balance"


def test_admin_request_balance_rejects_unconfirmed_booking(client):
    c, db_path = client
    booking_id, _ev = _insert_confirmed_booking(db_path, status="reserved")

    response = c.post(
        "/admin/request-balance",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"booking_id": booking_id},
    )

    assert response.status_code == 400
    assert "confirmed" in response.get_json()["error"].lower()


def test_admin_request_balance_rejects_fully_paid_booking(client):
    c, db_path = client
    ev = booking_app.EVENTS[0]
    booking_id, _ev = _insert_confirmed_booking(db_path, paid_amount=ev.get("full_price", 190.0))

    response = c.post(
        "/admin/request-balance",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"booking_id": booking_id},
    )

    assert response.status_code == 400
    assert "no balance" in response.get_json()["error"].lower()


def test_admin_template_contains_request_balance_button(client):
    c, _db = client
    response = c.get("/admin", headers={"X-Admin-Key": "test-admin-key"})
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "requestBalance" in html
    assert "Request Balance" in html
