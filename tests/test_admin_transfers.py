import io
import json
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone

import pytest

import app as booking_app
import check_etransfer_v2 as checker


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "bookings.db")
    gift_db_path = str(tmp_path / "gift_referral.db")
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app.gift_db, "DB_PATH", gift_db_path)
    monkeypatch.setattr(checker, "DB_PATH", db_path)
    booking_app.init_db()
    booking_app.gift_db.init_db(gift_db_path)
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        yield c, booking_app, db_path, gift_db_path


def _login(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True


def _insert_transfer(app, **overrides):
    values = {
        "reference_number": "test-ref-active",
        "message_id": "message-active",
        "sender_name": "Active Sender",
        "amount": 120.50,
        "memo": "Mission mini deposit",
        "direction": "in",
        "email_date": "2026-07-18 10:30:00",
        "matched_booking_id": None,
        "matched_gift_code": None,
        "status": "unmatched",
        "source": "email",
        "created_at": "2026-07-18 10:31:00",
        "archived_at": None,
        "restored_at": None,
    }
    values.update(overrides)
    conn = app.db_conn()
    cursor = conn.execute(
        """
        INSERT INTO etransfers (
            reference_number, message_id, sender_name, amount, memo, direction,
            email_date, matched_booking_id, matched_gift_code, status, source,
            created_at, archived_at, restored_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(values[key] for key in values),
    )
    transfer_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return transfer_id


def _transfer_row(app, transfer_id):
    conn = app.db_conn()
    row = conn.execute("SELECT * FROM etransfers WHERE id=?", (transfer_id,)).fetchone()
    conn.close()
    return dict(row)


def _set_transfer_payment_provenance(
    app,
    transfer_id,
    *,
    mutation,
    prior_status,
    prior_confirmed,
    prior_paid,
    prior_paid_amount,
):
    conn = app.db_conn()
    conn.execute(
        """
        UPDATE etransfers
           SET payment_mutation=?,
               prior_booking_status=?,
               prior_booking_confirmed=?,
               prior_booking_paid=?,
               prior_booking_paid_amount=?
         WHERE id=?
        """,
        (
            mutation,
            prior_status,
            prior_confirmed,
            prior_paid,
            prior_paid_amount,
            transfer_id,
        ),
    )
    conn.commit()
    conn.close()


def _insert_confirmed_auto_payment(
    app,
    *,
    message_id,
    reference_number,
    amount=120.50,
):
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-10', '12:40', 'Delayed Callback Client',
            'delayed-callback@example.com', '4035550299', '', 'Mission Mini',
            'confirmed', 1, 1, ?, ?, 241.00, 'mission-mini-10'
        )
        """,
        (amount, amount),
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES (?, ?, ?)
        """,
        (message_id, booking_id, amount),
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number=reference_number,
        message_id=message_id,
        amount=amount,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )
    return booking_id, transfer_id


def _forbid_payment_hooks(monkeypatch, app, *, include_link=True):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("triage must not invoke matching or payment hooks")

    names = [
        "_auto_link_etransfers",
        "_after_auto_payment_confirmed",
        "notify_payment_confirmed",
        "sync_to_notion",
    ]
    if include_link:
        names.append("_link_transfer_to_booking")
    for name in names:
        monkeypatch.setattr(app, name, forbidden, raising=False)
    monkeypatch.setattr(app.gift_db, "mark_gift_payment_confirmed", forbidden)
    return calls


def _forbid_manual_link_confirmation_effects(monkeypatch, app):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(
            "manual transfer linking must not emit confirmation side effects"
        )

    for name in (
        "_after_auto_payment_confirmed",
        "notify_payment_confirmed",
        "create_calendar_event_for_booking",
        "_send_client_email",
        "_notify_admin",
    ):
        monkeypatch.setattr(app, name, forbidden)
    return calls


def test_etransfer_archive_migration_is_idempotent(monkeypatch, tmp_path):
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE etransfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_number TEXT UNIQUE,
            message_id TEXT,
            sender_name TEXT,
            amount REAL,
            memo TEXT,
            direction TEXT DEFAULT 'in',
            email_date TEXT,
            matched_booking_id INTEGER,
            matched_gift_code TEXT,
            status TEXT DEFAULT 'unmatched',
            source TEXT DEFAULT 'email',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT INTO etransfers (
            reference_number, message_id, sender_name, amount, memo, direction,
            email_date, status, source, created_at
        ) VALUES (
            'legacy-ref', 'legacy-message', 'Legacy Sender', 88.25, 'Legacy memo',
            'in', '2026-07-17 09:00:00', 'unmatched', 'csv', '2026-07-17 09:01:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO etransfers (
            reference_number, message_id, sender_name, amount, memo, direction,
            email_date, status, source, created_at
        ) VALUES (
            'legacy-ignored-ref', 'legacy-ignored-message', 'Ignored Sender',
            99.50, 'Ignored memo', 'in', '2026-07-16 09:00:00', 'ignored',
            'email', '2026-07-16 09:01:00'
        )
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)

    booking_app._ensure_etransfers_table()
    booking_app._ensure_etransfers_table()

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(etransfers)")}
    row = conn.execute(
        """
        SELECT reference_number, message_id, sender_name, amount, memo, direction,
               email_date, status, source, created_at, archived_at, restored_at
          FROM etransfers
         WHERE reference_number='legacy-ref'
        """
    ).fetchone()
    ignored_archived_at = conn.execute(
        "SELECT archived_at FROM etransfers WHERE reference_number='legacy-ignored-ref'"
    ).fetchone()[0]
    conn.close()

    assert {
        "archived_at",
        "restored_at",
        "unlinked_at",
        "unlinked_booking_id",
        "unlink_external_status",
        "unlink_external_details",
        "unlink_external_reconciled_at",
        "payment_mutation",
        "prior_booking_status",
        "prior_booking_confirmed",
        "prior_booking_paid",
        "prior_booking_paid_amount",
    } <= columns
    assert row == (
        "legacy-ref",
        "legacy-message",
        "Legacy Sender",
        88.25,
        "Legacy memo",
        "in",
        "2026-07-17 09:00:00",
        "unmatched",
        "csv",
        "2026-07-17 09:01:00",
        None,
        None,
    )
    assert ignored_archived_at


def test_admin_transfers_page_imports_csv_and_renders(client):
    c, _, _, _ = client
    _login(c)
    csv_data = "# ,Date,Sender,Amount ($),Direction,Subject\n1,2026-05-30,Yulia Levitskaya and it,120.50,IN ←,Interac e-Transfer: You've received $120.50 from Yulia Levitskaya and it has been automatically deposited.\n"
    resp = c.post(
        "/admin/transfers/import",
        data={"csv": (io.BytesIO(csv_data.encode()), "interac.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["inserted"] == 1

    page = c.get("/admin/transfers")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Yulia Levitskaya" in html
    assert "$120.50" in html
    assert "unmatched" in html


def test_admin_transfer_link_updates_booking_paid_amount(client, monkeypatch):
    c, app, _, _ = client
    _login(c)
    confirmation_effects = _forbid_manual_link_confirmation_effects(monkeypatch, app)
    with app.app.app_context():
        import app as booking_app
        conn = booking_app.db_conn()
        conn.execute("""
            INSERT INTO bookings (date, time, name, email, phone, instagram, session_type,
                                  status, confirmed, paid, paid_amount, deposit_amount, full_price, event_id)
            VALUES ('2026-07-04','13:30','Yulia Levitskaya','yulia@example.com','403','',
                    'Canoe Mini Session','pending_payment',0,0,0,110.25,220.50,'canoe-jul4')
        """)
        booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("""
            INSERT INTO etransfers (reference_number, sender_name, amount, memo, direction, email_date, status, source)
            VALUES ('test-ref-12050','Yulia Levitskaya',120.50,'Canoe mini session July 4 1:30 pm','in','2026-05-30','unmatched','csv')
        """)
        transfer_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit(); conn.close()

    resp = c.post(f"/admin/transfers/{transfer_id}/link", json={"booking_id": booking_id})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    with app.app.app_context():
        import app as booking_app
        conn = booking_app.db_conn()
        b = conn.execute("SELECT paid_amount, confirmed, paid, status FROM bookings WHERE id=?", (booking_id,)).fetchone()
        t = conn.execute("SELECT matched_booking_id, status FROM etransfers WHERE id=?", (transfer_id,)).fetchone()
        conn.close()
    assert round(b["paid_amount"], 2) == 120.50
    assert b["confirmed"] == 1
    assert b["paid"] == 1
    assert b["status"] == "confirmed"
    assert t["matched_booking_id"] == booking_id
    assert t["status"] == "matched"
    assert confirmation_effects == []


def test_admin_transfer_link_uses_max_for_distinct_partial_transfers(client, monkeypatch):
    c, app, _, _ = client
    _login(c)
    confirmation_effects = _forbid_manual_link_confirmation_effects(monkeypatch, app)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type,
            status, confirmed, paid, paid_amount, deposit_amount, full_price,
            event_id
        ) VALUES (
            '2026-08-20', '11:00', 'Cumulative Partial Client',
            'cumulative@example.com', '4035550300', '', 'Mission Mini',
            'pending_payment', 0, 0, 0, 100.00, 200.00, 'mission-mini-20'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    transfer_ids = []
    for reference, message_id, amount in (
        ("cumulative-partial-80", "cumulative-partial-message-80", 80.00),
        ("cumulative-partial-20", "cumulative-partial-message-20", 20.00),
    ):
        cursor = conn.execute(
            """
            INSERT INTO etransfers (
                reference_number, message_id, sender_name, amount, direction,
                email_date, status, source
            ) VALUES (?, ?, 'Cumulative Partial Sender', ?, 'in',
                      '2026-07-20 10:00:00', 'unmatched', 'email')
            """,
            (reference, message_id, amount),
        )
        transfer_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()

    first = c.post(
        f"/admin/transfers/{transfer_ids[0]}/link",
        json={"booking_id": booking_id},
    )
    assert first.status_code == 200
    conn = app.db_conn()
    after_first = tuple(
        conn.execute(
            """
            SELECT paid_amount, confirmed, paid, status
              FROM bookings
             WHERE id=?
            """,
            (booking_id,),
        ).fetchone()
    )
    conn.close()
    assert after_first == (80.0, 0, 0, "partial_payment")

    second = c.post(
        f"/admin/transfers/{transfer_ids[1]}/link",
        json={"booking_id": booking_id},
    )
    assert second.status_code == 200

    repeated_first = c.post(
        f"/admin/transfers/{transfer_ids[0]}/link",
        json={"booking_id": booking_id},
    )
    repeated_second = c.post(
        f"/admin/transfers/{transfer_ids[1]}/link",
        json={"booking_id": booking_id},
    )
    assert repeated_first.status_code == 400
    assert repeated_second.status_code == 400

    conn = app.db_conn()
    booking = tuple(
        conn.execute(
            """
            SELECT paid_amount, confirmed, paid, status
              FROM bookings
             WHERE id=?
            """,
            (booking_id,),
        ).fetchone()
    )
    transfers = conn.execute(
        """
        SELECT id, matched_booking_id, status
          FROM etransfers
         WHERE id IN (?, ?)
         ORDER BY id
        """,
        transfer_ids,
    ).fetchall()
    conn.close()

    assert booking == (80.0, 0, 0, "partial_payment")
    assert [
        (row["id"], row["matched_booking_id"], row["status"])
        for row in transfers
    ] == [
        (transfer_ids[0], booking_id, "matched"),
        (transfer_ids[1], booking_id, "matched"),
    ]
    assert confirmation_effects == []


def test_admin_transfer_link_does_not_double_count_existing_paid_amount(
    client,
    monkeypatch,
):
    c, app, _, _ = client
    _login(c)
    confirmation_effects = _forbid_manual_link_confirmation_effects(monkeypatch, app)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type,
            status, confirmed, paid, paid_amount, deposit_amount, full_price,
            event_id
        ) VALUES (
            '2026-08-20', '11:20', 'Existing Payment Client',
            'existing-payment@example.com', '4035550399', '', 'Mission Mini',
            'partial_payment', 0, 0, 80.00, 100.00, 200.00, 'mission-mini-20'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    cursor = conn.execute(
        """
        INSERT INTO etransfers (
            reference_number, message_id, sender_name, amount, direction,
            email_date, status, source
        ) VALUES (
            'existing-payment-ref', 'existing-payment-message',
            'Existing Payment Sender', 80.00, 'in',
            '2026-07-20 10:15:00', 'unmatched', 'csv'
        )
        """
    )
    transfer_id = cursor.lastrowid
    conn.commit()
    conn.close()

    response = c.post(
        f"/admin/transfers/{transfer_id}/link",
        json={"booking_id": booking_id},
    )

    assert response.status_code == 200
    conn = app.db_conn()
    booking = tuple(
        conn.execute(
            """
            SELECT paid_amount, confirmed, paid, status
              FROM bookings
             WHERE id=?
            """,
            (booking_id,),
        ).fetchone()
    )
    transfer = tuple(
        conn.execute(
            """
            SELECT matched_booking_id, status
              FROM etransfers
             WHERE id=?
            """,
            (transfer_id,),
        ).fetchone()
    )
    conn.close()

    assert booking == (80.0, 0, 0, "partial_payment")
    assert transfer == (booking_id, "matched")
    assert confirmation_effects == []


def test_admin_transfer_link_rolls_back_when_atomic_ledger_claim_loses(client):
    c, app, _, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type,
            status, confirmed, paid, paid_amount, deposit_amount, full_price,
            event_id
        ) VALUES (
            '2026-08-20', '11:30', 'Claim Conflict Client',
            'claim-conflict@example.com', '4035550301', '', 'Mission Mini',
            'partial_payment', 0, 0, 25.00, 100.00, 200.00, 'mission-mini-20'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    cursor = conn.execute(
        """
        INSERT INTO etransfers (
            reference_number, message_id, sender_name, amount, direction,
            email_date, status, source
        ) VALUES (
            'claim-conflict-ref', 'claim-conflict-message',
            'Claim Conflict Sender', 50.00, 'in',
            '2026-07-20 10:30:00', 'unmatched', 'email'
        )
        """
    )
    transfer_id = cursor.lastrowid
    conn.execute(
        f"""
        CREATE TRIGGER simulate_manual_link_claim_conflict
        BEFORE UPDATE OF paid_amount ON bookings
        WHEN NEW.id={booking_id}
        BEGIN
            UPDATE etransfers SET status='ignored' WHERE id={transfer_id};
        END
        """
    )
    conn.commit()
    conn.close()

    response = c.post(
        f"/admin/transfers/{transfer_id}/link",
        json={"booking_id": booking_id},
    )

    assert response.status_code == 400
    assert "changed" in response.get_json()["error"].lower()
    conn = app.db_conn()
    booking = tuple(
        conn.execute(
            """
            SELECT paid_amount, confirmed, paid, status
              FROM bookings
             WHERE id=?
            """,
            (booking_id,),
        ).fetchone()
    )
    transfer = tuple(
        conn.execute(
            """
            SELECT matched_booking_id, status
              FROM etransfers
             WHERE id=?
            """,
            (transfer_id,),
        ).fetchone()
    )
    conn.close()

    assert booking == (25.0, 0, 0, "partial_payment")
    assert transfer == (None, "unmatched")


def test_admin_transfer_link_uses_max_without_unconfirming_booking(client):
    c, app, _, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type,
            status, confirmed, paid, paid_amount, deposit_amount, full_price,
            event_id
        ) VALUES (
            '2026-08-20', '12:00', 'Confirmed Cumulative Client',
            'confirmed-cumulative@example.com', '4035550302', '',
            'Mission Mini', 'confirmed', 1, 1, 40.00, 120.50, 241.00,
            'mission-mini-20'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    cursor = conn.execute(
        """
        INSERT INTO etransfers (
            reference_number, message_id, sender_name, amount, direction,
            email_date, status, source
        ) VALUES (
            'confirmed-cumulative-ref', 'confirmed-cumulative-message',
            'Confirmed Cumulative Sender', 20.00, 'in',
            '2026-07-20 11:00:00', 'unmatched', 'email'
        )
        """
    )
    transfer_id = cursor.lastrowid
    conn.commit()
    conn.close()

    response = c.post(
        f"/admin/transfers/{transfer_id}/link",
        json={"booking_id": booking_id},
    )

    assert response.status_code == 200
    conn = app.db_conn()
    booking = tuple(
        conn.execute(
            """
            SELECT paid_amount, confirmed, paid, status
              FROM bookings
             WHERE id=?
            """,
            (booking_id,),
        ).fetchone()
    )
    transfer = tuple(
        conn.execute(
            """
            SELECT matched_booking_id, status
              FROM etransfers
             WHERE id=?
            """,
            (transfer_id,),
        ).fetchone()
    )
    conn.close()

    assert booking == (40.0, 1, 1, "confirmed")
    assert transfer == (booking_id, "matched")


def test_archive_persists_filters_active_queue_and_keeps_auditable_history(client):
    c, app, db_path, _ = client
    _login(c)
    transfer_id = _insert_transfer(
        app,
        reference_number="archive-ref-001",
        message_id="archive-message-001",
        sender_name="Archive Review Sender",
        amount=147.25,
        memo="Maternity deposit July 19",
        email_date="2026-07-18 14:05:00",
        source="csv",
        created_at="2026-07-18 14:06:00",
    )
    original = _transfer_row(app, transfer_id)

    active_before = c.get("/admin/transfers")
    assert active_before.status_code == 200
    assert "Archive Review Sender" in active_before.get_data(as_text=True)

    response = c.post(f"/admin/transfers/{transfer_id}/archive", json={})
    assert response.status_code == 200
    assert response.get_json()["success"] is True

    fresh_conn = sqlite3.connect(db_path)
    fresh_conn.row_factory = sqlite3.Row
    archived = dict(
        fresh_conn.execute("SELECT * FROM etransfers WHERE id=?", (transfer_id,)).fetchone()
    )
    fresh_conn.close()

    assert archived["status"] == "ignored"
    assert archived["archived_at"]
    assert archived["restored_at"] is None
    repeated_archive = c.post(f"/admin/transfers/{transfer_id}/archive", json={})
    assert repeated_archive.status_code == 200
    assert repeated_archive.get_json()["already_archived"] is True
    assert _transfer_row(app, transfer_id)["archived_at"] == archived["archived_at"]
    for field in (
        "reference_number",
        "message_id",
        "sender_name",
        "amount",
        "memo",
        "direction",
        "email_date",
        "matched_booking_id",
        "matched_gift_code",
        "source",
        "created_at",
    ):
        assert archived[field] == original[field]

    active_after = c.get("/admin/transfers")
    assert active_after.status_code == 200
    assert "Archive Review Sender" not in active_after.get_data(as_text=True)

    history = c.get("/admin/transfers?status=ignored&direction=in")
    assert history.status_code == 200
    html = history.get_data(as_text=True)
    for value in (
        "Archive Review Sender",
        "archive-ref-001",
        "archive-message-001",
        "Maternity deposit July 19",
        "2026-07-18 14:05:00",
        "2026-07-18 14:06:00",
        "csv",
        "Archived",
        "Restore",
    ):
        assert value in html


def test_archive_and_restore_require_admin_authentication(client):
    c, app, _, _ = client
    active_id = _insert_transfer(app, reference_number="auth-active")
    archived_id = _insert_transfer(
        app,
        reference_number="auth-archived",
        status="ignored",
        archived_at="2026-07-18 15:00:00",
    )
    active_before = _transfer_row(app, active_id)
    archived_before = _transfer_row(app, archived_id)

    archive_response = c.post(f"/admin/transfers/{active_id}/archive", json={})
    restore_response = c.post(f"/admin/transfers/{archived_id}/restore", json={})

    assert archive_response.status_code == 401
    assert restore_response.status_code == 401
    assert _transfer_row(app, active_id) == active_before
    assert _transfer_row(app, archived_id) == archived_before


def test_archived_transfer_cannot_be_linked_from_a_stale_admin_page(client, monkeypatch):
    c, app, _, _ = client
    _login(c)
    hook_calls = _forbid_payment_hooks(monkeypatch, app, include_link=False)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-03', '12:00', 'Stale Link Client', 'stale@example.com',
            '4035550103', '', 'Mission Mini', 'pending_payment', 0, 0, 0,
            120.50, 241.00, 'mission-mini-3'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    booking_before = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
    )
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="stale-link-ref",
        message_id="stale-link-message",
        sender_name="Stale Link Sender",
        amount=120.50,
    )

    assert c.post(f"/admin/transfers/{transfer_id}/archive", json={}).status_code == 200
    archived = _transfer_row(app, transfer_id)
    link_response = c.post(
        f"/admin/transfers/{transfer_id}/link",
        json={"booking_id": booking_id},
    )

    assert link_response.status_code == 400
    assert "active, inbound, unmatched" in link_response.get_json()["error"]
    assert _transfer_row(app, transfer_id) == archived
    conn = app.db_conn()
    booking_after = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
    )
    conn.close()
    assert booking_after == booking_before
    assert hook_calls == []


def test_csv_reimport_does_not_change_archived_financial_history(client):
    c, app, _, _ = client
    _login(c)
    ref = app._transfer_ref_from_csv(
        "77",
        "2026-07-15",
        "Original CSV Sender",
        "88.25",
    )
    transfer_id = _insert_transfer(
        app,
        reference_number=ref,
        message_id=None,
        sender_name="Original CSV Sender",
        amount=88.25,
        memo="Original memo",
        email_date="2026-07-15",
        source="csv",
    )
    assert c.post(f"/admin/transfers/{transfer_id}/archive", json={}).status_code == 200
    archived = _transfer_row(app, transfer_id)
    csv_data = (
        "#,Date,Sender,Amount ($),Direction,Subject\n"
        "77,2026-07-15,Original CSV Sender,88.25,OUT,Changed memo\n"
    )

    response = c.post(
        "/admin/transfers/import",
        data={"csv": (io.BytesIO(csv_data.encode()), "interac.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["skipped"] == 1
    assert _transfer_row(app, transfer_id) == archived


def test_archived_email_is_skipped_by_real_matcher_boundary(client, monkeypatch):
    _, app, _, _ = client
    transfer_id = _insert_transfer(
        app,
        reference_number="archived-watcher-ref",
        message_id="archived-watcher-message",
        sender_name="Archived Watcher Sender",
        amount=120.50,
        status="ignored",
        archived_at="2026-07-18 18:00:00",
    )
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("archived email reached a payment or matching hook")

    for name in (
        "read_message_body",
        "try_confirm_gift_etransfer",
        "match_by_amount_only",
        "match_reconciliation_payment",
        "reconcile_confirmed_payment",
        "mark_message_processed",
    ):
        monkeypatch.setattr(checker, name, forbidden)

    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "archived-watcher-message", "date": "2026-07-18 18:00:00"},
        [{"id": 1, "deposit_amount": 120.50}],
        [{"id": 2, "paid_amount": 0}],
    )

    assert confirmed_id is None
    assert ambiguous is None
    assert calls == []
    assert _transfer_row(app, transfer_id)["status"] == "ignored"


def test_invalid_persisted_status_does_not_report_archive_success(client):
    c, app, _, _ = client
    _login(c)
    transfer_id = _insert_transfer(
        app,
        reference_number="invalid-status-ref",
        status="UNMATCHED",
    )
    before = _transfer_row(app, transfer_id)

    response = c.post(f"/admin/transfers/{transfer_id}/archive", json={})

    assert response.status_code == 409
    assert _transfer_row(app, transfer_id) == before


def test_restore_is_idempotent_and_does_not_change_payment_state(client, monkeypatch):
    c, app, _, gift_db_path = client
    _login(c)
    hook_calls = _forbid_payment_hooks(monkeypatch, app)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-01', '10:00', 'Same Amount Client', 'same@example.com',
            '4035550101', '', 'Mission Mini', 'pending_payment', 0, 0, 0,
            120.50, 241.00, 'mission-mini-1'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    booking_before = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
    )
    conn.close()
    gift_conn = sqlite3.connect(gift_db_path)
    gift_conn.execute(
        """
        INSERT INTO gift_certificates (
            code, purchaser_email, purchaser_name, session_type, amount,
            amount_with_gst, payment_method, payment_status, paid_amount, status
        ) VALUES (
            'GIFT-TEST-0001', 'buyer@example.com', 'Gift Buyer', 'Mission Mini',
            114.76, 120.50, 'etransfer', 'pending', 0, 'pending_payment'
        )
        """
    )
    gift_conn.commit()
    gift_before = gift_conn.execute(
        """
        SELECT status, payment_status, paid_amount, payment_reference, activated_at
          FROM gift_certificates
         WHERE code='GIFT-TEST-0001'
        """
    ).fetchone()
    gift_conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="restore-ref-001",
        message_id="restore-message-001",
        sender_name="Same Amount Unmatched",
        amount=120.50,
    )
    original = _transfer_row(app, transfer_id)

    archived_response = c.post(f"/admin/transfers/{transfer_id}/archive", json={})
    assert archived_response.status_code == 200
    archived = _transfer_row(app, transfer_id)
    assert archived["status"] == "ignored"
    assert archived["archived_at"]

    first_restore = c.post(f"/admin/transfers/{transfer_id}/restore", json={})
    assert first_restore.status_code == 200
    restored = _transfer_row(app, transfer_id)
    assert restored["status"] == "unmatched"
    assert restored["archived_at"] == archived["archived_at"]
    assert restored["restored_at"]

    repeated_restore = c.post(f"/admin/transfers/{transfer_id}/restore", json={})
    assert repeated_restore.status_code == 200
    assert repeated_restore.get_json()["already_active"] is True
    restored_again = _transfer_row(app, transfer_id)
    assert restored_again["restored_at"] == restored["restored_at"]
    for field in (
        "reference_number",
        "message_id",
        "sender_name",
        "amount",
        "memo",
        "direction",
        "email_date",
        "matched_booking_id",
        "matched_gift_code",
        "source",
        "created_at",
    ):
        assert restored_again[field] == original[field]

    conn = app.db_conn()
    booking_after = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
    )
    conn.close()
    gift_conn = sqlite3.connect(gift_db_path)
    gift_after = gift_conn.execute(
        """
        SELECT status, payment_status, paid_amount, payment_reference, activated_at
          FROM gift_certificates
         WHERE code='GIFT-TEST-0001'
        """
    ).fetchone()
    gift_conn.close()

    assert booking_after == booking_before
    assert gift_after == gift_before
    assert hook_calls == []
    assert "Same Amount Unmatched" in c.get("/admin/transfers").get_data(as_text=True)


def test_matched_booking_and_gift_transfers_cannot_be_archived_or_restored(client, monkeypatch):
    c, app, _, gift_db_path = client
    _login(c)
    hook_calls = _forbid_payment_hooks(monkeypatch, app)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-02', '11:00', 'Matched Client', 'matched@example.com',
            '4035550102', '', 'Mission Mini', 'confirmed', 1, 1, 120.50,
            120.50, 241.00, 'mission-mini-2'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    booking_before = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
    )
    conn.close()
    gift_conn = sqlite3.connect(gift_db_path)
    gift_conn.execute(
        """
        INSERT INTO gift_certificates (
            code, purchaser_email, purchaser_name, session_type, amount,
            amount_with_gst, payment_method, payment_status, paid_amount,
            payment_reference, activated_at, status
        ) VALUES (
            'GIFT-TEST-0002', 'paid@example.com', 'Paid Gift Buyer', 'Mission Mini',
            114.76, 120.50, 'etransfer', 'paid', 120.50,
            'gift-message-002', '2026-07-18 16:00:00', 'active'
        )
        """
    )
    gift_conn.commit()
    gift_before = gift_conn.execute(
        """
        SELECT status, payment_status, paid_amount, payment_reference, activated_at
          FROM gift_certificates
         WHERE code='GIFT-TEST-0002'
        """
    ).fetchone()
    gift_conn.close()
    booking_transfer_id = _insert_transfer(
        app,
        reference_number="matched-booking-ref",
        message_id="matched-booking-message",
        matched_booking_id=booking_id,
        status="matched",
    )
    gift_transfer_id = _insert_transfer(
        app,
        reference_number="matched-gift-ref",
        message_id="gift-message-002",
        matched_gift_code="GIFT-TEST-0002",
        status="matched",
    )
    booking_transfer_before = _transfer_row(app, booking_transfer_id)
    gift_transfer_before = _transfer_row(app, gift_transfer_id)

    for transfer_id in (booking_transfer_id, gift_transfer_id):
        archive_response = c.post(f"/admin/transfers/{transfer_id}/archive", json={})
        restore_response = c.post(f"/admin/transfers/{transfer_id}/restore", json={})
        assert archive_response.status_code == 409
        assert restore_response.status_code == 409

    assert _transfer_row(app, booking_transfer_id) == booking_transfer_before
    assert _transfer_row(app, gift_transfer_id) == gift_transfer_before
    conn = app.db_conn()
    booking_after = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
    )
    conn.close()
    gift_conn = sqlite3.connect(gift_db_path)
    gift_after = gift_conn.execute(
        """
        SELECT status, payment_status, paid_amount, payment_reference, activated_at
          FROM gift_certificates
         WHERE code='GIFT-TEST-0002'
        """
    ).fetchone()
    gift_conn.close()

    assert booking_after == booking_before
    assert gift_after == gift_before
    assert hook_calls == []


def test_unlink_reverses_real_auto_payment_ownership_without_reuse(
    client,
    monkeypatch,
):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id,
            created_at, reserved_until
        ) VALUES (
            '2026-08-10', '13:00', 'Original Auto Client',
            'original-auto@example.com', '4035550200', '', 'Mission Mini',
            'pending_payment', 0, 0, 0, 120.50, 241.00, 'mission-mini-10',
            '2026-07-18 10:00:00', '2026-08-10 13:15:00'
        )
        """
    )
    original_booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    message_id = "unlink-real-auto-message"
    scan_now = datetime(2026, 7, 18, 11, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(checker, "_utc_now", lambda: scan_now)
    body = (
        "Interac e-Transfer: You've received $120.50 from Original Auto Client.\n"
        "Reference Number: CA1234567890\n"
        "Sent From: Original Auto Client"
    )
    monkeypatch.setattr(checker, "read_message_body", lambda _message_id: body)
    monkeypatch.setattr(checker, "try_confirm_gift_etransfer", lambda *a, **k: False)
    pending = checker.get_pending_bookings(within_minutes=30)

    confirmed_id, ambiguous = checker.check_single_email(
        {"id": message_id, "date": "2026-07-18 11:00:00+00:00"},
        pending,
    )

    assert confirmed_id == original_booking_id
    assert ambiguous is None
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id,
            created_at, reserved_until
        ) VALUES (
            '2026-08-10', '13:30', 'Second Credit Target',
            'second-credit@example.com', '4035550201', '', 'Mission Mini',
            'pending_payment', 0, 0, 0, 120.50, 241.00, 'mission-mini-10',
            '2026-07-18 10:00:00', '2026-08-10 13:45:00'
        )
        """
    )
    second_booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db_path)
    transfer_id = conn.execute(
        "SELECT id FROM etransfers WHERE message_id=?",
        (message_id,),
    ).fetchone()[0]
    conn.close()

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    row = _transfer_row(app, transfer_id)
    assert row["matched_booking_id"] is None
    assert row["matched_gift_code"] is None
    assert row["status"] == "unlinked"
    assert row["unlinked_booking_id"] == original_booking_id
    assert row["unlinked_at"]
    assert row["payment_mutation"] == "confirm"
    assert row["prior_booking_status"] == "pending_payment"
    assert row["prior_booking_confirmed"] == 0
    assert row["prior_booking_paid"] == 0
    assert float(row["prior_booking_paid_amount"] or 0) == 0
    conn = sqlite3.connect(db_path)
    original_booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (original_booking_id,),
    ).fetchone()
    processed = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        (message_id,),
    ).fetchone()
    conn.close()
    assert original_booking == ("pending_payment", 0, 0, 0.0)
    assert processed == (None, 120.50)

    relink = c.post(
        f"/admin/transfers/{transfer_id}/link",
        json={"booking_id": second_booking_id},
    )
    assert relink.status_code == 400
    conn = app.db_conn()
    second_booking = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (second_booking_id,),
        ).fetchone()
    )
    conn.close()
    assert second_booking == ("pending_payment", 0, 0, 0.0)
    assert app._auto_link_etransfers() == 0
    assert _transfer_row(app, transfer_id)["status"] == "unlinked"
    rescanned_id, rescanned_ambiguous = checker.check_single_email(
        {"id": message_id, "date": "2026-07-18 11:00:00+00:00"},
        [
            {
                "id": second_booking_id,
                "date": "2026-08-10",
                "time": "13:30",
                "name": "Second Credit Target",
                "status": "pending_payment",
                "confirmed": 0,
                "paid": 0,
                "paid_amount": 0.0,
                "deposit_amount": 120.50,
                "full_price": 241.00,
                "event_id": "mission-mini-10",
                "created_at": "2026-07-18 10:00:00",
            }
        ],
    )
    assert rescanned_id is None
    assert rescanned_ambiguous is None
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (second_booking_id,),
    ).fetchone() == ("pending_payment", 0, 0, 0.0)
    conn.close()
    history = c.get("/admin/transfers?status=unlinked")
    assert history.status_code == 200
    history_html = history.get_data(as_text=True)
    assert "Unlinked history" in history_html
    assert f"Previously #{original_booking_id}" in history_html
    assert "Audit history only" in history_html


def test_delayed_confirmation_callback_skips_all_effects_after_unlink(
    client,
    monkeypatch,
):
    c, app, db_path, _ = client
    _login(c)
    message_id = "unlink-delayed-callback-message"
    booking_id, transfer_id = _insert_confirmed_auto_payment(
        app,
        message_id=message_id,
        reference_number="unlink-delayed-callback-ref",
    )
    effects = []
    monkeypatch.setattr(
        app,
        "create_calendar_event_for_booking",
        lambda target: effects.append(("calendar", target)),
    )
    monkeypatch.setattr(
        app,
        "sync_to_notion",
        lambda target: effects.append(("notion", target)),
    )
    monkeypatch.setattr(
        app,
        "_send_client_email",
        lambda **kwargs: effects.append(("email", kwargs["booking_id"])),
    )
    monkeypatch.setattr(
        app,
        "notify_payment_confirmed",
        lambda target, amount=None: effects.append(("telegram", target)),
    )
    monkeypatch.setattr(
        app,
        "_record_booking_funnel_event",
        lambda booking, event, details=None: effects.append(("funnel", booking["id"])),
    )
    monkeypatch.setattr(
        app,
        "get_event_by_id",
        lambda event_id: {
            "id": event_id,
            "date": "2026-08-10",
            "title": "Mission Mini",
        },
    )
    monkeypatch.setattr(
        app,
        "delete_calendar_event_for_booking",
        lambda *args, **kwargs: {"status": "not_created"},
    )
    monkeypatch.setattr(
        app,
        "update_notion_after_transfer_unlink",
        lambda *args, **kwargs: {"status": "not_created"},
    )

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})
    assert response.status_code == 200

    app._after_auto_payment_confirmed(booking_id, message_id=message_id)

    assert effects == []
    conn = sqlite3.connect(db_path)
    booking_state = conn.execute(
        """
        SELECT status, confirmed, paid, paid_amount,
               calendar_event_id, calendar_event_url, notion_page_id
          FROM bookings
         WHERE id=?
        """,
        (booking_id,),
    ).fetchone()
    transfer_state = conn.execute(
        """
        SELECT status, matched_booking_id, unlinked_booking_id,
               unlink_external_status
          FROM etransfers
         WHERE id=?
        """,
        (transfer_id,),
    ).fetchone()
    conn.close()
    assert booking_state == (
        "pending_payment",
        0,
        0,
        0.0,
        None,
        None,
        None,
    )
    assert transfer_state == (
        "unlinked",
        None,
        booking_id,
        "corrected_with_irreversible_notifications",
    )


def test_confirmation_callback_preserves_effects_for_current_payment_owner(
    client,
    monkeypatch,
):
    _, app, db_path, _ = client
    message_id = "valid-delayed-callback-message"
    booking_id, transfer_id = _insert_confirmed_auto_payment(
        app,
        message_id=message_id,
        reference_number="valid-delayed-callback-ref",
    )
    effects = []
    monkeypatch.setattr(
        app,
        "create_calendar_event_for_booking",
        lambda target: effects.append(("calendar", target)) or "calendar-url",
    )
    monkeypatch.setattr(
        app,
        "sync_to_notion",
        lambda target: effects.append(("notion", target)),
    )
    monkeypatch.setattr(
        app,
        "_send_client_email",
        lambda **kwargs: effects.append(("email", kwargs["booking_id"])),
    )
    monkeypatch.setattr(
        app,
        "notify_payment_confirmed",
        lambda target, amount=None: effects.append(("telegram", target, amount)),
    )
    monkeypatch.setattr(
        app,
        "_record_booking_funnel_event",
        lambda booking, event, details=None: effects.append(
            ("funnel", booking["id"], event)
        ),
    )
    monkeypatch.setattr(
        app,
        "get_event_by_id",
        lambda event_id: {
            "id": event_id,
            "date": "2026-08-10",
            "title": "Mission Mini",
        },
    )

    app._after_auto_payment_confirmed(booking_id, message_id=message_id)

    assert effects == [
        ("calendar", booking_id),
        ("notion", booking_id),
        ("email", booking_id),
        ("telegram", booking_id, 120.50),
        ("funnel", booking_id, "booking_confirmed"),
    ]
    conn = sqlite3.connect(db_path)
    booking_state = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    transfer_state = conn.execute(
        "SELECT status, matched_booking_id FROM etransfers WHERE id=?",
        (transfer_id,),
    ).fetchone()
    conn.close()
    assert booking_state == ("confirmed", 1, 1, 120.50)
    assert transfer_state == ("matched", booking_id)


def test_unlink_waits_for_valid_callback_then_reconciles_its_effects(
    client,
    monkeypatch,
):
    _, app, db_path, _ = client
    message_id = "callback-first-interleaving-message"
    booking_id, transfer_id = _insert_confirmed_auto_payment(
        app,
        message_id=message_id,
        reference_number="callback-first-interleaving-ref",
    )
    calendar_entered = threading.Event()
    release_calendar = threading.Event()
    unlink_started = threading.Event()
    callback_errors = []
    unlink_results = []
    effects = []

    def blocking_calendar(target):
        calendar_entered.set()
        if not release_calendar.wait(timeout=2):
            raise AssertionError("test did not release the Calendar provider")
        effects.append(("calendar", target))
        return "calendar-url"

    monkeypatch.setattr(app, "create_calendar_event_for_booking", blocking_calendar)
    monkeypatch.setattr(
        app,
        "sync_to_notion",
        lambda target: effects.append(("notion", target)),
    )
    monkeypatch.setattr(
        app,
        "_send_client_email",
        lambda **kwargs: effects.append(("email", kwargs["booking_id"])),
    )
    monkeypatch.setattr(
        app,
        "notify_payment_confirmed",
        lambda target, amount=None: effects.append(("telegram", target)),
    )
    monkeypatch.setattr(
        app,
        "_record_booking_funnel_event",
        lambda booking, event, details=None: effects.append(("funnel", booking["id"])),
    )
    monkeypatch.setattr(
        app,
        "get_event_by_id",
        lambda event_id: {
            "id": event_id,
            "date": "2026-08-10",
            "title": "Mission Mini",
        },
    )
    monkeypatch.setattr(
        app,
        "delete_calendar_event_for_booking",
        lambda *args, **kwargs: {"status": "not_created"},
    )
    monkeypatch.setattr(
        app,
        "update_notion_after_transfer_unlink",
        lambda *args, **kwargs: {"status": "not_created"},
    )

    def run_callback():
        try:
            app._after_auto_payment_confirmed(
                booking_id,
                message_id=message_id,
            )
        except Exception as exc:
            callback_errors.append(exc)

    def run_unlink():
        with app.app.test_client() as unlink_client:
            _login(unlink_client)
            unlink_started.set()
            response = unlink_client.post(
                f"/admin/transfers/{transfer_id}/unlink",
                json={},
            )
            unlink_results.append((response.status_code, response.get_json()))

    callback_thread = threading.Thread(target=run_callback)
    callback_thread.start()
    assert calendar_entered.wait(timeout=2)

    unlink_thread = threading.Thread(target=run_unlink)
    unlink_thread.start()
    assert unlink_started.wait(timeout=2)
    unlink_thread.join(timeout=0.1)
    assert unlink_thread.is_alive()

    conn = sqlite3.connect(db_path)
    state_while_callback_runs = conn.execute(
        """
        SELECT b.status, b.confirmed, b.paid, e.status, e.matched_booking_id
          FROM bookings b
          JOIN etransfers e ON e.id=?
         WHERE b.id=?
        """,
        (transfer_id, booking_id),
    ).fetchone()
    conn.close()
    assert state_while_callback_runs == (
        "confirmed",
        1,
        1,
        "matched",
        booking_id,
    )

    release_calendar.set()
    callback_thread.join(timeout=2)
    unlink_thread.join(timeout=2)
    assert not callback_thread.is_alive()
    assert not unlink_thread.is_alive()
    assert callback_errors == []
    assert unlink_results[0][0] == 200
    assert unlink_results[0][1]["success"] is True
    assert effects == [
        ("calendar", booking_id),
        ("notion", booking_id),
        ("email", booking_id),
        ("telegram", booking_id),
        ("funnel", booking_id),
    ]

    conn = sqlite3.connect(db_path)
    final_state = conn.execute(
        """
        SELECT b.status, b.confirmed, b.paid, e.status, e.matched_booking_id
          FROM bookings b
          JOIN etransfers e ON e.id=?
         WHERE b.id=?
        """,
        (transfer_id, booking_id),
    ).fetchone()
    conn.close()
    assert final_state == ("pending_payment", 0, 0, "unlinked", None)


def test_old_confirmation_callback_skips_booking_owned_by_new_transfer(
    client,
    monkeypatch,
):
    c, app, _, _ = client
    _login(c)
    old_message_id = "old-confirmation-owner-message"
    booking_id, old_transfer_id = _insert_confirmed_auto_payment(
        app,
        message_id=old_message_id,
        reference_number="old-confirmation-owner-ref",
    )
    monkeypatch.setattr(
        app,
        "delete_calendar_event_for_booking",
        lambda *args, **kwargs: {"status": "not_created"},
    )
    monkeypatch.setattr(
        app,
        "update_notion_after_transfer_unlink",
        lambda *args, **kwargs: {"status": "not_created"},
    )
    response = c.post(f"/admin/transfers/{old_transfer_id}/unlink", json={})
    assert response.status_code == 200

    new_message_id = "new-confirmation-owner-message"
    conn = app.db_conn()
    conn.execute(
        """
        UPDATE bookings
           SET status='confirmed', confirmed=1, paid=1, paid_amount=120.50
         WHERE id=?
        """,
        (booking_id,),
    )
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES (?, ?, 120.50)
        """,
        (new_message_id, booking_id),
    )
    conn.commit()
    conn.close()
    new_transfer_id = _insert_transfer(
        app,
        reference_number="new-confirmation-owner-ref",
        message_id=new_message_id,
        amount=120.50,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        new_transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )
    effects = []
    monkeypatch.setattr(
        app,
        "create_calendar_event_for_booking",
        lambda target: effects.append(("calendar", target)),
    )
    monkeypatch.setattr(
        app,
        "sync_to_notion",
        lambda target: effects.append(("notion", target)),
    )
    monkeypatch.setattr(
        app,
        "_send_client_email",
        lambda **kwargs: effects.append(("email", kwargs["booking_id"])),
    )
    monkeypatch.setattr(
        app,
        "notify_payment_confirmed",
        lambda target, amount=None: effects.append(("telegram", target)),
    )

    app._after_auto_payment_confirmed(
        booking_id,
        message_id=old_message_id,
    )

    assert effects == []


