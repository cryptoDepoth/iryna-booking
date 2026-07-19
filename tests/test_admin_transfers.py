import io
import sqlite3

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

    assert {"archived_at", "restored_at"} <= columns
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


def test_admin_transfer_link_updates_booking_paid_amount(client):
    c, app, _, _ = client
    _login(c)
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


def test_unlink_only_accepts_a_matched_booking_transfer(client):
    c, app, _, _ = client
    _login(c)
    matched_id = _insert_transfer(
        app,
        reference_number="unlink-matched-booking-ref",
        message_id="unlink-matched-booking-message",
        matched_booking_id=73,
        status="matched",
    )

    response = c.post(f"/admin/transfers/{matched_id}/unlink", json={})

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    row = _transfer_row(app, matched_id)
    assert row["matched_booking_id"] is None
    assert row["matched_gift_code"] is None
    assert row["status"] == "unmatched"


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
    before = {
        archived_id: _transfer_row(app, archived_id),
        gift_id: _transfer_row(app, gift_id),
    }

    for transfer_id in (archived_id, gift_id):
        response = c.post(f"/admin/transfers/{transfer_id}/unlink", json={})
        assert response.status_code == 409
        assert response.get_json()["success"] is False
        assert _transfer_row(app, transfer_id) == before[transfer_id]


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
