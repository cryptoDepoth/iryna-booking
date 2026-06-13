"""Durable balance-payment page (/pay-balance) + email/success wiring.

The balance flow used to be a one-time Stripe Checkout URL that expires ~24h —
useless when a client pays the balance days after the shoot, and absent entirely
from the auto-confirmed e-Transfer email. These tests guard the durable page and
its links (e-Transfer + on-demand Stripe), the bad-token rejection, the
e-Transfer fallback when Stripe is off, and that the confirmation email + success
page surface the link only when a balance is actually due.
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
    monkeypatch.setattr(booking_app, "BASE_URL", "https://book.test")
    monkeypatch.setattr(booking_app, "_notify_admin", lambda *a, **kw: None, raising=False)
    booking_app.init_db()
    with booking_app.app.test_client() as c:
        yield c, db_path
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _insert(db_path, *, paid_amount=250.0, token="bal-token", status="confirmed"):
    ev = booking_app.EVENTS[0]
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO bookings (event_id, date, time, name, email, phone, instagram,
               confirmed, paid, status, paid_amount, deposit_amount, confirmation_token)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ev["id"], ev.get("date") or "2026-06-07", "15:00", "Balance Client",
         "bal@example.com", "4035550000", "@bal", 1, 1, status, paid_amount,
         ev.get("deposit", 95.0), token),
    )
    bid = cur.lastrowid
    conn.commit()
    conn.close()
    return bid, ev


# ── URL helper ─────────────────────────────────────────────────────────────

def test_balance_page_url_is_durable_and_identity_safe(monkeypatch):
    monkeypatch.setattr(booking_app, "BASE_URL", "https://book.test")
    url = booking_app._balance_page_url({"id": 42, "confirmation_token": "tok42"})
    assert url == "https://book.test/pay-balance?booking_id=42&token=tok42"
    # No id or token → no link (cannot build an unauthenticated balance link).
    assert booking_app._balance_page_url({"id": 42}) is None
    assert booking_app._balance_page_url({}) is None


# ── Page rendering + auth ────────────────────────────────────────────────────

def test_pay_balance_renders_with_amount_and_etransfer(client):
    c, db_path = client
    bid, ev = _insert(db_path, paid_amount=250.0)  # full 500 − 250 deposit = 250 due
    r = c.get(f"/pay-balance?booking_id={bid}&token=bal-token")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Remaining balance" in html
    assert "250.00" in html
    assert "Interac e-Transfer" in html
    assert booking_app.META_PIXEL_ID in html  # funnel continuity


def test_pay_balance_rejects_wrong_token(client):
    c, db_path = client
    bid, _ = _insert(db_path)
    r = c.get(f"/pay-balance?booking_id={bid}&token=WRONG", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_pay_balance_paid_in_full_shows_done_state(client):
    c, db_path = client
    ev = booking_app.EVENTS[0]
    bid, _ = _insert(db_path, paid_amount=float(ev.get("full_price", 500.0)))
    html = c.get(f"/pay-balance?booking_id={bid}&token=bal-token").get_data(as_text=True)
    assert "Paid in full" in html


# ── Stripe checkout endpoint ─────────────────────────────────────────────────

def test_balance_checkout_falls_back_to_etransfer_when_stripe_off(client, monkeypatch):
    c, db_path = client
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "")
    bid, _ = _insert(db_path, paid_amount=250.0)
    r = c.post("/pay-balance/checkout", json={"booking_id": bid, "confirmation_token": "bal-token"})
    assert r.status_code == 400
    assert "e-Transfer" in r.get_json()["error"]


def test_balance_checkout_creates_session_when_stripe_on(client, monkeypatch):
    c, db_path = client
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(booking_app, "_create_balance_checkout_url",
                        lambda b, e, due: "https://checkout.stripe.test/sess", raising=False)
    bid, _ = _insert(db_path, paid_amount=250.0)
    r = c.post("/pay-balance/checkout", json={"booking_id": bid, "confirmation_token": "bal-token"})
    assert r.status_code == 200
    assert r.get_json()["checkout_url"] == "https://checkout.stripe.test/sess"


# ── Email + success wiring ───────────────────────────────────────────────────

def test_email_context_carries_balance_link_only_when_due(monkeypatch):
    monkeypatch.setattr(booking_app, "BASE_URL", "https://book.test")
    ev = {"full_price": 500, "deposit": 250}
    due = {"id": 5, "confirmation_token": "t5", "paid_amount": 250, "deposit_amount": 250}
    ctx = booking_app._client_email_context(due, ev)
    assert ctx["balance_due"] == 250.0
    assert ctx["balance_url"] == "https://book.test/pay-balance?booking_id=5&token=t5"

    paid = {"id": 6, "confirmation_token": "t6", "paid_amount": 500, "deposit_amount": 250}
    ctx2 = booking_app._client_email_context(paid, ev)
    assert ctx2["balance_url"] is None
    assert ctx2["balance_due"] is None


def test_success_page_shows_balance_cta_when_due(client):
    c, db_path = client
    bid, _ = _insert(db_path, paid_amount=250.0)
    html = c.get(f"/success?booking_id={bid}&token=bal-token").get_data(as_text=True)
    assert "Pay remaining balance" in html
    assert "/pay-balance?booking_id=" in html