def test_confirmation_callback_contains_provider_exception_without_db_mutation(
    client,
    monkeypatch,
):
    _, app, db_path, _ = client
    message_id = "callback-provider-failure-message"
    booking_id, transfer_id = _insert_confirmed_auto_payment(
        app,
        message_id=message_id,
        reference_number="callback-provider-failure-ref",
    )
    later_effects = []

    def fail_calendar(target):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(app, "create_calendar_event_for_booking", fail_calendar)
    monkeypatch.setattr(
        app,
        "sync_to_notion",
        lambda target: later_effects.append(("notion", target)),
    )
    monkeypatch.setattr(
        app,
        "_send_client_email",
        lambda **kwargs: later_effects.append(("email", kwargs["booking_id"])),
    )
    monkeypatch.setattr(
        app,
        "notify_payment_confirmed",
        lambda target, amount=None: later_effects.append(("telegram", target)),
    )

    app._after_auto_payment_confirmed(booking_id, message_id=message_id)

    assert later_effects == []
    conn = sqlite3.connect(db_path)
    booking_state = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed_state = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        (message_id,),
    ).fetchone()
    transfer_state = conn.execute(
        "SELECT status, matched_booking_id FROM etransfers WHERE id=?",
        (transfer_id,),
    ).fetchone()
    conn.close()
    assert booking_state == ("confirmed", 1, 1, 120.50)
    assert processed_state == (booking_id, 120.50)
    assert transfer_state == ("matched", booking_id)


