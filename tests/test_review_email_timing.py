"""Regression coverage for premature review emails reported 2026-07-27—08-03.

Clients received "How were your photos?" before any gallery was delivered.
Automatic review outreach must now require both the promised two-week session
window and a successfully delivered gallery that the client had time to view.
"""

from datetime import datetime, timedelta
import os
import sqlite3
import tempfile

import pytest

import app as booking_app


@pytest.fixture()
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setattr(booking_app, "DB_PATH", path)
    monkeypatch.setenv("REVIEW_REQUEST_MIN_SESSION_DAYS", "14")
    monkeypatch.setenv("REVIEW_REQUEST_GALLERY_DELAY_DAYS", "3")
    booking_app.init_db()
    yield path
    if os.path.exists(path):
        os.unlink(path)


def _booking(now, **overrides):
    values = {
        "id": 1,
        "date": (now - timedelta(days=15)).strftime("%Y-%m-%d"),
        "time": "10:00",
        "name": "Timing Test",
        "email": "timing@example.com",
        "status": "confirmed",
        "confirmed": 1,
        "review_email_sent": None,
        "wfolio_url": "https://wfolio.com/gallery/timing-test",
        "gallery_email_sent_at": (now - timedelta(days=4)).isoformat(),
    }
    values.update(overrides)
    return values


def test_review_due_requires_two_weeks_and_delivered_gallery(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=booking_app._tz)
    assert booking_app._is_due_for_review_email(_booking(now), now) is True

    assert booking_app._is_due_for_review_email(
        _booking(now, date=(now - timedelta(days=13)).strftime("%Y-%m-%d")), now
    ) is False
    assert booking_app._is_due_for_review_email(
        _booking(now, gallery_email_sent_at=(now - timedelta(days=2)).isoformat()), now
    ) is False
    assert booking_app._is_due_for_review_email(
        _booking(now, wfolio_url="", gallery_email_sent_at=None), now
    ) is False


def test_process_review_emails_only_sends_due_gallery(temp_db, monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=booking_app._tz)
    monkeypatch.setattr(booking_app, "_local_now", lambda: now)
    sent_ids = []
    monkeypatch.setattr(
        booking_app,
        "_send_review_email",
        lambda booking: sent_ids.append(booking["id"]) or True,
    )

    conn = sqlite3.connect(temp_db)
    base = {
        "date": (now - timedelta(days=15)).strftime("%Y-%m-%d"),
        "time": "10:00",
        "status": "confirmed",
        "confirmed": 1,
        "wfolio_url": "https://wfolio.com/gallery/delivered",
        "gallery_email_sent_at": (now - timedelta(days=4)).isoformat(),
    }
    rows = [
        ("Due", "due@example.com", base),
        ("No gallery", "nogallery@example.com", {**base, "wfolio_url": "", "gallery_email_sent_at": None}),
        ("Too soon session", "session@example.com", {**base, "date": (now - timedelta(days=13)).strftime("%Y-%m-%d")}),
        ("Too soon gallery", "gallery@example.com", {**base, "gallery_email_sent_at": (now - timedelta(days=2)).isoformat()}),
    ]
    for index, (name, email, values) in enumerate(rows):
        conn.execute(
            """INSERT INTO bookings
               (date,time,name,email,phone,status,confirmed,wfolio_url,gallery_email_sent_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                values["date"], f"{10 + index:02d}:00", name, email, "403-555-0100",
                values["status"], values["confirmed"], values["wfolio_url"],
                values["gallery_email_sent_at"],
            ),
        )
    conn.commit()
    due_id = conn.execute("SELECT id FROM bookings WHERE email='due@example.com'").fetchone()[0]
    conn.close()

    booking_app._process_review_emails()

    assert sent_ids == [due_id]
    conn = sqlite3.connect(temp_db)
    sent_rows = conn.execute(
        "SELECT id, review_email_sent FROM bookings WHERE review_email_sent IS NOT NULL"
    ).fetchall()
    conn.close()
    assert len(sent_rows) == 1
    assert sent_rows[0][0] == due_id


def test_review_email_copy_refers_to_delivered_gallery(monkeypatch):
    captured = {}

    def fake_send(*args, **kwargs):
        captured["subject"] = args[2]
        captured["plain"] = args[3]
        captured["html"] = args[4]
        return True

    monkeypatch.setattr(booking_app, "_send_email_raw", fake_send)
    assert booking_app._send_review_email({
        "id": 9,
        "name": "Gallery Client",
        "email": "gallery-client@example.com",
        "confirmation_token": "safe-token",
    }) is True
    assert captured["subject"] == "How are you enjoying your gallery? 🌸"
    assert "Now that your gallery has arrived" in captured["plain"]
    assert "Now that your gallery has arrived" in captured["html"]
