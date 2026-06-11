"""Regression tests for abandoned booking recovery emails."""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

import app as booking_app


@pytest.fixture()
def temp_db(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    booking_app.init_db()
    booking_app._rate_limits.clear()
    return db_path


def _insert_expired_booking(created_at, abandoned_email_sent=None, second_sent=None):
    conn = booking_app.db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bookings
          (date, time, name, email, phone, session_type, status, confirmed, paid,
           created_at, reserved_until, event_id, abandoned_email_sent, abandoned_second_email_sent)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-11",
            "10:00",
            "Abandoned Client",
            "abandoned@example.com",
            "",
            "Mini Session",
            "expired",
            0,
            0,
            created_at,
            created_at,
            "mountain-mini-session-2026-07-11",
            abandoned_email_sent,
            second_sent,
        ),
    )
    bid = cur.lastrowid
    conn.commit()
    conn.close()
    return bid


def _booking_row(booking_id):
    conn = booking_app.db_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    return dict(row)


class TestSecondAbandonedFollowup:
    def test_first_followup_still_sends_once_after_two_hours(self, temp_db, monkeypatch):
        now = booking_app._local_now()
        booking_id = _insert_expired_booking((now - timedelta(hours=3)).isoformat())
        sent = []

        monkeypatch.setattr(booking_app, "_local_now", lambda: now)
        monkeypatch.setattr(booking_app, "_send_abandoned_email", lambda booking: sent.append(booking["id"]) or True)
        monkeypatch.setattr(booking_app, "_record_booking_funnel_event", lambda *args, **kwargs: None)
        monkeypatch.setattr(booking_app, "_emit_n8n_event", lambda *args, **kwargs: None)

        booking_app._process_abandoned_emails()

        row = _booking_row(booking_id)
        assert sent == [booking_id]
        assert row["abandoned_email_sent"] is not None
        assert row["abandoned_second_email_sent"] is None

    def test_second_followup_sends_after_48_hours_from_first_followup(self, temp_db, monkeypatch):
        now = booking_app._local_now()
        first_sent_at = (now - timedelta(hours=49)).isoformat()
        booking_id = _insert_expired_booking(
            (now - timedelta(days=4)).isoformat(),
            abandoned_email_sent=first_sent_at,
        )
        sent = []

        monkeypatch.setattr(booking_app, "_local_now", lambda: now)
        monkeypatch.setattr(booking_app, "_send_abandoned_second_email", lambda booking: sent.append(booking["id"]) or True)
        monkeypatch.setattr(booking_app, "_record_booking_funnel_event", lambda *args, **kwargs: None)
        monkeypatch.setattr(booking_app, "_emit_n8n_event", lambda *args, **kwargs: None)

        booking_app._process_abandoned_emails()

        row = _booking_row(booking_id)
        assert sent == [booking_id]
        assert row["abandoned_second_email_sent"] is not None

    def test_second_followup_waits_until_48_hours_after_first(self, temp_db, monkeypatch):
        now = booking_app._local_now()
        first_sent_at = (now - timedelta(hours=47, minutes=59)).isoformat()
        booking_id = _insert_expired_booking(
            (now - timedelta(days=4)).isoformat(),
            abandoned_email_sent=first_sent_at,
        )
        sent = []

        monkeypatch.setattr(booking_app, "_local_now", lambda: now)
        monkeypatch.setattr(booking_app, "_send_abandoned_second_email", lambda booking: sent.append(booking["id"]) or True)

        booking_app._process_abandoned_emails()

        row = _booking_row(booking_id)
        assert sent == []
        assert row["abandoned_second_email_sent"] is None

    def test_second_followup_never_duplicates(self, temp_db, monkeypatch):
        now = booking_app._local_now()
        booking_id = _insert_expired_booking(
            (now - timedelta(days=5)).isoformat(),
            abandoned_email_sent=(now - timedelta(days=3)).isoformat(),
            second_sent=(now - timedelta(days=1)).isoformat(),
        )
        sent = []

        monkeypatch.setattr(booking_app, "_local_now", lambda: now)
        monkeypatch.setattr(booking_app, "_send_abandoned_second_email", lambda booking: sent.append(booking["id"]) or True)

        booking_app._process_abandoned_emails()

        assert sent == []
        assert _booking_row(booking_id)["abandoned_second_email_sent"] is not None
