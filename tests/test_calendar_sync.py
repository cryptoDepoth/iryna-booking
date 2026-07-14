"""Regression coverage for Google Calendar booking sync."""

import json
import os
import subprocess
import tempfile
from datetime import date, timedelta

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


def test_calendar_health_probe_is_cached(monkeypatch):
    monkeypatch.setenv("GCAL_HELPER", "/tmp/fake-gcal-helper")
    monkeypatch.setenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh-marker")
    booking_app._calendar_health_cache.update({
        "checked_at": 0.0,
        "config_marker": None,
        "ok": False,
        "warning": None,
    })
    calls = {"count": 0}

    def fake_run(args, **kwargs):
        calls["count"] += 1
        assert args[1:3] == ["probe", "--calendar"]
        return subprocess.CompletedProcess(args, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert booking_app._probe_calendar_health() == (True, None)
    assert booking_app._probe_calendar_health() == (True, None)
    assert calls["count"] == 1


def test_admin_health_exposes_missing_future_calendar_events(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setenv("GCAL_HELPER", "/tmp/fake-gcal-helper")
    booking_app._calendar_health_cache.update({
        "checked_at": 0.0,
        "config_marker": None,
        "ok": False,
        "warning": None,
    })
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout='{"ok": true}', stderr=""
        ),
    )
    booking_app.init_db()
    future = (date.today() + timedelta(days=30)).isoformat()
    conn = booking_app.db_conn()
    conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,session_type,status,confirmed,paid)
           VALUES (?,?,?,?,?,'mini','confirmed',1,1)""",
        (future, "16:00", "Missing Event", "missing@example.com", ""),
    )
    conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,session_type,status,confirmed,paid,
              calendar_event_id,calendar_event_url)
           VALUES (?,?,?,?,?,'mini','confirmed',1,1,?,?)""",
        (
            future,
            "16:40",
            "Linked Event",
            "linked@example.com",
            "",
            "event-123",
            "https://calendar.example/event-123",
        ),
    )
    conn.commit()
    conn.close()

    try:
        booking_app.app.config["TESTING"] = True
        with booking_app.app.test_client() as client:
            response = client.get(
                "/admin/health", headers={"X-Admin-Key": "test-admin-key"}
            )
        calendar = response.get_json()["checks"]["calendar"]
        assert calendar["configured"] is True
        assert calendar["ok"] is False
        assert calendar["linked_future_bookings"] == 1
        assert calendar["missing_future_bookings"] == 1
        assert "1 confirmed future booking" in calendar["warning"]
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_admin_calendar_backfill_requires_confirmation_and_updates_missing(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    booking_app.init_db()
    future = (date.today() + timedelta(days=30)).isoformat()
    conn = booking_app.db_conn()
    cur = conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,session_type,status,confirmed,paid)
           VALUES (?,?,?,?,?,'mini','confirmed',1,1)""",
        (future, "17:20", "Backfill Client", "backfill@example.com", ""),
    )
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()

    calls = []

    def fake_create(target_id):
        calls.append(target_id)
        conn = booking_app.db_conn()
        conn.execute(
            "UPDATE bookings SET calendar_event_id=?, calendar_event_url=? WHERE id=?",
            (
                f"calendar-{target_id}",
                f"https://calendar.example/{target_id}",
                target_id,
            ),
        )
        conn.commit()
        conn.close()
        return f"https://calendar.example/{target_id}"

    monkeypatch.setattr(booking_app, "create_calendar_event_for_booking", fake_create)
    booking_app.app.config["TESTING"] = True
    try:
        with booking_app.app.test_client() as client:
            denied = client.post(
                "/admin/calendar/backfill",
                headers={"X-Admin-Key": "test-admin-key"},
                json={"limit": 10},
            )
            assert denied.status_code == 400

            response = client.post(
                "/admin/calendar/backfill",
                headers={"X-Admin-Key": "test-admin-key"},
                json={"confirm": "BACKFILL", "limit": 10},
            )

        assert response.status_code == 200
        result = response.get_json()
        assert result["ok"] is True
        assert result["attempted"] == 1
        assert result["remaining"] == 0
        assert result["synced"][0]["booking_id"] == booking_id
        assert calls == [booking_id]
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