def test_unlink_reconciles_confirmation_external_state_after_ownership_commit(
    client,
    monkeypatch,
):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id,
            calendar_event_id, calendar_event_url, notion_page_id
        ) VALUES (
            '2026-08-10', '13:20', 'External Reconciliation Client',
            'external-reconciliation@example.com', '4035550207', '',
            'Mission Mini', 'confirmed', 1, 1, 120.50, 120.50, 241.00,
            'mission-mini-10', 'calendar-external-1',
            'https://calendar.example/calendar-external-1', 'notion-external-1'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES ('unlink-external-success-message', ?, 120.50)
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-external-success-ref",
        message_id="unlink-external-success-message",
        amount=120.50,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )
    provider_calls = []

    def fake_delete(target_booking_id, event_id=None, expected_state=None):
        conn = sqlite3.connect(db_path)
        booking_state = conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
        transfer_state = conn.execute(
            "SELECT status, matched_booking_id FROM etransfers WHERE id=?",
            (transfer_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE bookings
               SET calendar_event_id=NULL,
                   calendar_event_url=NULL
             WHERE id=?
            """,
            (booking_id,),
        )
        conn.commit()
        conn.close()
        provider_calls.append(("calendar", target_booking_id, event_id))
        assert booking_state == ("pending_payment", 0, 0, 0.0)
        assert transfer_state == ("unlinked", None)
        assert expected_state == {
            "status": "pending_payment",
            "confirmed": 0,
            "paid": 0,
            "paid_amount": 0.0,
        }
        return {"status": "removed", "event_id": event_id}

    def fake_notion(target_booking_id, expected_state=None):
        conn = sqlite3.connect(db_path)
        state = conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
        conn.close()
        provider_calls.append(("notion", target_booking_id))
        assert state == ("pending_payment", 0, 0, 0.0)
        assert expected_state == {
            "status": "pending_payment",
            "confirmed": 0,
            "paid": 0,
            "paid_amount": 0.0,
        }
        return {"status": "updated", "page_id": "notion-external-1"}

    monkeypatch.setattr(app, "delete_calendar_event_for_booking", fake_delete)
    monkeypatch.setattr(app, "update_notion_after_transfer_unlink", fake_notion)
    monkeypatch.setattr(
        app,
        "_notify_admin",
        lambda *args, **kwargs: pytest.fail(
            "unlink operator warning must not emit another Telegram notification"
        ),
    )
    monkeypatch.setattr(
        app,
        "_send_client_email",
        lambda *args, **kwargs: pytest.fail(
            "unlink must not send another client email"
        ),
    )

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert (
        payload["external_reconciliation"]["status"]
        == "corrected_with_irreversible_notifications"
    )
    assert payload["external_reconciliation"]["calendar"]["status"] == "removed"
    assert payload["external_reconciliation"]["notion"]["status"] == "updated"
    assert "client confirmation email" in payload["operator_alert"].lower()
    assert "admin telegram" in payload["operator_alert"].lower()
    assert "cannot be recalled" in payload["operator_alert"].lower()
    assert provider_calls == [
        ("calendar", booking_id, "calendar-external-1"),
        ("notion", booking_id),
    ]

    conn = sqlite3.connect(db_path)
    booking_after = conn.execute(
        """
        SELECT status, confirmed, paid, paid_amount,
               calendar_event_id, calendar_event_url, notion_page_id
          FROM bookings
         WHERE id=?
        """,
        (booking_id,),
    ).fetchone()
    audit_row = conn.execute(
        """
        SELECT unlink_external_status, unlink_external_details,
               unlink_external_reconciled_at
          FROM etransfers
         WHERE id=?
        """,
        (transfer_id,),
    ).fetchone()
    conn.close()
    assert booking_after == (
        "pending_payment",
        0,
        0,
        0.0,
        None,
        None,
        "notion-external-1",
    )
    assert audit_row[0] == "corrected_with_irreversible_notifications"
    audit = json.loads(audit_row[1])
    assert audit["calendar"]["status"] == "removed"
    assert audit["notion"]["status"] == "updated"
    assert audit["notifications"]["client_email"] == "possibly_sent_irreversible"
    assert audit["notifications"]["admin_telegram"] == "possibly_sent_irreversible"
    assert audit_row[2]

    history = c.get("/admin/transfers?status=unlinked")
    assert history.status_code == 200
    history_html = history.get_data(as_text=True)
    assert "External reconciliation" in history_html
    assert "Calendar hold removed" in history_html
    assert "Notion updated" in history_html
    assert "cannot be recalled" in history_html
    assert "window.alert(result.operator_alert" in history_html


def test_unlink_contains_provider_failures_and_records_attention_required(
    client,
    monkeypatch,
):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id,
            calendar_event_id, calendar_event_url, notion_page_id
        ) VALUES (
            '2026-08-10', '13:40', 'Provider Failure Client',
            'provider-failure@example.com', '4035550208', '', 'Mission Mini',
            'confirmed', 1, 1, 120.50, 120.50, 241.00, 'mission-mini-10',
            'calendar-provider-failure',
            'https://calendar.example/calendar-provider-failure',
            'notion-provider-failure'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES ('unlink-provider-failure-message', ?, 120.50)
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-provider-failure-ref",
        message_id="unlink-provider-failure-message",
        amount=120.50,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )
    provider_calls = []

    def fail_calendar(target_booking_id, event_id=None, expected_state=None):
        conn = sqlite3.connect(db_path)
        state = conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
        conn.close()
        provider_calls.append(("calendar", target_booking_id, event_id))
        assert state == ("pending_payment", 0, 0, 0.0)
        raise RuntimeError("calendar unavailable")

    def fail_notion(target_booking_id, expected_state=None):
        conn = sqlite3.connect(db_path)
        state = conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
        conn.close()
        provider_calls.append(("notion", target_booking_id))
        assert state == ("pending_payment", 0, 0, 0.0)
        raise RuntimeError("notion unavailable")

    monkeypatch.setattr(app, "delete_calendar_event_for_booking", fail_calendar)
    monkeypatch.setattr(app, "update_notion_after_transfer_unlink", fail_notion)

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["external_reconciliation"]["status"] == "attention_required"
    assert payload["external_reconciliation"]["calendar"]["status"] == "failed"
    assert payload["external_reconciliation"]["notion"]["status"] == "failed"
    assert "calendar" in payload["operator_alert"].lower()
    assert "notion" in payload["operator_alert"].lower()
    assert provider_calls == [
        ("calendar", booking_id, "calendar-provider-failure"),
        ("notion", booking_id),
    ]

    conn = sqlite3.connect(db_path)
    booking_after = conn.execute(
        """
        SELECT status, confirmed, paid, paid_amount,
               calendar_event_id, calendar_event_url, notion_page_id
          FROM bookings
         WHERE id=?
        """,
        (booking_id,),
    ).fetchone()
    audit_row = conn.execute(
        """
        SELECT status, matched_booking_id, unlink_external_status,
               unlink_external_details, unlink_external_reconciled_at
          FROM etransfers
         WHERE id=?
        """,
        (transfer_id,),
    ).fetchone()
    conn.close()
    assert booking_after == (
        "pending_payment",
        0,
        0,
        0.0,
        "calendar-provider-failure",
        "https://calendar.example/calendar-provider-failure",
        "notion-provider-failure",
    )
    assert audit_row[0:3] == ("unlinked", None, "attention_required")
    audit = json.loads(audit_row[3])
    assert audit["calendar"]["status"] == "failed"
    assert audit["calendar"]["error"] == "RuntimeError"
    assert audit["notion"]["status"] == "failed"
    assert audit["notion"]["error"] == "RuntimeError"
    assert audit_row[4]


def test_unlink_does_not_remove_new_confirmation_calendar_state(
    client,
    monkeypatch,
):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id,
            calendar_event_id, calendar_event_url
        ) VALUES (
            '2026-08-10', '14:00', 'Reconfirmed Client',
            'reconfirmed@example.com', '4035550209', '', 'Mission Mini',
            'confirmed', 1, 1, 120.50, 120.50, 241.00, 'mission-mini-10',
            'calendar-original-confirmation',
            'https://calendar.example/calendar-original-confirmation'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES ('unlink-reconfirmed-message', ?, 120.50)
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-reconfirmed-ref",
        message_id="unlink-reconfirmed-message",
        amount=120.50,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )
    real_delete = app.delete_calendar_event_for_booking

    def reconfirm_then_reconcile(
        target_booking_id,
        event_id=None,
        expected_state=None,
    ):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            UPDATE bookings
               SET status='confirmed',
                   confirmed=1,
                   paid=1,
                   paid_amount=120.50,
                   calendar_event_id='calendar-new-confirmation',
                   calendar_event_url='https://calendar.example/calendar-new-confirmation'
             WHERE id=?
            """,
            (booking_id,),
        )
        conn.commit()
        conn.close()
        return real_delete(
            target_booking_id,
            event_id=event_id,
            expected_state=expected_state,
        )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "superseded reconciliation must not call the Calendar provider"
        ),
    )
    monkeypatch.setattr(
        app,
        "delete_calendar_event_for_booking",
        reconfirm_then_reconcile,
    )

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["external_reconciliation"]["status"] == "attention_required"
    assert payload["external_reconciliation"]["calendar"]["status"] == "superseded"
    assert payload["external_reconciliation"]["notion"]["status"] == "superseded"
    conn = sqlite3.connect(db_path)
    booking_after = conn.execute(
        """
        SELECT status, confirmed, paid, paid_amount,
               calendar_event_id, calendar_event_url
          FROM bookings
         WHERE id=?
        """,
        (booking_id,),
    ).fetchone()
    transfer_after = conn.execute(
        """
        SELECT status, matched_booking_id, unlink_external_status
          FROM etransfers
         WHERE id=?
        """,
        (transfer_id,),
    ).fetchone()
    conn.close()
    assert booking_after == (
        "confirmed",
        1,
        1,
        120.50,
        "calendar-new-confirmation",
        "https://calendar.example/calendar-new-confirmation",
    )
    assert transfer_after == ("unlinked", None, "attention_required")


