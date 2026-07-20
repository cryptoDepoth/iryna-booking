import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

import app as booking_app
import check_etransfer_v2 as checker


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def atomic_env(monkeypatch, tmp_path):
    booking_db_path = str(tmp_path / "bookings.db")
    gift_db_path = str(tmp_path / "gift.db")
    monkeypatch.setattr(booking_app, "DB_PATH", booking_db_path)
    monkeypatch.setattr(booking_app.gift_db, "DB_PATH", gift_db_path)
    monkeypatch.setattr(checker, "DB_PATH", booking_db_path)
    monkeypatch.setenv("GIFT_REFERRAL_DB", gift_db_path)
    monkeypatch.setenv("TEST_MODE", "true")
    booking_app.init_db()
    booking_app.gift_db.init_db(gift_db_path)

    gift_dir = os.path.join(ROOT, "gift-referral")
    if gift_dir not in sys.path:
        sys.path.insert(0, gift_dir)
    import gift_referral_db as gift_db

    monkeypatch.setattr(gift_db, "DB_PATH", gift_db_path)
    gift_db.init_db(gift_db_path)
    return booking_db_path, gift_db_path, gift_db


def _insert_booking(
    db_path,
    *,
    name="Atomic Client",
    status="pending_payment",
    confirmed=0,
    paid=0,
    paid_amount=0.0,
    deposit_amount=120.50,
    full_price=241.00,
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id,
            created_at, reserved_until
        ) VALUES (
            '2026-08-09', '14:30', ?, 'atomic@example.com', '4035550100', '',
            'mini', ?, ?, ?, ?, ?, ?, 'atomic-event', ?, ?
        )
        """,
        (
            name,
            status,
            confirmed,
            paid,
            paid_amount,
            deposit_amount,
            full_price,
            (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
            (now + timedelta(hours=1)).isoformat(),
        ),
    )
    booking_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return booking_id


def _insert_transfer(db_path, message_id, amount):
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        INSERT INTO etransfers (
            reference_number, message_id, sender_name, amount, memo, direction,
            email_date, status, source
        ) VALUES (?, ?, 'Atomic Client', ?, 'Atomic payment', 'in',
                  '2026-07-19 12:00:00', 'unmatched', 'email')
        """,
        (f"msg:{message_id}", message_id, amount),
    )
    transfer_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return transfer_id


