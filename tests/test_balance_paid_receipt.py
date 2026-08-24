"""Final-balance accounting, delivery promise, and receipt regressions."""

import sqlite3

import pytest

import app as booking_app
import check_etransfer_v2 as checker


EVENT = {
    "id": "receipt-event",
    "title": "Family Golden Hour Session",
    "date": "2026-09-12",
    "full_price": 340.0,
    "deposit": 100.0,
    "included": [],
}


@pytest.fixture()
def receipt_env(monkeypatch, tmp_path):
    db_path = str(tmp_path / "bookings.db")
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(checker, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "BASE_URL", "https://book.test")
    monkeypatch.setattr(
        booking_app,
        "get_event_by_id",
        lambda event_id: dict(EVENT) if event_id == EVENT["id"] else None,
    )
    monkeypatch.setattr(booking_app, "get_active_event", lambda: dict(EVENT))
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda *_a, **_k: None)
    monkeypatch.setattr(booking_app, "_notify_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(booking_app, "_emit_n8n_event", lambda *_a, **_k: True)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()
    return db_path


def _insert_booking(
    db_path, *, paid_amount=100.0, full_price=340.0, slot_time="18:00"
):
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        INSERT INTO bookings (
            event_id, date, time, name, email, phone, status, confirmed, paid,
            paid_amount, deposit_amount, full_price, confirmation_token
        ) VALUES (?, ?, ?, 'Receipt Client', 'receipt@example.com',
                  '4035550100', 'confirmed', 1, 1, ?, 100, ?, 'receipt-token')
        """,
        (EVENT["id"], EVENT["date"], slot_time, paid_amount, full_price),
    )
    booking_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return booking_id


def _booking_payment_state(db_path, booking_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        """
        SELECT paid_amount, balance_receipt_claimed_at,
               balance_receipt_sent_at, paid_in_full_at
          FROM bookings WHERE id=?
        """,
        (booking_id,),
    ).fetchone()
    conn.close()
    return row


def test_delivery_promise_uses_event_copy_and_safe_default():
    mini = booking_app._delivery_promise(
        {"included": ["Quick turnaround within 48 hours"]}
    )
    assert mini["preview"] == ""
    assert mini["gallery"] == "within 48 hours"
    assert mini["summary"] == "Your complete gallery arrives within 48 hours."

    standard = booking_app._delivery_promise({})
    assert standard["preview"] == "within 48 hours"
    assert standard["gallery"] == "within 14 calendar days"
    assert "Preview within 48 hours" in standard["invoice"]


def test_paid_in_full_receipt_is_branded_and_sent_exactly_once(
    receipt_env, monkeypatch
):
    booking_id = _insert_booking(receipt_env, paid_amount=340.0)
    captured = []
    monkeypatch.setattr(
        booking_app,
        "_send_email_raw",
        lambda *args: captured.append(args) or True,
    )

    first = booking_app._maybe_send_balance_paid_email(
        booking_id,
        amount_received=240.0,
        payment_method="Interac e-Transfer",
    )
    second = booking_app._maybe_send_balance_paid_email(
        booking_id,
        amount_received=240.0,
        payment_method="Interac e-Transfer",
    )

    assert first == "sent"
    assert second == "already_sent"
    assert len(captured) == 1
    _to, _name, subject, plain, html = captured[0]
    assert subject == "Payment received — your session is paid in full"
    assert "Balance remaining: $0.00 CAD" in plain
    assert "complete gallery within 14 calendar days" in plain
    assert "#9A7628" in html or "#A77C25" in html
    assert "<script" not in html.lower()
    state = _booking_payment_state(receipt_env, booking_id)
    assert state[0] == 340.0
    assert state[1] is None
    assert state[2]
    assert state[3]


def test_receipt_failure_releases_claim_for_safe_retry(receipt_env, monkeypatch):
    booking_id = _insert_booking(receipt_env, paid_amount=340.0)
    attempts = []

    def send(*_args):
        attempts.append(1)
        return len(attempts) > 1

    monkeypatch.setattr(booking_app, "_send_email_raw", send)
    assert booking_app._maybe_send_balance_paid_email(booking_id) == "failed"
    assert _booking_payment_state(receipt_env, booking_id)[1] is None
    assert booking_app._maybe_send_balance_paid_email(booking_id) == "sent"
    assert len(attempts) == 2


def test_partial_payment_does_not_send_paid_in_full_receipt(
    receipt_env, monkeypatch
):
    booking_id = _insert_booking(receipt_env, paid_amount=200.0)
    monkeypatch.setattr(
        booking_app,
        "_send_email_raw",
        lambda *_args: pytest.fail("partial payment must not send receipt"),
    )
    assert (
        booking_app._maybe_send_balance_paid_email(booking_id)
        == "not_paid_in_full"
    )


def test_interac_balance_adds_to_deposit_while_legacy_reconcile_overwrites(
    receipt_env, monkeypatch
):
    balance_booking_id = _insert_booking(receipt_env, paid_amount=100.0)
    reconcile_booking_id = _insert_booking(
        receipt_env, paid_amount=100.0, slot_time="18:40"
    )
    sent = []
    monkeypatch.setattr(
        booking_app,
        "_send_email_raw",
        lambda *args: sent.append(args) or True,
    )

    balance_ledger = checker.record_etransfer(
        reference_number="balance-ref",
        message_id="balance-message",
        sender_name="Receipt Client",
        amount=240.0,
        memo=f"Balance for booking #{balance_booking_id}",
    )
    assert checker.apply_confirmed_balance_payment(
        balance_booking_id,
        240.0,
        "balance-message",
        ledger_id=balance_ledger,
    )
    assert not checker.apply_confirmed_balance_payment(
        balance_booking_id,
        240.0,
        "balance-message",
        ledger_id=balance_ledger,
    )

    reconcile_ledger = checker.record_etransfer(
        reference_number="reconcile-ref",
        message_id="reconcile-message",
        sender_name="Receipt Client",
        amount=240.0,
        memo="corrected deposit",
    )
    assert checker.reconcile_confirmed_payment(
        reconcile_booking_id,
        240.0,
        "reconcile-message",
        ledger_id=reconcile_ledger,
    )

    assert _booking_payment_state(receipt_env, balance_booking_id)[0] == 340.0
    assert _booking_payment_state(receipt_env, reconcile_booking_id)[0] == 240.0
    assert len(sent) == 1
    conn = sqlite3.connect(receipt_env)
    mutation = conn.execute(
        "SELECT payment_mutation FROM etransfers WHERE message_id='balance-message'"
    ).fetchone()[0]
    conn.close()
    assert mutation == "balance"


def test_explicit_balance_memo_wins_over_same_amount_pending_deposit(
    receipt_env, monkeypatch
):
    balance_booking_id = _insert_booking(receipt_env, paid_amount=100.0)
    conn = sqlite3.connect(receipt_env)
    conn.execute(
        """
        INSERT INTO bookings (
            event_id, date, time, name, email, phone, status, confirmed, paid,
            paid_amount, deposit_amount, full_price, reserved_until
        ) VALUES (?, ?, '19:00', 'Pending Client', 'pending@example.com',
                  '4035550199',
                  'pending_payment', 0, 0, 0, 240, 480,
                  datetime('now', '+1 hour'))
        """,
        (EVENT["id"], EVENT["date"]),
    )
    conn.commit()
    conn.close()
    body = (
        "Interac e-Transfer: You've received $240.00 from Receipt Client.\n"
        f"Message: Balance for booking #{balance_booking_id}"
    )
    monkeypatch.setattr(checker, "read_message_body", lambda _message_id: body)
    monkeypatch.setattr(checker, "try_confirm_gift_etransfer", lambda *_a, **_k: False)
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *_args: True)
    monkeypatch.setattr(checker, "_notify_admin_balance", lambda *_a, **_k: None)

    result = checker.check_single_email(
        {"id": "priority-balance", "date": "2026-09-01T12:00:00+00:00"},
        checker.get_pending_bookings(within_minutes=120),
        checker.get_reconciliation_bookings(within_days=120),
    )

    assert result == (None, None)
    assert _booking_payment_state(receipt_env, balance_booking_id)[0] == 340.0


def test_unlinking_balance_restores_deposit_and_warns_about_sent_receipt(
    receipt_env, monkeypatch
):
    booking_id = _insert_booking(receipt_env, paid_amount=100.0)
    monkeypatch.setattr(booking_app, "_send_email_raw", lambda *_args: True)
    ledger_id = checker.record_etransfer(
        reference_number="unlink-balance-ref",
        message_id="unlink-balance-message",
        sender_name="Receipt Client",
        amount=240.0,
        memo=f"Balance for booking #{booking_id}",
    )
    assert checker.apply_confirmed_balance_payment(
        booking_id,
        240.0,
        "unlink-balance-message",
        ledger_id=ledger_id,
    )
    assert _booking_payment_state(receipt_env, booking_id)[0] == 340.0

    with booking_app.app.test_client() as client:
        response = client.post(
            f"/admin/transfers/{ledger_id}/unlink",
            headers={"X-Admin-Key": "test-admin-key"},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert "cannot recall" in payload["operator_alert"]
    state = _booking_payment_state(receipt_env, booking_id)
    assert state[0] == 100.0
    assert state[1:] == (None, None, None)


def test_manual_and_stripe_balance_paths_send_one_receipt_each(
    receipt_env, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        booking_app,
        "_send_email_raw",
        lambda *args: sent.append(args) or True,
    )

    manual_id = _insert_booking(receipt_env, paid_amount=100.0)
    with booking_app.app.test_client() as client:
        response = client.post(
            "/admin/mark-paid",
            headers={"X-Admin-Key": "test-admin-key"},
            json={"booking_id": manual_id, "paid_amount": 340.0},
        )
    assert response.status_code == 200
    assert response.get_json()["receipt_status"] == "sent"
    assert response.get_json()["balance_due"] == 0.0

    stripe_id = _insert_booking(
        receipt_env, paid_amount=100.0, slot_time="18:40"
    )
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "sk_test_receipt")
    monkeypatch.setattr(booking_app, "STRIPE_WEBHOOK_SECRET", "whsec_receipt")
    import stripe

    stripe_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_balance_receipt",
                "amount_total": 24000,
                "metadata": {
                    "payment_type": "balance",
                    "booking_id": str(stripe_id),
                },
            }
        },
    }
    monkeypatch.setattr(
        stripe.Webhook,
        "construct_event",
        lambda *_args, **_kwargs: stripe_event,
    )
    with booking_app.app.test_client() as client:
        first = client.post(
            "/stripe/webhook",
            data=b"{}",
            headers={"Stripe-Signature": "test"},
        )
        second = client.post(
            "/stripe/webhook",
            data=b"{}",
            headers={"Stripe-Signature": "test"},
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert _booking_payment_state(receipt_env, stripe_id)[0] == 340.0
    assert len(sent) == 2