def test_unlink_flags_calendar_url_without_event_id_for_manual_follow_up(client):
    c, app, _, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id,
            calendar_event_id, calendar_event_url
        ) VALUES (
            '2026-08-10', '14:20', 'Legacy Calendar Client',
            'legacy-calendar@example.com', '4035550210', '', 'Mission Mini',
            'confirmed', 1, 1, 120.50, 120.50, 241.00, 'mission-mini-10',
            NULL, 'https://calendar.example/legacy-without-id'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES ('unlink-legacy-calendar-message', ?, 120.50)
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-legacy-calendar-ref",
        message_id="unlink-legacy-calendar-message",
        amount=120.50,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["external_reconciliation"]["status"] == "attention_required"
    assert (
        payload["external_reconciliation"]["calendar"]["status"]
        == "inconsistent_local_state"
    )
    assert "calendar needs manual follow-up" in payload["operator_alert"].lower()
    conn = app.db_conn()
    booking_after = conn.execute(
        """
        SELECT status, confirmed, paid, paid_amount,
               calendar_event_id, calendar_event_url
          FROM bookings
         WHERE id=?
        """,
        (booking_id,),
    ).fetchone()
    audit_status = conn.execute(
        "SELECT unlink_external_status FROM etransfers WHERE id=?",
        (transfer_id,),
    ).fetchone()[0]
    conn.close()
    assert tuple(booking_after) == (
        "pending_payment",
        0,
        0,
        0.0,
        None,
        "https://calendar.example/legacy-without-id",
    )
    assert audit_status == "attention_required"


@pytest.mark.parametrize(
    ("mutation", "booking_state", "prior_state", "expected_after"),
    [
        (
            "confirm",
            ("confirmed", 1, 1, 80.00),
            ("pending_payment", 0, 0, 0.0),
            ("pending_payment", 0, 0, 0.0),
        ),
        (
            "partial",
            ("partial_payment", 0, 0, 80.00),
            ("pending_payment", 0, 0, 0.0),
            ("pending_payment", 0, 0, 0.0),
        ),
        (
            "reconcile",
            ("confirmed", 1, 1, 80.00),
            ("confirmed", 1, 1, 40.00),
            ("confirmed", 1, 1, 40.00),
        ),
    ],
)
def test_unlink_reverses_only_exact_auto_owned_booking_state(
    client,
    mutation,
    booking_state,
    prior_state,
    expected_after,
):
    c, app, db_path, _ = client
    _login(c)
    status, confirmed, paid, paid_amount = booking_state
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status, confirmed, paid,
            paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-11', '14:00', 'Exact Owner', 'exact-owner@example.com',
            '4035550202', '',
            'Mission Mini', ?, ?, ?, ?, 120.50, 241.00, 'mission-mini-11'
        )
        """,
        (status, confirmed, paid, paid_amount),
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES ('unlink-exact-owner-message', ?, 80.00)
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-exact-owner-ref-" + status,
        message_id="unlink-exact-owner-message",
        amount=80.00,
        matched_booking_id=booking_id,
        status="matched",
        archived_at="2026-07-17 09:00:00",
        restored_at="2026-07-17 10:00:00",
    )
    _set_transfer_payment_provenance(
        app,
        transfer_id,
        mutation=mutation,
        prior_status=prior_state[0],
        prior_confirmed=prior_state[1],
        prior_paid=prior_state[2],
        prior_paid_amount=prior_state[3],
    )

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    booking_after = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed_after = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        ("unlink-exact-owner-message",),
    ).fetchone()
    conn.close()
    assert booking_after == expected_after
    assert processed_after == (None, 80.00)
    transfer_after = _transfer_row(app, transfer_id)
    assert transfer_after["status"] == "unlinked"
    assert transfer_after["unlinked_booking_id"] == booking_id
    assert transfer_after["unlinked_at"]
    assert transfer_after["archived_at"] == "2026-07-17 09:00:00"
    assert transfer_after["restored_at"] == "2026-07-17 10:00:00"


def test_unlink_name_only_match_retires_ledger_without_payment_change(client):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-11', '14:30', 'Name Only Match',
            'name-only@example.com', '4035550205', '', 'Mission Mini',
            'pending_payment', 0, 0, 0, 120.50, 241.00, 'mission-mini-11'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-name-only-ref",
        message_id="unlink-name-only-message",
        sender_name="Name Only Match",
    )
    assert app._auto_link_etransfers() == 1

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    transfer = _transfer_row(app, transfer_id)
    assert transfer["status"] == "unlinked"
    assert transfer["matched_booking_id"] is None
    assert transfer["unlinked_booking_id"] == booking_id
    assert transfer["payment_mutation"] == "name_only"
    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id=?",
        ("unlink-name-only-message",),
    ).fetchone()
    conn.close()
    assert booking == ("pending_payment", 0, 0, 0.0)
    assert processed is None


def test_unlink_name_only_match_from_paid_booking_keeps_finances_unchanged(client):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-11', '14:40', 'Paid Name Only Match',
            'paid-name-only@example.com', '4035550207', '', 'Mission Mini',
            'confirmed', 1, 1, 120.50, 120.50, 241.00, 'mission-mini-11'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES ('paid-booking-owner-message', ?, 120.50)
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    owner_transfer_id = _insert_transfer(
        app,
        reference_number="paid-booking-owner-ref",
        message_id="paid-booking-owner-message",
        sender_name="Original Financial Owner",
        amount=120.50,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        owner_transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-paid-name-only-ref",
        message_id="unlink-paid-name-only-message",
        sender_name="Paid Name Only Match",
        amount=75.00,
        archived_at="2026-07-18 09:00:00",
        restored_at="2026-07-18 10:00:00",
    )
    assert app._auto_link_etransfers() == 1
    linked = _transfer_row(app, transfer_id)
    assert linked["matched_booking_id"] == booking_id
    assert linked["payment_mutation"] == "name_only"

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    transfer = _transfer_row(app, transfer_id)
    assert transfer["status"] == "unlinked"
    assert transfer["matched_booking_id"] is None
    assert transfer["unlinked_booking_id"] == booking_id
    assert transfer["unlinked_at"]
    assert transfer["archived_at"] == "2026-07-18 09:00:00"
    assert transfer["restored_at"] == "2026-07-18 10:00:00"
    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed = conn.execute(
        """
        SELECT message_id, booking_id, amount
          FROM processed_emails
         WHERE message_id IN (?, ?)
         ORDER BY message_id
        """,
        ("paid-booking-owner-message", "unlink-paid-name-only-message"),
    ).fetchall()
    conn.close()
    assert booking == ("confirmed", 1, 1, 120.50)
    assert processed == [("paid-booking-owner-message", booking_id, 120.50)]
    owner_transfer = _transfer_row(app, owner_transfer_id)
    assert owner_transfer["status"] == "matched"
    assert owner_transfer["matched_booking_id"] == booking_id


def test_unlink_rejects_manual_financial_link_without_reversal_provenance(client):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-11', '14:50', 'Manual Financial Link',
            'manual-financial@example.com', '4035550208', '', 'Mission Mini',
            'pending_payment', 0, 0, 0, 120.50, 241.00, 'mission-mini-11'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-manual-financial-ref",
        message_id="unlink-manual-financial-message",
        sender_name="Manual Financial Link",
        amount=120.50,
    )
    link = c.post(
        f"/admin/transfers/{transfer_id}/link",
        json={"booking_id": booking_id},
    )
    assert link.status_code == 200
    linked_before = _transfer_row(app, transfer_id)
    assert linked_before["payment_mutation"] == "manual"

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 409
    assert response.get_json() == {
        "success": False,
        "error": "Transfer payment ownership cannot be safely reversed",
    }
    assert _transfer_row(app, transfer_id) == linked_before
    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id=?",
        ("unlink-manual-financial-message",),
    ).fetchone()
    conn.close()
    assert booking == ("confirmed", 1, 1, 120.50)
    assert processed is None


def test_unlink_infers_legacy_name_only_provenance_for_paid_booking(client):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-11', '15:10', 'Legacy Name Match',
            'legacy-name@example.com', '4035550209', '', 'Mission Mini',
            'confirmed', 1, 1, 120.50, 120.50, 241.00, 'mission-mini-11'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES ('legacy-paid-owner-message', ?, 120.50)
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    owner_transfer_id = _insert_transfer(
        app,
        reference_number="legacy-paid-owner-ref",
        message_id="legacy-paid-owner-message",
        sender_name="Original Legacy Financial Owner",
        amount=120.50,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        owner_transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-legacy-name-only-ref",
        message_id="unlink-legacy-name-only-message",
        sender_name="Legacy Name Match",
        amount=75.00,
        matched_booking_id=booking_id,
        status="matched",
    )
    linked_before = _transfer_row(app, transfer_id)
    assert linked_before["payment_mutation"] is None

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    transfer = _transfer_row(app, transfer_id)
    assert transfer["status"] == "unlinked"
    assert transfer["matched_booking_id"] is None
    assert transfer["unlinked_booking_id"] == booking_id
    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id=?",
        ("unlink-legacy-name-only-message",),
    ).fetchone()
    owner_processed = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        ("legacy-paid-owner-message",),
    ).fetchone()
    conn.close()
    assert booking == ("confirmed", 1, 1, 120.50)
    assert processed is None
    assert owner_processed == (booking_id, 120.50)
    owner_transfer = _transfer_row(app, owner_transfer_id)
    assert owner_transfer["status"] == "matched"
    assert owner_transfer["matched_booking_id"] == booking_id


def test_unlink_rejects_legacy_manual_link_without_safe_provenance(client):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-11', '15:20', 'Legacy Manual Match',
            'legacy-manual@example.com', '4035550210', '', 'Mission Mini',
            'confirmed', 1, 1, 120.50, 120.50, 241.00, 'mission-mini-11'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-legacy-manual-ref",
        message_id="unlink-legacy-manual-message",
        sender_name="Legacy Manual Match",
        amount=120.50,
        matched_booking_id=booking_id,
        status="matched",
    )
    transfer_before = _transfer_row(app, transfer_id)
    assert transfer_before["payment_mutation"] is None

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 409
    assert response.get_json() == {
        "success": False,
        "error": "Transfer payment ownership cannot be safely reversed",
    }
    assert _transfer_row(app, transfer_id) == transfer_before
    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    conn.close()
    assert booking == ("confirmed", 1, 1, 120.50)


def test_unlink_restores_prior_amount_from_real_reconciliation_transaction(client):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-11', '15:00', 'Reconciled Owner',
            'reconciled-owner@example.com', '4035550206', '', 'Mission Mini',
            'confirmed', 1, 1, 40.00, 120.50, 241.00, 'mission-mini-11'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    message_id = "unlink-real-reconcile-message"
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-real-reconcile-ref",
        message_id=message_id,
        amount=80.00,
    )

    assert checker.reconcile_confirmed_payment(
        booking_id,
        80.00,
        message_id,
        ledger_id=transfer_id,
    ) is True
    claimed = _transfer_row(app, transfer_id)
    assert claimed["payment_mutation"] == "reconcile"
    assert claimed["prior_booking_status"] == "confirmed"
    assert claimed["prior_booking_confirmed"] == 1
    assert claimed["prior_booking_paid"] == 1
    assert claimed["prior_booking_paid_amount"] == 40.00

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 200
    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        (message_id,),
    ).fetchone()
    conn.close()
    assert booking == ("confirmed", 1, 1, 40.0)
    assert processed == (None, 80.0)
    assert _transfer_row(app, transfer_id)["status"] == "unlinked"


@pytest.mark.parametrize(
    "ownership_problem",
    [
        "missing_processed",
        "different_processed_owner",
        "changed_booking_amount",
        "missing_provenance",
    ],
)
def test_unlink_rejects_ambiguous_payment_ownership_without_mutation(
    client,
    ownership_problem,
):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status, confirmed, paid,
            paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-12', '15:00', 'Ambiguous Owner',
            'ambiguous-owner@example.com', '4035550203', '', 'Mission Mini',
            'confirmed', 1, 1,
            ?, 120.50, 241.00, 'mission-mini-12'
        )
        """,
        (99.00 if ownership_problem == "changed_booking_amount" else 80.00,),
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if ownership_problem != "missing_processed":
        conn.execute(
            """
            INSERT INTO processed_emails (message_id, booking_id, amount)
            VALUES ('unlink-ambiguous-owner-message', ?, 80.00)
            """,
            (999 if ownership_problem == "different_processed_owner" else booking_id,),
        )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-ambiguous-owner-ref-" + ownership_problem,
        message_id="unlink-ambiguous-owner-message",
        amount=80.00,
        matched_booking_id=booking_id,
        status="matched",
    )
    if ownership_problem != "missing_provenance":
        _set_transfer_payment_provenance(
            app,
            transfer_id,
            mutation="confirm",
            prior_status="pending_payment",
            prior_confirmed=0,
            prior_paid=0,
            prior_paid_amount=0.0,
        )
    transfer_before = _transfer_row(app, transfer_id)
    conn = sqlite3.connect(db_path)
    booking_before = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed_before = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        ("unlink-ambiguous-owner-message",),
    ).fetchone()
    conn.close()

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 409
    assert response.get_json()["success"] is False
    assert _transfer_row(app, transfer_id) == transfer_before
    conn = sqlite3.connect(db_path)
    booking_after = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed_after = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        ("unlink-ambiguous-owner-message",),
    ).fetchone()
    conn.close()
    assert booking_after == booking_before
    assert processed_after == processed_before