def _archive_transfer(db_path, transfer_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE etransfers
           SET status='ignored', archived_at='2026-07-19 12:01:00'
         WHERE id=?
        """,
        (transfer_id,),
    )
    conn.commit()
    conn.close()


def _booking_state(db_path, booking_id):
    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    conn.close()
    return booking


def _ownership_state(db_path, message_id):
    conn = sqlite3.connect(db_path)
    ledger = conn.execute(
        """
        SELECT status, matched_booking_id, matched_gift_code, archived_at
          FROM etransfers
         WHERE message_id=?
        """,
        (message_id,),
    ).fetchone()
    processed = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        (message_id,),
    ).fetchone()
    conn.close()
    return ledger, processed


def _install_archive_before_booking_transaction(
    monkeypatch,
    db_path,
    transfer_id,
):
    real = getattr(checker, "_apply_booking_payment_transaction", None)

    def archive_then_apply(*args, **kwargs):
        _archive_transfer(db_path, transfer_id)
        return real(*args, **kwargs) if real is not None else False

    monkeypatch.setattr(
        checker,
        "_apply_booking_payment_transaction",
        archive_then_apply,
        raising=False,
    )


def _install_archive_before_gift_transaction(
    monkeypatch,
    db_path,
    transfer_id,
):
    real = getattr(checker, "_apply_gift_payment_transaction", None)

    def archive_then_apply(*args, **kwargs):
        _archive_transfer(db_path, transfer_id)
        return real(*args, **kwargs) if real is not None else False

    monkeypatch.setattr(
        checker,
        "_apply_gift_payment_transaction",
        archive_then_apply,
        raising=False,
    )


@pytest.mark.parametrize(
    ("amount", "expected_status", "expected_paid_amount", "alert_name"),
    [
        (120.50, "pending_payment", 0.0, None),
        (150.00, "pending_payment", 0.0, "_notify_admin_overpaid"),
        (80.00, "pending_payment", 0.0, "_notify_admin_underpaid"),
    ],
)
def test_archive_wins_before_exact_overpaid_or_partial_booking_mutation(
    atomic_env,
    monkeypatch,
    amount,
    expected_status,
    expected_paid_amount,
    alert_name,
):
    db_path, _, _ = atomic_env
    message_id = f"archive-booking-{amount:.2f}"
    booking_id = _insert_booking(db_path)
    transfer_id = _insert_transfer(db_path, message_id, amount)
    _install_archive_before_booking_transaction(
        monkeypatch,
        db_path,
        transfer_id,
    )
    monkeypatch.setattr(
        checker,
        "read_message_body",
        lambda _message_id: f"You've received ${amount:.2f} from Atomic Client",
    )
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    alerts = []
    if alert_name:
        monkeypatch.setattr(
            checker,
            alert_name,
            lambda *args, **kwargs: alerts.append((args, kwargs)),
        )

    result = checker.check_single_email(
        {
            "id": message_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        checker.get_pending_bookings(within_minutes=30),
    )

    assert result == (None, None)
    assert _booking_state(db_path, booking_id) == (
        expected_status,
        0,
        0,
        expected_paid_amount,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("ignored", None, None, "2026-07-19 12:01:00")
    assert processed is None
    assert alerts == []


@pytest.mark.parametrize("terminal_status", ["cancelled", "expired"])
def test_cancel_or_expire_during_body_read_rolls_back_confirmation_claims(
    atomic_env,
    monkeypatch,
    terminal_status,
):
    db_path, _, _ = atomic_env
    message_id = f"{terminal_status}-during-body-read"
    booking_id = _insert_booking(db_path)
    _insert_transfer(db_path, message_id, 120.50)

    def release_booking_during_body_read(_message_id):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE bookings SET status=?, reserved_until=NULL WHERE id=?",
            (terminal_status, booking_id),
        )
        conn.commit()
        conn.close()
        return "You've received $120.50 from Atomic Client"

    monkeypatch.setattr(
        checker,
        "read_message_body",
        release_booking_during_body_read,
    )
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    effects = []
    monkeypatch.setattr(
        booking_app,
        "_after_auto_payment_confirmed",
        lambda confirmed_id: effects.append(confirmed_id),
    )
    stale_pending = checker.get_pending_bookings(within_minutes=30)

    confirmed_ids = booking_app._process_etransfer_email_batch(
        [
            {
                "id": message_id,
                "date": datetime.now(timezone.utc).isoformat(),
                "subject": "Interac e-Transfer: You've received $120.50",
                "from": {"addr": "notify@payments.interac.ca"},
            }
        ],
        stale_pending,
        [],
    )
    for confirmed_id in confirmed_ids:
        booking_app._after_auto_payment_confirmed(confirmed_id)

    assert confirmed_ids == []
    assert effects == []
    assert _booking_state(db_path, booking_id) == (
        terminal_status,
        0,
        0,
        0.0,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_reserved_deadline_elapsing_during_body_read_rolls_back_confirmation_claims(
    atomic_env,
    monkeypatch,
):
    db_path, _, _ = atomic_env
    message_id = "deadline-elapsed-during-body-read"
    clock = {"now": datetime(2026, 7, 19, 12, 0, 0)}
    monkeypatch.setattr(checker, "_utc_now", lambda: clock["now"])
    booking_id = _insert_booking(db_path, status="reserved")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE bookings
           SET created_at='2026-07-19 11:55:00',
               reserved_until='2026-07-19T06:01:00-06:00'
         WHERE id=?
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    _insert_transfer(db_path, message_id, 120.50)

    stale_pending = checker.get_pending_bookings(within_minutes=30)
    assert [booking["id"] for booking in stale_pending] == [booking_id]

    def advance_past_deadline(_message_id):
        clock["now"] = datetime(2026, 7, 19, 12, 2, 0)
        return "You've received $120.50 from Atomic Client"

    monkeypatch.setattr(checker, "read_message_body", advance_past_deadline)
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    effects = []
    monkeypatch.setattr(
        booking_app,
        "_after_auto_payment_confirmed",
        lambda confirmed_id: effects.append(confirmed_id),
    )

    confirmed_ids = booking_app._process_etransfer_email_batch(
        [
            {
                "id": message_id,
                "date": "2026-07-19T12:00:00+00:00",
                "subject": "Interac e-Transfer: You've received $120.50",
                "from": {"addr": "notify@payments.interac.ca"},
            }
        ],
        stale_pending,
        [],
    )
    for confirmed_id in confirmed_ids:
        booking_app._after_auto_payment_confirmed(confirmed_id)

    assert confirmed_ids == []
    assert effects == []
    assert _booking_state(db_path, booking_id) == (
        "reserved",
        0,
        0,
        0.0,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_full_price_change_during_body_read_rolls_back_confirmation_claims(
    atomic_env,
    monkeypatch,
):
    db_path, _, _ = atomic_env
    message_id = "full-price-changed-during-body-read"
    amount = 200.00
    booking_id = _insert_booking(
        db_path,
        deposit_amount=120.50,
        full_price=241.00,
    )
    _insert_transfer(db_path, message_id, amount)
    stale_pending = checker.get_pending_bookings(within_minutes=30)
    assert [booking["id"] for booking in stale_pending] == [booking_id]

    def reprice_during_body_read(_message_id):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE bookings SET full_price=150.00 WHERE id=?",
            (booking_id,),
        )
        conn.commit()
        conn.close()
        return f"You've received ${amount:.2f} from Atomic Client"

    monkeypatch.setattr(
        checker,
        "read_message_body",
        reprice_during_body_read,
    )
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    overpaid_alerts = []
    monkeypatch.setattr(
        checker,
        "_notify_admin_overpaid",
        lambda *args, **kwargs: overpaid_alerts.append((args, kwargs)),
    )
    effects = []
    monkeypatch.setattr(
        booking_app,
        "_after_auto_payment_confirmed",
        lambda confirmed_id: effects.append(confirmed_id),
    )

    confirmed_ids = booking_app._process_etransfer_email_batch(
        [
            {
                "id": message_id,
                "date": datetime.now(timezone.utc).isoformat(),
                "subject": f"Interac e-Transfer: You've received ${amount:.2f}",
                "from": {"addr": "notify@payments.interac.ca"},
            }
        ],
        stale_pending,
        [],
    )
    for confirmed_id in confirmed_ids:
        booking_app._after_auto_payment_confirmed(confirmed_id)

    assert confirmed_ids == []
    assert effects == []
    assert overpaid_alerts == []
    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        0,
        0.0,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_accepted_full_price_change_uses_live_notification_balance(
    atomic_env,
    monkeypatch,
):
    db_path, _, _ = atomic_env
    message_id = "accepted-full-price-change"
    booking_id = _insert_booking(
        db_path,
        deposit_amount=120.50,
        full_price=241.00,
    )
    _insert_transfer(db_path, message_id, 200.00)
    stale_pending = checker.get_pending_bookings(within_minutes=30)

    def reprice_during_body_read(_message_id):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE bookings SET full_price=300.00 WHERE id=?",
            (booking_id,),
        )
        conn.commit()
        conn.close()
        return "You've received $200.00 from Atomic Client"

    monkeypatch.setattr(checker, "read_message_body", reprice_during_body_read)
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    notifications = []
    monkeypatch.setattr(
        booking_app,
        "_notify_admin",
        lambda text: notifications.append(text),
    )

    result = checker.check_single_email(
        {
            "id": message_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        stale_pending,
    )

    assert result == (booking_id, None)
    assert len(notifications) == 1
    assert "Expected: $120.50" in notifications[0]
    assert "Remaining balance: $100.00" in notifications[0]


@pytest.mark.parametrize(
    ("amount", "updated_deposit", "expected_match_type"),
    [
        (120.50, 100.00, "overpaid"),
        (150.00, 150.00, "exact"),
    ],
)
def test_confirmation_uses_live_pricing_band_for_notification(
    atomic_env,
    monkeypatch,
    amount,
    updated_deposit,
    expected_match_type,
):
    db_path, _, _ = atomic_env
    message_id = f"live-pricing-band-{expected_match_type}"
    booking_id = _insert_booking(
        db_path,
        deposit_amount=120.50,
        full_price=241.00,
    )
    _insert_transfer(db_path, message_id, amount)
    monkeypatch.setattr(
        checker,
        "read_message_body",
        lambda _message_id: (
            f"You've received ${amount:.2f} from Atomic Client"
        ),
    )
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    real_confirm_booking = checker.confirm_booking

    def reprice_before_transaction(*args, **kwargs):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE bookings SET deposit_amount=? WHERE id=?",
            (updated_deposit, booking_id),
        )
        conn.commit()
        conn.close()
        return real_confirm_booking(*args, **kwargs)

    monkeypatch.setattr(
        checker,
        "confirm_booking",
        reprice_before_transaction,
    )
    alerts = []
    monkeypatch.setattr(
        checker,
        "_notify_admin_overpaid",
        lambda booking, expected, actual: alerts.append(
            (booking["deposit_amount"], expected, actual)
        ),
    )

    result = checker.check_single_email(
        {
            "id": message_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        checker.get_pending_bookings(within_minutes=30),
    )

    assert result == (booking_id, None)
    if expected_match_type == "overpaid":
        assert alerts == [(100.00, 100.00, 120.50)]
    else:
        assert alerts == []


@pytest.mark.parametrize("previously_processed", [False, True])
def test_archive_wins_before_both_reconciliation_mutations(
    atomic_env,
    monkeypatch,
    previously_processed,
):
    db_path, _, _ = atomic_env
    message_id = (
        "archive-reconcile-processed"
        if previously_processed
        else "archive-reconcile-new"
    )
    booking_id = _insert_booking(
        db_path,
        name="Reconcile Client",
        status="confirmed",
        confirmed=1,
        paid=1,
        paid_amount=100.00,
        deposit_amount=100.00,
    )
    transfer_id = _insert_transfer(db_path, message_id, 120.50)
    if previously_processed:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO processed_emails (message_id, booking_id, amount)
            VALUES (?, NULL, 120.50)
            """,
            (message_id,),
        )
        conn.commit()
        conn.close()
    _install_archive_before_booking_transaction(
        monkeypatch,
        db_path,
        transfer_id,
    )
    body = (
        "You've received $120.50 from Reconcile Client.\n"
        "Message: Reconcile Client August 9th 2:30 pm"
    )
    monkeypatch.setattr(checker, "read_message_body", lambda _message_id: body)
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    alerts = []
    monkeypatch.setattr(
        checker,
        "_notify_admin_reconciled",
        lambda *args, **kwargs: alerts.append((args, kwargs)),
    )

    result = checker.check_single_email(
        {
            "id": message_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        [],
        checker.get_reconciliation_bookings(within_days=120),
    )

    assert result == (None, None)
    assert _booking_state(db_path, booking_id) == (
        "confirmed",
        1,
        1,
        100.00,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("ignored", None, None, "2026-07-19 12:01:00")
    if previously_processed:
        assert processed == (None, 120.50)
    else:
        assert processed is None
    assert alerts == []


@pytest.mark.parametrize("include_code", [True, False])
def test_archive_wins_before_explicit_or_amount_only_gift_activation(
    atomic_env,
    monkeypatch,
    include_code,
):
    db_path, _, gift_db = atomic_env
    code = gift_db.create_gift_certificate(
        purchaser_email="buyer@example.com",
        purchaser_name="Buyer",
        recipient_name="Recipient",
        recipient_email="recipient@example.com",
        personal_message="Enjoy",
        session_type="mini",
        amount=210.0,
        amount_with_gst=220.50,
        payment_method="interac",
        payment_status="pending",
        paid_amount=0.0,
        status="pending_payment",
    )
    message_id = (
        "archive-gift-code"
        if include_code
        else "archive-gift-amount"
    )
    transfer_id = _insert_transfer(db_path, message_id, 220.50)
    _install_archive_before_gift_transaction(
        monkeypatch,
        db_path,
        transfer_id,
    )
    body = "You've received $220.50 from Gift Buyer"
    if include_code:
        body += f". Message: Gift certificate {code}"
    monkeypatch.setattr(checker, "read_message_body", lambda _message_id: body)
    external_calls = []
    monkeypatch.setattr(
        checker,
        "_gift_modules",
        lambda: (
            gift_db,
            lambda *args, **kwargs: external_calls.append("pdf"),
            lambda *args, **kwargs: external_calls.append("buyer_email"),
            lambda *args, **kwargs: external_calls.append("recipient_email"),
        ),
    )
    monkeypatch.setattr(
        checker,
        "_send_admin_alert",
        lambda *args, **kwargs: external_calls.append("admin_alert"),
    )

    result = checker.check_single_email(
        {
            "id": message_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        [],
    )

    assert result == (None, None)
    cert = gift_db.get_gift_certificate(code)
    assert cert["status"] == "pending_payment"
    assert cert["payment_status"] == "pending"
    assert cert["paid_amount"] == 0.0
    assert cert["payment_reference"] is None
    assert cert["activated_at"] is None
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("ignored", None, None, "2026-07-19 12:01:00")
    assert processed is None
    assert external_calls == []


@pytest.mark.parametrize(
    ("mutation", "amount", "booking_kwargs", "expected_booking"),
    [
        (
            "confirm",
            120.50,
            {},
            ("confirmed", 1, 1, 120.50),
        ),
        (
            "confirm",
            150.00,
            {},
            ("confirmed", 1, 1, 150.00),
        ),
        (
            "partial",
            80.00,
            {},
            ("partial_payment", 0, 0, 80.00),
        ),
        (
            "reconcile",
            120.50,
            {
                "status": "confirmed",
                "confirmed": 1,
                "paid": 1,
                "paid_amount": 100.00,
                "deposit_amount": 100.00,
            },
            ("confirmed", 1, 1, 120.50),
        ),
    ],
)
def test_writer_first_booking_commit_owns_ledger_and_rejects_archive(
    atomic_env,
    mutation,
    amount,
    booking_kwargs,
    expected_booking,
):
    db_path, _, _ = atomic_env
    message_id = f"writer-first-{mutation}-{amount:.2f}"
    booking_id = _insert_booking(db_path, **booking_kwargs)
    transfer_id = _insert_transfer(db_path, message_id, amount)

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        amount,
        mutation,
    )

    assert committed is True
    assert _booking_state(db_path, booking_id) == expected_booking
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("matched", booking_id, None, None)
    assert processed == (booking_id, amount)
    success, error, status, _meta = booking_app._mutate_transfer_archive_state(
        transfer_id
    )
    assert success is False
    assert error == "Matched transfers cannot be archived or restored"
    assert status == 409


def test_atomic_confirmation_rejects_already_paid_unconfirmed_booking(
    atomic_env,
):
    db_path, _, _ = atomic_env
    message_id = "already-paid-unconfirmed"
    booking_id = _insert_booking(
        db_path,
        status="pending_payment",
        confirmed=0,
        paid=1,
        paid_amount=120.50,
    )
    _insert_transfer(db_path, message_id, 120.50)

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        120.50,
        "confirm",
    )

    assert committed is False
    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        1,
        120.50,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_atomic_confirmation_revalidates_current_expected_amount(
    atomic_env,
):
    db_path, _, _ = atomic_env
    message_id = "expected-amount-repriced"
    booking_id = _insert_booking(
        db_path,
        deposit_amount=120.50,
        full_price=600.00,
    )
    _insert_transfer(db_path, message_id, 120.50)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE bookings SET deposit_amount=500.00 WHERE id=?",
        (booking_id,),
    )
    conn.commit()
    conn.close()

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        120.50,
        "confirm",
    )

    assert committed is False
    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        0,
        0.0,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


