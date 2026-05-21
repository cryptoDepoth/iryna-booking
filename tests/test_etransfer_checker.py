import os
import sqlite3
from datetime import datetime, timedelta

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
