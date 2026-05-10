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
    confirmed_id, ambiguous = checker.check_single_email({"id": "msg-1"}, pending)

    assert confirmed_id == booking_id
    assert ambiguous is None

    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT status, paid, confirmed, paid_amount FROM bookings WHERE id=?", (booking_id,)).fetchone())
    processed = conn.execute("SELECT message_id FROM processed_emails WHERE message_id='msg-1'").fetchone()
    conn.close()

    assert row == {"status": "confirmed", "paid": 1, "confirmed": 1, "paid_amount": 3.0}
    assert processed is not None