@pytest.mark.parametrize(
    ("session_type", "created_at"),
    [
        ("mini", datetime(2026, 7, 18, 11, 59, 59)),
        ("private", datetime(2026, 6, 4, 11, 59, 59)),
    ],
)
def test_atomic_confirmation_revalidates_candidate_age_windows(
    atomic_env,
    monkeypatch,
    session_type,
    created_at,
):
    db_path, _, _ = atomic_env
    message_id = f"candidate-window-{session_type}"
    now = datetime(2026, 7, 19, 12, 0, 0)
    monkeypatch.setattr(checker, "_utc_now", lambda: now)
    booking_id = _insert_booking(db_path, status="pending_payment")
    _insert_transfer(db_path, message_id, 120.50)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE bookings
           SET session_type=?, created_at=?
         WHERE id=?
        """,
        (
            session_type,
            created_at.strftime("%Y-%m-%d %H:%M:%S"),
            booking_id,
        ),
    )
    conn.commit()
    conn.close()

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        120.50,
        "confirm",
    )

    assert committed is False
    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        0,
        0.0,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_writer_first_processed_orphan_reconciliation_updates_existing_owner(
    atomic_env,
):
    db_path, _, _ = atomic_env
    message_id = "writer-first-processed-orphan"
    booking_id = _insert_booking(
        db_path,
        status="confirmed",
        confirmed=1,
        paid=1,
        paid_amount=100.00,
        deposit_amount=100.00,
    )
    _insert_transfer(db_path, message_id, 120.50)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES (?, NULL, 120.50)
        """,
        (message_id,),
    )
    conn.commit()
    conn.close()

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        120.50,
        "reconcile",
        allow_existing_orphan=True,
    )

    assert committed is True
    assert _booking_state(db_path, booking_id) == (
        "confirmed",
        1,
        1,
        120.50,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("matched", booking_id, None, None)
    assert processed == (booking_id, 120.50)


def test_booking_transaction_rolls_back_on_processed_email_conflict(atomic_env):
    db_path, _, _ = atomic_env
    message_id = "processed-conflict"
    booking_id = _insert_booking(db_path)
    _insert_transfer(db_path, message_id, 120.50)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES (?, 999, 120.50)
        """,
        (message_id,),
    )
    conn.commit()
    conn.close()

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        120.50,
        "confirm",
    )

    assert committed is False
    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        0,
        0.0,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed == (999, 120.50)


def test_duplicate_message_ledger_rows_fail_closed_without_financial_change(
    atomic_env,
):
    db_path, _, _ = atomic_env
    message_id = "duplicate-ledger-message"
    booking_id = _insert_booking(db_path)
    _insert_transfer(db_path, message_id, 120.50)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO etransfers (
            reference_number, message_id, sender_name, amount, direction,
            status, source
        ) VALUES (
            'duplicate-ledger-ref', ?, 'Duplicate Sender', 120.50, 'in',
            'unmatched', 'email'
        )
        """,
        (message_id,),
    )
    conn.commit()
    conn.close()

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        120.50,
        "confirm",
    )

    assert committed is False
    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        0,
        0.0,
    )
    conn = sqlite3.connect(db_path)
    statuses = conn.execute(
        """
        SELECT status, matched_booking_id, matched_gift_code
          FROM etransfers
         WHERE message_id=?
         ORDER BY id
        """,
        (message_id,),
    ).fetchall()
    processed = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id=?",
        (message_id,),
    ).fetchone()
    conn.close()
    assert statuses == [
        ("unmatched", None, None),
        ("unmatched", None, None),
    ]
    assert processed is None