def test_unlink_rolls_back_all_ownership_when_terminal_ledger_update_fails(client):
    c, app, db_path, _ = client
    _login(c)
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status, confirmed, paid,
            paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-13', '16:00', 'Rollback Owner',
            'rollback-owner@example.com', '4035550204', '', 'Mission Mini',
            'confirmed', 1, 1,
            80.00, 120.50, 241.00, 'mission-mini-13'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES ('unlink-rollback-message', ?, 80.00)
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="unlink-rollback-ref",
        message_id="unlink-rollback-message",
        amount=80.00,
        matched_booking_id=booking_id,
        status="matched",
    )
    _set_transfer_payment_provenance(
        app,
        transfer_id,
        mutation="confirm",
        prior_status="pending_payment",
        prior_confirmed=0,
        prior_paid=0,
        prior_paid_amount=0.0,
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TRIGGER fail_terminal_unlink
        BEFORE UPDATE OF status ON etransfers
        WHEN NEW.status='unlinked'
        BEGIN
            SELECT RAISE(ABORT, 'injected unlink failure');
        END
        """
    )
    conn.commit()
    conn.close()

    response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})

    assert response.status_code == 500
    assert _transfer_row(app, transfer_id)["status"] == "matched"
    conn = sqlite3.connect(db_path)
    booking_after = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed_after = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id=?",
        ("unlink-rollback-message",),
    ).fetchone()
    conn.close()
    assert booking_after == ("confirmed", 1, 1, 80.0)
    assert processed_after == (booking_id, 80.0)


def test_unlink_rejects_archived_and_gift_linked_rows_without_mutation(client):
    c, app, _, _ = client
    _login(c)
    archived_id = _insert_transfer(
        app,
        reference_number="unlink-archived-ref",
        message_id="unlink-archived-message",
        status="ignored",
        archived_at="2026-07-18 19:00:00",
    )
    gift_id = _insert_transfer(
        app,
        reference_number="unlink-gift-ref",
        message_id="unlink-gift-message",
        matched_gift_code="GIFT-TEST-UNLINK",
        status="matched",
    )
    unmatched_id = _insert_transfer(
        app,
        reference_number="unlink-unmatched-ref",
        message_id="unlink-unmatched-message",
        status="unmatched",
    )
    outbound_id = _insert_transfer(
        app,
        reference_number="unlink-outbound-ref",
        message_id="unlink-outbound-message",
        direction="out",
        matched_booking_id=91,
        status="matched",
    )
    before = {
        archived_id: _transfer_row(app, archived_id),
        gift_id: _transfer_row(app, gift_id),
        unmatched_id: _transfer_row(app, unmatched_id),
        outbound_id: _transfer_row(app, outbound_id),
    }

    for transfer_id in (archived_id, gift_id, unmatched_id, outbound_id):
        response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})
        assert response.status_code == 409
        assert response.get_json()["success"] is False
        assert _transfer_row(app, transfer_id) == before[transfer_id]

    missing = c.post("/admin/transfers/999999/unlink", json={})
    assert missing.status_code == 404
    assert missing.get_json()["success"] is False


def test_unlink_requires_admin_authentication_without_mutation(client):
    c, app, _, _ = client
    matched_id = _insert_transfer(
        app,
        reference_number="unlink-auth-ref",
        message_id="unlink-auth-message",
        matched_booking_id=84,
        status="matched",
    )
    before = _transfer_row(app, matched_id)

    response = c.post(f"/admin/transfers/{matched_id}/unlink", json={})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Unauthorized"
    assert _transfer_row(app, matched_id) == before


def test_auto_link_does_not_reactivate_transfer_archived_after_candidate_read(
    client,
    monkeypatch,
):
    _, app, db_path, _ = client
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-04', '13:00', 'Archive Race Client',
            'archive-race@example.com', '4035550188', '', 'Mission Mini',
            'pending_payment', 0, 0, 0, 120.50, 241.00, 'mission-mini-4'
        )
        """
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="auto-link-archive-race-ref",
        message_id="auto-link-archive-race-message",
        sender_name="Archive Race Client",
    )
    real_db_conn = app.db_conn
    archived = False

    class ArchiveBeforeAutoLinkUpdate:
        def __init__(self, connection):
            self.connection = connection

        @property
        def row_factory(self):
            return self.connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.connection.row_factory = value

        def execute(self, sql, params=()):
            nonlocal archived
            if (
                not archived
                and "UPDATE etransfers" in sql
                and "SET matched_booking_id" in sql
            ):
                archive_conn = sqlite3.connect(db_path)
                archive_conn.execute(
                    """
                    UPDATE etransfers
                       SET status='ignored',
                           archived_at='2026-07-18 20:00:00'
                     WHERE id=?
                    """,
                    (transfer_id,),
                )
                archive_conn.commit()
                archive_conn.close()
                archived = True
            return self.connection.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        app,
        "db_conn",
        lambda: ArchiveBeforeAutoLinkUpdate(real_db_conn()),
    )

    linked = app._auto_link_etransfers()

    assert archived is True
    assert linked == 0
    row = _transfer_row(booking_app, transfer_id)
    assert row["status"] == "ignored"
    assert row["archived_at"] == "2026-07-18 20:00:00"
    assert row["matched_booking_id"] is None


