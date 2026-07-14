"""Regression coverage for Google Calendar booking sync."""

import json
import os
import subprocess
import tempfile

import app as booking_app


def test_calendar_sync_uses_event_details_and_persists_link(monkeypatch):
    """2026-07-14: event lookup must happen before building the description."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setenv("GCAL_HELPER", "/tmp/fake-gcal-helper")
    booking_app.init_db()

    event = next(
        item for item in booking_app.EVENTS
        if item.get("status") in ("active", "upcoming") and not item.get("hidden")
    )
    conn = booking_app.db_conn()
    cursor = conn.execute(
        """
        INSERT INTO bookings
          (date, time, name, email, phone, instagram, session_type, status,
           paid, confirmed, event_id)
        VALUES (?, ?, ?, ?, ?, ?, 'mini', 'confirmed', 1, 1, ?)
        """,
        (
            event["date"],
            event["start_time"],
            "Calendar Test",
            "calendar-test@example.com",
            "4035550000",
            "@calendar-test",
            event["id"],
        ),
    )
    booking_id = cursor.lastrowid
    conn.commit()
    conn.close()

    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps({"id": "gcal-123", "htmlLink": "https://calendar.example/gcal-123"}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        link = booking_app.create_calendar_event_for_booking(booking_id)
        assert link == "https://calendar.example/gcal-123"
        assert event.get("location", "") in captured["args"]
        summary = captured["args"][captured["args"].index("--summary") + 1]
        assert event.get("title", booking_app.EVENT_TITLE) in summary

        conn = booking_app.db_conn()
        row = conn.execute(
            "SELECT calendar_event_id, calendar_event_url FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
        conn.close()
        assert row["calendar_event_id"] == "gcal-123"
        assert row["calendar_event_url"] == "https://calendar.example/gcal-123"
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