@pytest.mark.parametrize(
    ("mutation", "booking_kwargs", "amount"),
    [
        (
            "confirm",
            {
                "status": "confirmed",
                "confirmed": 1,
                "paid": 1,
                "paid_amount": 120.50,
            },
            120.50,
        ),
        (
            "partial",
            {
                "status": "partial_payment",
                "paid_amount": 80.00,
            },
            80.00,
        ),
        (
            "reconcile",
            {
                "status": "confirmed",
                "confirmed": 1,
                "paid": 1,
                "paid_amount": 120.50,
            },
            120.50,
        ),
    ],
)
def test_failed_booking_predicate_rolls_back_all_ownership(
    atomic_env,
    mutation,
    booking_kwargs,
    amount,
):
    db_path, _, _ = atomic_env
    message_id = f"predicate-failure-{mutation}"
    booking_id = _insert_booking(db_path, **booking_kwargs)
    _insert_transfer(db_path, message_id, amount)
    before = _booking_state(db_path, booking_id)

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        amount,
        mutation,
    )

    assert committed is False
    assert _booking_state(db_path, booking_id) == before
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_injected_booking_exception_rolls_back_claims_and_financial_state(
    atomic_env,
    monkeypatch,
):
    db_path, _, _ = atomic_env
    message_id = "booking-injected-exception"
    booking_id = _insert_booking(db_path)
    _insert_transfer(db_path, message_id, 120.50)
    real_claim = checker._claim_processed_email

    def claim_then_raise(*args, **kwargs):
        assert real_claim(*args, **kwargs) is True
        raise RuntimeError("injected after ownership claims")

    monkeypatch.setattr(checker, "_claim_processed_email", claim_then_raise)

    with pytest.raises(RuntimeError, match="injected after ownership claims"):
        checker._apply_booking_payment_transaction(
            message_id,
            booking_id,
            120.50,
            "confirm",
        )

    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        0,
        0.0,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_booking_sql_exception_after_claims_rolls_back_every_mutation(
    atomic_env,
):
    db_path, _, _ = atomic_env
    message_id = "booking-sql-exception"
    booking_id = _insert_booking(db_path)
    _insert_transfer(db_path, message_id, 120.50)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TRIGGER reject_atomic_booking_update
        BEFORE UPDATE OF confirmed ON bookings
        WHEN NEW.confirmed=1
        BEGIN
            SELECT RAISE(ABORT, 'injected booking financial failure');
        END
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="injected booking"):
        checker._apply_booking_payment_transaction(
            message_id,
            booking_id,
            120.50,
            "confirm",
        )

    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        0,
        0.0,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_failed_processed_orphan_reconciliation_restores_original_owner(
    atomic_env,
):
    db_path, _, _ = atomic_env
    message_id = "processed-orphan-predicate-failure"
    booking_id = _insert_booking(
        db_path,
        status="confirmed",
        confirmed=1,
        paid=1,
        paid_amount=120.50,
        deposit_amount=100.00,
    )
    _insert_transfer(db_path, message_id, 120.50)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES (?, NULL, 120.50)
        """,
        (message_id,),
    )
    conn.commit()
    conn.close()

    committed = checker._apply_booking_payment_transaction(
        message_id,
        booking_id,
        120.50,
        "reconcile",
        allow_existing_orphan=True,
    )

    assert committed is False
    assert _booking_state(db_path, booking_id) == (
        "confirmed",
        1,
        1,
        120.50,
    )
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed == (None, 120.50)


@pytest.mark.parametrize(
    ("mutation", "booking_kwargs", "amount"),
    [
        ("confirm", {}, 120.50),
        ("partial", {}, 80.00),
        (
            "reconcile",
            {
                "status": "confirmed",
                "confirmed": 1,
                "paid": 1,
                "paid_amount": 100.00,
                "deposit_amount": 100.00,
            },
            120.50,
        ),
    ],
)
def test_concurrent_booking_claims_commit_each_financial_path_once(
    atomic_env,
    mutation,
    booking_kwargs,
    amount,
):
    db_path, _, _ = atomic_env
    message_id = f"concurrent-{mutation}"
    booking_id = _insert_booking(db_path, **booking_kwargs)
    _insert_transfer(db_path, message_id, amount)
    start = threading.Barrier(2)
    results = []
    errors = []

    def run():
        try:
            start.wait(timeout=5)
            results.append(
                checker._apply_booking_payment_transaction(
                    message_id,
                    booking_id,
                    amount,
                    mutation,
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(results) == [False, True]
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("matched", booking_id, None, None)
    assert processed == (booking_id, amount)


def _create_pending_gift(gift_db):
    return gift_db.create_gift_certificate(
        purchaser_email="buyer@example.com",
        purchaser_name="Buyer",
        recipient_name="Recipient",
        recipient_email="recipient@example.com",
        personal_message="Enjoy",
        session_type="mini",
        amount=210.0,
        amount_with_gst=220.50,
        payment_method="interac",
        payment_status="pending",
        paid_amount=0.0,
        status="pending_payment",
    )


def test_temp_booking_and_gift_databases_are_attach_compatible(atomic_env):
    _, _, gift_db = atomic_env
    conn = checker.get_db()
    try:
        assert checker._gift_database_is_attach_compatible(
            conn,
            gift_db.DB_PATH,
        ) is True
        assert conn.execute("PRAGMA main.journal_mode").fetchone()[0] == "delete"
        assert (
            conn.execute("PRAGMA gift_atomic.journal_mode").fetchone()[0]
            == "delete"
        )
    finally:
        conn.close()


def test_incompatible_gift_journal_mode_rejects_cross_database_commit(
    atomic_env,
):
    db_path, gift_db_path, gift_db = atomic_env
    message_id = "gift-wal-incompatible"
    code = _create_pending_gift(gift_db)
    _insert_transfer(db_path, message_id, 220.50)
    conn = sqlite3.connect(gift_db_path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()

    committed = checker._apply_gift_payment_transaction(
        gift_db,
        message_id,
        code,
        220.50,
    )

    assert committed is False
    cert = gift_db.get_gift_certificate(code)
    assert cert["status"] == "pending_payment"
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_writer_first_gift_commit_owns_both_databases_and_rejects_archive(
    atomic_env,
):
    db_path, _, gift_db = atomic_env
    message_id = "writer-first-gift"
    code = _create_pending_gift(gift_db)
    transfer_id = _insert_transfer(db_path, message_id, 220.50)

    committed = checker._apply_gift_payment_transaction(
        gift_db,
        message_id,
        code,
        220.50,
    )

    assert committed is True
    cert = gift_db.get_gift_certificate(code)
    assert cert["status"] == "active"
    assert cert["payment_status"] == "paid"
    assert cert["paid_amount"] == 220.50
    assert cert["payment_reference"] == message_id
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("matched", None, code, None)
    assert processed == (None, 220.50)
    success, error, status, _meta = booking_app._mutate_transfer_archive_state(
        transfer_id
    )
    assert success is False
    assert error == "Matched transfers cannot be archived or restored"
    assert status == 409


def test_gift_financial_predicate_failure_rolls_back_main_database_claims(
    atomic_env,
):
    db_path, _, gift_db = atomic_env
    message_id = "gift-predicate-failure"
    code = _create_pending_gift(gift_db)
    gift_db.mark_gift_payment_confirmed(code, 220.50, "manual-before-scan")
    _insert_transfer(db_path, message_id, 220.50)
    before = gift_db.get_gift_certificate(code)

    committed = checker._apply_gift_payment_transaction(
        gift_db,
        message_id,
        code,
        220.50,
    )

    assert committed is False
    assert gift_db.get_gift_certificate(code) == before
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_gift_processed_email_conflict_rolls_back_attached_activation(
    atomic_env,
):
    db_path, _, gift_db = atomic_env
    message_id = "gift-processed-conflict"
    code = _create_pending_gift(gift_db)
    _insert_transfer(db_path, message_id, 220.50)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES (?, 999, 220.50)
        """,
        (message_id,),
    )
    conn.commit()
    conn.close()

    committed = checker._apply_gift_payment_transaction(
        gift_db,
        message_id,
        code,
        220.50,
    )

    assert committed is False
    cert = gift_db.get_gift_certificate(code)
    assert cert["status"] == "pending_payment"
    assert cert["payment_status"] == "pending"
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed == (999, 220.50)


