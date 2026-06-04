import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import check_etransfer_v2 as checker


def _init_db(path):
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            time TEXT,
            name TEXT,
            email TEXT,
            phone TEXT,
            instagram TEXT,
            session_type TEXT,
            status TEXT,
            paid INTEGER DEFAULT 0,
            confirmed INTEGER DEFAULT 0,
            created_at TEXT,
            reserved_until TEXT,
            paid_amount REAL,
            deposit_amount REAL,
            full_price REAL,
            event_id TEXT
        )
    """)
    c.execute("""
        CREATE TABLE processed_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            booking_id INTEGER,
            amount REAL,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def test_extract_payment_info_handles_interac_amounts():
    assert checker.extract_payment_info("Interac e-Transfer: You've received $3.00 from andhon") == 3.00
    assert checker.extract_payment_info("Funds Deposited! $95.00 CAD") == 95.00
    assert checker.extract_payment_info("Amount: $1,250.50") == 1250.50


def test_is_etransfer_email_accepts_incoming_and_rejects_outgoing_transfer():
    incoming = {
        "subject": "Interac e-Transfer: You've received $120.75 from ANNA LAFLECHE and it has been automatically deposited.",
        "from": {"addr": "notify@payments.interac.ca"},
    }
    outgoing = {
        "subject": "Interac e-Transfer: Your $300.00 transfer to ANDRZEJ HONHALO has been successfully deposited.",
        "from": {"addr": "notify@payments.interac.ca"},
    }

    assert checker.is_etransfer_email(incoming) is True
    assert checker.is_etransfer_email(outgoing) is False


def test_get_emails_uses_server_side_interac_filter(monkeypatch):
    calls = []
    checker._EMAIL_FETCH_CACHE.update({
        "ts": 0.0,
        "page_size": None,
        "lookback_days": None,
        "emails": None,
    })

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='[{"id":"recent-interac","subject":"Interac e-Transfer"}]',
            stderr="",
        )

    monkeypatch.setattr(checker.subprocess, "run", fake_run)

    emails = checker.get_emails(page_size=25, lookback_days=7)

    assert emails == [{"id": "recent-interac", "subject": "Interac e-Transfer"}]
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert "--quiet" in cmd
    assert "--page-size" in cmd and "25" in cmd
    assert "from" in cmd and "interac.ca" in cmd
    assert "after" in cmd
    assert cmd[-4:] == ["order", "by", "date", "desc"]
    assert kwargs["timeout"] >= 15


def test_get_pending_bookings_includes_reserved_without_clicked_confirm(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime.now()
    conn.execute("""
        INSERT INTO bookings(date,time,name,email,status,paid,confirmed,created_at,reserved_until,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        "2026-05-17", "10:30", "Andrzej", "andreygongalo@example.com",
        "reserved", 0, 0,
        (now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),
        (now + timedelta(minutes=13)).strftime('%Y-%m-%d %H:%M:%S'),
        "test-event",
    ))
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    pending = checker.get_pending_bookings(within_minutes=30)

    assert len(pending) == 1
    assert pending[0]["status"] == "reserved"


def test_get_pending_bookings_keeps_payment_submitted_candidate_for_24_hours(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime.now()
    conn.execute("""
        INSERT INTO bookings(date,time,name,email,status,paid,confirmed,created_at,reserved_until,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        "2026-06-20", "16:30", "Slow Interac", "slow@example.com",
        "pending_payment", 0, 0,
        (now - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'),
        (now - timedelta(minutes=90)).strftime('%Y-%m-%d %H:%M:%S'),
        "test-event",
    ))
    conn.commit()
    conn.close()

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))

    pending = checker.get_pending_bookings(within_minutes=30)

    assert len(pending) == 1
    assert pending[0]["status"] == "pending_payment"