def test_auto_link_keeps_existing_name_match_and_payment_semantics(client):
    _, app, _, _ = client
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id
        ) VALUES (
            '2026-08-04', '13:30', 'Single Name Match',
            'single-match@example.com', '4035550189', '', 'Mission Mini',
            'pending_payment', 0, 0, 0, 120.50, 241.00, 'mission-mini-4'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    booking_before = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
    )
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="auto-link-single-name-ref",
        message_id="auto-link-single-name-message",
        sender_name="Single Name Match",
        amount=999.99,
    )

    linked = app._auto_link_etransfers()

    assert linked == 1
    row = _transfer_row(app, transfer_id)
    assert row["status"] == "matched"
    assert row["matched_booking_id"] == booking_id
    conn = app.db_conn()
    booking_after = tuple(
        conn.execute(
            "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
    )
    conn.close()
    assert booking_after == booking_before


def test_archive_during_body_read_prevents_booking_confirmation(client, monkeypatch):
    _, app, db_path, _ = client
    conn = app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status,
            confirmed, paid, paid_amount, deposit_amount, full_price, event_id,
            created_at, reserved_until
        ) VALUES (
            '2026-08-05', '14:00', 'Body Race Client',
            'body-race@example.com', '4035550199', '', 'Mission Mini',
            'pending_payment', 0, 0, 0, 120.50, 241.00, 'mission-mini-5',
            '2026-07-18 10:00:00', '2026-08-05 14:15:00'
        )
        """
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    transfer_id = _insert_transfer(
        app,
        reference_number="body-read-archive-race-ref",
        message_id="body-read-archive-race-message",
        sender_name="Body Race Client",
        amount=120.50,
    )

    def archive_during_body_read(message_id):
        assert message_id == "body-read-archive-race-message"
        archive_conn = sqlite3.connect(db_path)
        archive_conn.execute(
            """
            UPDATE etransfers
               SET status='ignored',
                   archived_at='2026-07-18 20:30:00'
             WHERE id=?
            """,
            (transfer_id,),
        )
        archive_conn.commit()
        archive_conn.close()
        return (
            "Interac e-Transfer: You've received $120.50 from Body Race Client.\n"
            "Sent From: Body Race Client"
        )

    monkeypatch.setattr(checker, "read_message_body", archive_during_body_read)
    monkeypatch.setattr(checker, "try_confirm_gift_etransfer", lambda *a, **k: False)
    monkeypatch.setattr(
        checker,
        "_notify_admin_orphan",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("archived transfer must not reach orphan handling")
        ),
    )
    pending = checker.get_pending_bookings(within_minutes=30)

    confirmed_id, ambiguous = checker.check_single_email(
        {
            "id": "body-read-archive-race-message",
            "date": "2026-07-18 11:00:00+00:00",
        },
        pending,
    )

    assert confirmed_id is None
    assert ambiguous is None
    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, confirmed, paid, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()
    processed = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id=?",
        ("body-read-archive-race-message",),
    ).fetchone()
    conn.close()
    assert booking == ("pending_payment", 0, 0, 0.0)
    assert processed is None
    row = _transfer_row(app, transfer_id)
    assert row["status"] == "ignored"
    assert row["archived_at"] == "2026-07-18 20:30:00"
    assert row["matched_booking_id"] is None


def test_mark_message_processed_does_not_overwrite_archived_ledger_row(client):
    _, app, db_path, _ = client
    transfer_id = _insert_transfer(
        app,
        reference_number="processed-archive-guard-ref",
        message_id="processed-archive-guard-message",
        status="ignored",
        archived_at="2026-07-18 21:00:00",
    )
    before = _transfer_row(app, transfer_id)

    marked = checker.mark_message_processed(
        "processed-archive-guard-message",
        92,
        120.50,
    )

    assert marked is False
    assert _transfer_row(app, transfer_id) == before
    conn = sqlite3.connect(db_path)
    processed = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id=?",
        ("processed-archive-guard-message",),
    ).fetchone()
    conn.close()
    assert processed is None