def test_amount_only_gift_candidate_is_revalidated_inside_attached_transaction(
    atomic_env,
):
    db_path, _, gift_db = atomic_env
    message_id = "gift-amount-became-ambiguous"
    first_code = _create_pending_gift(gift_db)
    second_code = _create_pending_gift(gift_db)
    _insert_transfer(db_path, message_id, 220.50)

    committed = checker._apply_gift_payment_transaction(
        gift_db,
        message_id,
        first_code,
        220.50,
        unique_amount_match=True,
        email_received_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    assert committed is False
    assert gift_db.get_gift_certificate(first_code)["status"] == "pending_payment"
    assert gift_db.get_gift_certificate(second_code)["status"] == "pending_payment"
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_amount_only_gift_timing_is_revalidated_inside_attached_transaction(
    atomic_env,
):
    db_path, _, gift_db = atomic_env
    message_id = "gift-amount-became-too-new"
    code = _create_pending_gift(gift_db)
    _insert_transfer(db_path, message_id, 220.50)

    committed = checker._apply_gift_payment_transaction(
        gift_db,
        message_id,
        code,
        220.50,
        unique_amount_match=True,
        email_received_at=datetime(2020, 1, 1),
    )

    assert committed is False
    assert gift_db.get_gift_certificate(code)["status"] == "pending_payment"
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_injected_gift_exception_rolls_back_attached_and_main_databases(
    atomic_env,
    monkeypatch,
):
    db_path, _, gift_db = atomic_env
    message_id = "gift-injected-exception"
    code = _create_pending_gift(gift_db)
    _insert_transfer(db_path, message_id, 220.50)
    real_claim = checker._claim_processed_email

    def claim_then_raise(*args, **kwargs):
        assert real_claim(*args, **kwargs) is True
        raise RuntimeError("injected attached transaction failure")

    monkeypatch.setattr(checker, "_claim_processed_email", claim_then_raise)

    with pytest.raises(RuntimeError, match="injected attached transaction failure"):
        checker._apply_gift_payment_transaction(
            gift_db,
            message_id,
            code,
            220.50,
        )

    cert = gift_db.get_gift_certificate(code)
    assert cert["status"] == "pending_payment"
    assert cert["payment_status"] == "pending"
    assert cert["paid_amount"] == 0.0
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_gift_sql_exception_after_claims_rolls_back_both_databases(
    atomic_env,
):
    db_path, gift_db_path, gift_db = atomic_env
    message_id = "gift-sql-exception"
    code = _create_pending_gift(gift_db)
    _insert_transfer(db_path, message_id, 220.50)
    conn = sqlite3.connect(gift_db_path)
    conn.execute(
        """
        CREATE TRIGGER reject_atomic_gift_update
        BEFORE UPDATE OF status ON gift_certificates
        WHEN NEW.status='active'
        BEGIN
            SELECT RAISE(ABORT, 'injected gift financial failure');
        END
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="injected gift"):
        checker._apply_gift_payment_transaction(
            gift_db,
            message_id,
            code,
            220.50,
        )

    cert = gift_db.get_gift_certificate(code)
    assert cert["status"] == "pending_payment"
    assert cert["payment_status"] == "pending"
    assert cert["paid_amount"] == 0.0
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("unmatched", None, None, None)
    assert processed is None


def test_concurrent_gift_claims_activate_certificate_once(atomic_env):
    db_path, _, gift_db = atomic_env
    message_id = "concurrent-gift"
    code = _create_pending_gift(gift_db)
    _insert_transfer(db_path, message_id, 220.50)
    start = threading.Barrier(2)
    results = []
    errors = []

    def run():
        try:
            start.wait(timeout=5)
            results.append(
                checker._apply_gift_payment_transaction(
                    gift_db,
                    message_id,
                    code,
                    220.50,
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(results) == [False, True]
    cert = gift_db.get_gift_certificate(code)
    assert cert["status"] == "active"
    assert cert["payment_status"] == "paid"
    ledger, processed = _ownership_state(db_path, message_id)
    assert ledger == ("matched", None, code, None)
    assert processed == (None, 220.50)


def test_gift_external_side_effects_observe_durable_atomic_commit(
    atomic_env,
    monkeypatch,
):
    db_path, _, gift_db = atomic_env
    message_id = "gift-side-effects-after-commit"
    code = _create_pending_gift(gift_db)
    _insert_transfer(db_path, message_id, 220.50)
    observed = []

    def assert_committed(label):
        cert = gift_db.get_gift_certificate(code)
        ledger, processed = _ownership_state(db_path, message_id)
        assert cert["status"] == "active"
        assert ledger == ("matched", None, code, None)
        assert processed == (None, 220.50)
        observed.append(label)

    monkeypatch.setattr(
        checker,
        "_gift_modules",
        lambda: (
            gift_db,
            lambda _cert: assert_committed("pdf") or None,
            lambda _cert, pdf_path=None: assert_committed("buyer_email"),
            lambda _cert: assert_committed("recipient_email"),
        ),
    )
    monkeypatch.setattr(
        checker,
        "_send_admin_alert",
        lambda _text: assert_committed("admin_alert"),
    )
    body = f"You've received $220.50. Message: Gift certificate {code}"

    handled = checker.try_confirm_gift_etransfer(
        220.50,
        body,
        message_id,
        email_received_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    assert handled == "committed"
    assert observed == ["pdf", "buyer_email", "recipient_email", "admin_alert"]


def test_overpayment_notification_observes_durable_atomic_commit(
    atomic_env,
    monkeypatch,
):
    db_path, _, _ = atomic_env
    message_id = "booking-side-effects-after-commit"
    booking_id = _insert_booking(db_path)
    _insert_transfer(db_path, message_id, 150.00)
    observed = []
    monkeypatch.setattr(
        checker,
        "read_message_body",
        lambda _message_id: "You've received $150.00 from Atomic Client",
    )
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )

    def assert_committed(*_args, **_kwargs):
        assert _booking_state(db_path, booking_id) == (
            "confirmed",
            1,
            1,
            150.00,
        )
        ledger, processed = _ownership_state(db_path, message_id)
        assert ledger == ("matched", booking_id, None, None)
        assert processed == (booking_id, 150.00)
        observed.append("overpaid")

    monkeypatch.setattr(checker, "_notify_admin_overpaid", assert_committed)

    result = checker.check_single_email(
        {
            "id": message_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        checker.get_pending_bookings(within_minutes=30),
    )

    assert result == (booking_id, None)
    assert observed == ["overpaid"]


def test_underpayment_notification_observes_durable_atomic_commit(
    atomic_env,
    monkeypatch,
):
    db_path, _, _ = atomic_env
    message_id = "partial-side-effects-after-commit"
    booking_id = _insert_booking(db_path)
    _insert_transfer(db_path, message_id, 80.00)
    monkeypatch.setattr(
        checker,
        "read_message_body",
        lambda _message_id: "You've received $80.00 from Atomic Client",
    )
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    observed = []

    def assert_committed(*_args, **_kwargs):
        assert _booking_state(db_path, booking_id) == (
            "partial_payment",
            0,
            0,
            80.00,
        )
        ledger, processed = _ownership_state(db_path, message_id)
        assert ledger == ("matched", booking_id, None, None)
        assert processed == (booking_id, 80.00)
        observed.append("underpaid")

    monkeypatch.setattr(checker, "_notify_admin_underpaid", assert_committed)

    result = checker.check_single_email(
        {
            "id": message_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        checker.get_pending_bookings(within_minutes=30),
    )

    assert result == (None, None)
    assert observed == ["underpaid"]


@pytest.mark.parametrize("previously_processed", [False, True])
def test_reconciliation_notification_observes_durable_atomic_commit(
    atomic_env,
    monkeypatch,
    previously_processed,
):
    db_path, _, _ = atomic_env
    message_id = (
        "reconcile-new-side-effects"
        if not previously_processed
        else "reconcile-processed-side-effects"
    )
    booking_id = _insert_booking(
        db_path,
        name="Reconcile Client",
        status="confirmed",
        confirmed=1,
        paid=1,
        paid_amount=100.00,
        deposit_amount=100.00,
    )
    _insert_transfer(db_path, message_id, 120.50)
    if previously_processed:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO processed_emails (message_id, booking_id, amount)
            VALUES (?, NULL, 120.50)
            """,
            (message_id,),
        )
        conn.commit()
        conn.close()
    body = (
        "You've received $120.50 from Reconcile Client.\n"
        "Message: Reconcile Client August 9th 2:30 pm"
    )
    monkeypatch.setattr(checker, "read_message_body", lambda _message_id: body)
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    observed = []

    def assert_committed(*_args, **_kwargs):
        assert _booking_state(db_path, booking_id) == (
            "confirmed",
            1,
            1,
            120.50,
        )
        ledger, processed = _ownership_state(db_path, message_id)
        assert ledger == ("matched", booking_id, None, None)
        assert processed == (booking_id, 120.50)
        observed.append("reconciled")

    monkeypatch.setattr(checker, "_notify_admin_reconciled", assert_committed)

    result = checker.check_single_email(
        {
            "id": message_id,
            "date": datetime.now(timezone.utc).isoformat(),
        },
        [],
        checker.get_reconciliation_bookings(within_days=120),
    )

    assert result == (None, None)
    assert observed == ["reconciled"]


def test_archive_winning_batch_never_runs_booking_post_commit_effects(
    atomic_env,
    monkeypatch,
):
    db_path, _, _ = atomic_env
    message_id = "archive-batch-side-effects"
    booking_id = _insert_booking(db_path)
    transfer_id = _insert_transfer(db_path, message_id, 120.50)
    _install_archive_before_booking_transaction(
        monkeypatch,
        db_path,
        transfer_id,
    )
    monkeypatch.setattr(
        checker,
        "read_message_body",
        lambda _message_id: "You've received $120.50 from Atomic Client",
    )
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    effects = []
    monkeypatch.setattr(
        booking_app,
        "_after_auto_payment_confirmed",
        lambda confirmed_id: effects.append(confirmed_id),
    )

    confirmed_ids = booking_app._process_etransfer_email_batch(
        [
            {
                "id": message_id,
                "date": datetime.now(timezone.utc).isoformat(),
                "subject": "Interac e-Transfer: You've received $120.50",
                "from": {"addr": "notify@payments.interac.ca"},
            }
        ],
        checker.get_pending_bookings(within_minutes=30),
        [],
    )
    for confirmed_id in confirmed_ids:
        booking_app._after_auto_payment_confirmed(confirmed_id)

    assert confirmed_ids == []
    assert effects == []
    assert _booking_state(db_path, booking_id) == (
        "pending_payment",
        0,
        0,
        0.0,
    )


@pytest.mark.parametrize("eligible_status", ["reserved", "pending_payment"])
def test_writer_winning_batch_runs_booking_effects_after_durable_commit(
    atomic_env,
    monkeypatch,
    eligible_status,
):
    db_path, _, _ = atomic_env
    message_id = f"writer-batch-side-effects-{eligible_status}"
    booking_id = _insert_booking(db_path, status=eligible_status)
    _insert_transfer(db_path, message_id, 120.50)
    monkeypatch.setattr(
        checker,
        "read_message_body",
        lambda _message_id: "You've received $120.50 from Atomic Client",
    )
    monkeypatch.setattr(
        checker,
        "try_confirm_gift_etransfer",
        lambda *args, **kwargs: False,
    )
    effects = []

    def assert_committed(confirmed_id):
        assert confirmed_id == booking_id
        assert _booking_state(db_path, booking_id) == (
            "confirmed",
            1,
            1,
            120.50,
        )
        ledger, processed = _ownership_state(db_path, message_id)
        assert ledger == ("matched", booking_id, None, None)
        assert processed == (booking_id, 120.50)
        effects.append(confirmed_id)

    monkeypatch.setattr(
        booking_app,
        "_after_auto_payment_confirmed",
        assert_committed,
    )

    confirmed_ids = booking_app._process_etransfer_email_batch(
        [
            {
                "id": message_id,
                "date": datetime.now(timezone.utc).isoformat(),
                "subject": "Interac e-Transfer: You've received $120.50",
                "from": {"addr": "notify@payments.interac.ca"},
            }
        ],
        checker.get_pending_bookings(within_minutes=30),
        [],
    )
    for confirmed_id in confirmed_ids:
        booking_app._after_auto_payment_confirmed(confirmed_id)

    assert confirmed_ids == [booking_id]
    assert effects == [booking_id]