def test_check_single_email_confirms_reserved_booking_by_amount(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime.now()
    cur = conn.execute("""
        INSERT INTO bookings(date,time,name,email,status,paid,confirmed,created_at,reserved_until,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        "2026-05-17", "10:30", "Andrzej", "andreygongalo@example.com",
        "reserved", 0, 0,
        (now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),
        (now + timedelta(minutes=13)).strftime('%Y-%m-%d %H:%M:%S'),
        "test-event",
    ))
    booking_id = cur.lastrowid
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "get_expected_amount_for_booking", lambda booking_id: 3.00)
    monkeypatch.setattr(checker, "read_message_body", lambda message_id: "You've received $3.00 from andhon")

    pending = checker.get_pending_bookings(within_minutes=30)
    confirmed_id, ambiguous = checker.check_single_email({"id": "msg-1", "date": now.strftime('%Y-%m-%d %H:%M+00:00')}, pending)

    assert confirmed_id == booking_id
    assert ambiguous is None

    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT status, paid, confirmed, paid_amount FROM bookings WHERE id=?", (booking_id,)).fetchone())
    processed = conn.execute("SELECT message_id FROM processed_emails WHERE message_id='msg-1'").fetchone()
    conn.close()

    assert row == {"status": "confirmed", "paid": 1, "confirmed": 1, "paid_amount": 3.0}
    assert processed is not None


def test_check_single_email_rejects_stale_email_for_future_booking(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    created = datetime(2026, 5, 10, 16, 0, 0)
    cur = conn.execute("""
        INSERT INTO bookings(date,time,name,email,status,paid,confirmed,created_at,reserved_until,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        "2026-05-17", "10:30", "Future Client", "future@example.com",
        "reserved", 0, 0,
        created.strftime('%Y-%m-%d %H:%M:%S'),
        (created + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S'),
        "test-event",
    ))
    booking_id = cur.lastrowid
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "get_expected_amount_for_booking", lambda booking_id: 3.00)
    monkeypatch.setattr(checker, "read_message_body", lambda message_id: "You've received $3.00 from old payment")

    pending = checker.get_pending_bookings(within_minutes=30)
    confirmed_id, ambiguous = checker.check_single_email({"id": "old-msg", "date": "2026-05-10 15:19+00:00"}, pending)

    assert confirmed_id is None
    assert ambiguous is None

    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT status, paid, confirmed, paid_amount FROM bookings WHERE id=?", (booking_id,)).fetchone())
    processed = conn.execute("SELECT message_id FROM processed_emails WHERE message_id='old-msg'").fetchone()
    conn.close()

    assert row == {"status": "reserved", "paid": 0, "confirmed": 0, "paid_amount": None}
    assert processed is None


def test_stale_email_does_not_create_orphan_alert_just_because_pending_booking_exists(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime.now()
    conn.execute("""
        INSERT INTO bookings(date,time,name,email,status,paid,confirmed,created_at,reserved_until,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        "2026-06-20", "16:30", "Current Client", "current@example.com",
        "pending_payment", 0, 0,
        now.strftime('%Y-%m-%d %H:%M:%S'),
        (now + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S'),
        "test-event",
    ))
    conn.commit()
    conn.close()

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "get_expected_amount_for_booking", lambda booking_id: 120.75)
    monkeypatch.setattr(checker, "read_message_body", lambda message_id: "You've received $120.75 from Old Client")
    monkeypatch.setattr(
        checker,
        "_notify_admin_orphan",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale email must not alert")),
    )

    pending = checker.get_pending_bookings(within_minutes=30)
    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "stale-current-pending", "date": (now - timedelta(days=2)).strftime('%Y-%m-%d %H:%M+00:00')},
        pending,
    )

    assert confirmed_id is None
    assert ambiguous is None
    conn = sqlite3.connect(str(db_path))
    processed = conn.execute(
        "SELECT message_id FROM processed_emails WHERE message_id='stale-current-pending'"
    ).fetchone()
    conn.close()
    assert processed is None


def test_check_single_email_does_not_finalize_ambiguous_same_amount_message(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime.now()
    for name, minutes_ago in (("Older hold", 12), ("Paying client", 1)):
        conn.execute("""
            INSERT INTO bookings(date,time,name,email,status,paid,confirmed,created_at,reserved_until,event_id)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            "2026-06-01", "10:00", name, f"{name.replace(' ', '').lower()}@example.com",
            "reserved", 0, 0,
            (now - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S'),
            (now + timedelta(minutes=15 - minutes_ago)).strftime('%Y-%m-%d %H:%M:%S'),
            "test-event",
        ))
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "get_expected_amount_for_booking", lambda booking_id: 1.00)
    monkeypatch.setattr(checker, "read_message_body", lambda message_id: "You've received $1.00 from andhon")
    monkeypatch.setattr(checker, "_notify_admin_ambiguity", lambda amount, candidates: None)

    pending = checker.get_pending_bookings(within_minutes=30)
    confirmed_id, ambiguous = checker.check_single_email({"id": "ambiguous-msg", "date": now.strftime('%Y-%m-%d %H:%M+00:00')}, pending)

    assert confirmed_id is None
    assert ambiguous is not None
    assert len(ambiguous) == 2

    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    processed = conn.execute("SELECT message_id FROM processed_emails WHERE message_id='ambiguous-msg'").fetchone()
    confirmed_count = conn.execute("SELECT COUNT(*) FROM bookings WHERE confirmed=1 OR paid=1").fetchone()[0]
    conn.close()

    assert processed is None
    assert confirmed_count == 0


def test_check_single_email_disambiguates_same_amount_with_name_date_and_time(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime.now()
    ids = {}
    for name, date, slot_time in (
        ("Other Client", "2026-06-20", "14:00"),
        ("Anna Lafleche", "2026-06-20", "16:30"),
    ):
        cur = conn.execute("""
            INSERT INTO bookings(date,time,name,email,status,paid,confirmed,created_at,reserved_until,event_id)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            date, slot_time, name, f"{name.replace(' ', '').lower()}@example.com",
            "pending_payment", 0, 0,
            (now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),
            (now + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S'),
            "mountains-mini-session-2026-06-20",
        ))
        ids[name] = cur.lastrowid
    conn.commit()
    conn.close()

    body = """
    Interac e-Transfer: You've received $120.75 from Anna Lafleche.
    Message: Mountains mini session, June 20th, 4:30 pm
    Sent From: Anna Lafleche
    """
    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "get_expected_amount_for_booking", lambda booking_id: 120.75)
    monkeypatch.setattr(checker, "read_message_body", lambda message_id: body)
    monkeypatch.setattr(checker, "_notify_admin_ambiguity", lambda amount, candidates: None)

    pending = checker.get_pending_bookings(within_minutes=30)
    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "strong-match-msg", "date": now.strftime('%Y-%m-%d %H:%M+00:00')},
        pending,
    )

    assert confirmed_id == ids["Anna Lafleche"]
    assert ambiguous is None

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = {
        row["name"]: dict(row)
        for row in conn.execute("SELECT name, status, paid, confirmed FROM bookings")
    }
    conn.close()
    assert rows["Anna Lafleche"] == {
        "name": "Anna Lafleche", "status": "confirmed", "paid": 1, "confirmed": 1
    }
    assert rows["Other Client"] == {
        "name": "Other Client", "status": "pending_payment", "paid": 0, "confirmed": 0
    }


def test_underpayment_records_partial_but_does_not_report_auto_confirm(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime.now()
    cur = conn.execute("""
        INSERT INTO bookings(date,time,name,email,status,paid,confirmed,created_at,reserved_until,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        "2026-06-20", "16:30", "Partial Client", "partial@example.com",
        "pending_payment", 0, 0,
        (now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),
        (now + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S'),
        "mountains-mini-session-2026-06-20",
    ))
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "get_expected_amount_for_booking", lambda _booking_id: 120.75)
    monkeypatch.setattr(checker, "read_message_body", lambda _message_id: "You've received $80.00")
    monkeypatch.setattr(checker, "_notify_admin_underpaid", lambda *args, **kwargs: None)

    pending = checker.get_pending_bookings(within_minutes=30)
    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "partial-msg", "date": now.strftime('%Y-%m-%d %H:%M+00:00')},
        pending,
    )

    assert confirmed_id is None
    assert ambiguous is None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute(
        "SELECT status, paid, confirmed, paid_amount FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone())
    conn.close()
    assert row == {"status": "partial_payment", "paid": 0, "confirmed": 0, "paid_amount": 80.0}


def test_processed_linked_email_skips_slow_body_read(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    conn.execute(
        "INSERT INTO processed_emails(message_id, booking_id, amount) VALUES (?, ?, ?)",
        ("already-linked", 42, 120.75),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))

    def fail_read(_message_id):
        raise AssertionError("already-linked messages must not re-read Gmail bodies")

    monkeypatch.setattr(checker, "read_message_body", fail_read)

    assert checker.check_single_email({"id": "already-linked"}, [], []) == (None, None)


def test_check_single_email_reconciles_late_interac_amount_for_confirmed_booking(tmp_path, monkeypatch):
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime(2026, 5, 30, 12, 0, 0)
    cur = conn.execute("""
        INSERT INTO bookings(date,time,name,email,phone,instagram,session_type,
                             status,paid,confirmed,created_at,reserved_until,
                             paid_amount,deposit_amount,full_price,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        "2026-07-04", "13:30", "Yulia Levitskaya", "yulia@example.com",
        "5874324626", "", "mini", "confirmed", 1, 1,
        (now - timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S'),
        None, 110.25, 110.25, 220.50, "canoe-mini-session-2026-07-04",
    ))
    booking_id = cur.lastrowid
    conn.commit(); conn.close()

    body = """
    Interac e-Transfer: You've received $120.50 from Yulia Levitskaya.
    Message:
    Canoe mini session, July 4th, 1:30 pm
    Amount:
    $120.50 (CAD)
    Sent From:
    Yulia Levitskaya
    """
    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "read_message_body", lambda message_id: body)
    monkeypatch.setattr(checker, "_notify_admin_reconciled", lambda *a, **k: None)

    reconciliation = checker.get_reconciliation_bookings(within_days=45)
    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "late-msg", "date": now.strftime('%Y-%m-%d %H:%M:%S')},
        [],
        reconciliation,
    )

    assert confirmed_id is None
    assert ambiguous is None

    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, paid, confirmed, paid_amount FROM bookings WHERE id=?", (booking_id,)).fetchone()
    processed = conn.execute("SELECT message_id, amount FROM processed_emails WHERE message_id='late-msg'").fetchone()
    conn.close()

    assert dict(row) == {"status": "confirmed", "paid": 1, "confirmed": 1, "paid_amount": 120.50}
    assert processed["amount"] == 120.50


def test_check_single_email_reconciles_previously_processed_orphan_interac(tmp_path, monkeypatch):
    """A real Interac email may be seen before a safe match exists.

    If it was recorded as an orphan, later hourly watcher passes must still be
    able to use the same email to raise paid_amount on a strongly identified
    confirmed booking.
    """
    db_path = tmp_path / "bookings.db"
    conn = _init_db(str(db_path))
    now = datetime(2026, 5, 30, 12, 0, 0)
    cur = conn.execute("""
        INSERT INTO bookings(date,time,name,email,phone,instagram,session_type,
                             status,paid,confirmed,created_at,reserved_until,
                             paid_amount,deposit_amount,full_price,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        "2026-07-04", "13:30", "Yulia Levitskaya", "yulia@example.com",
        "5874324626", "", "mini", "confirmed", 1, 1,
        (now - timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S'),
        None, 110.25, 110.25, 220.50, "canoe-mini-session-2026-07-04",
    ))
    booking_id = cur.lastrowid
    conn.execute(
        "INSERT INTO processed_emails(message_id, booking_id, amount) VALUES (?, ?, ?)",
        ("orphan-before-manual-confirm", None, 120.50),
    )
    conn.commit(); conn.close()

    body = """
    Interac e-Transfer: You've received $120.50 from Yulia Levitskaya.
    Message:
    Canoe mini session, July 4th, 1:30 pm
    Amount:
    $120.50 (CAD)
    Sent From:
    Yulia Levitskaya
    """
    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "read_message_body", lambda message_id: body)
    monkeypatch.setattr(checker, "_notify_admin_reconciled", lambda *a, **k: None)

    reconciliation = checker.get_reconciliation_bookings(within_days=45)
    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "orphan-before-manual-confirm", "date": now.strftime('%a, %d %b %Y %H:%M:%S +0000')},
        [],
        reconciliation,
    )

    assert confirmed_id is None
    assert ambiguous is None

    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, paid, confirmed, paid_amount FROM bookings WHERE id=?", (booking_id,)).fetchone()
    processed = conn.execute(
        "SELECT booking_id, amount FROM processed_emails WHERE message_id='orphan-before-manual-confirm'"
    ).fetchone()
    conn.close()

    assert dict(row) == {"status": "confirmed", "paid": 1, "confirmed": 1, "paid_amount": 120.50}
    assert processed["booking_id"] == booking_id
    assert processed["amount"] == 120.50


def test_parse_email_datetime_accepts_rfc2822_headers():
    parsed = checker._parse_email_datetime("Fri, 29 May 2026 23:52:00 -0600")
    assert parsed == datetime(2026, 5, 30, 5, 52, 0)
