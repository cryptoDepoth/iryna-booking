"""Waitlist / Notify me regression tests.

The waitlist is intentionally low-risk: it must not reserve slots or modify the
booking flow. It only captures interested clients for sold-out sessions.
"""
import os
import sqlite3
import tempfile

import pytest

import app as booking_app


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "_notify_waitlist_signup", lambda entry: None, raising=False)
    booking_app._rate_limits.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _event_id():
    return booking_app.EVENTS[0]["id"]


def test_waitlist_table_created(client):
    _c, db_path = client
    conn = sqlite3.connect(db_path)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='waitlist'").fetchall()
    conn.close()
    assert tables


def test_waitlist_requires_event_name_and_email(client):
    c, _db = client
    response = c.post("/waitlist", json={"event_id": _event_id(), "name": "A"})
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_waitlist_accepts_valid_signup(client):
    c, db_path = client
    response = c.post("/waitlist", json={
        "event_id": _event_id(),
        "name": "Test Client",
        "email": "client@example.com",
        "phone": "4035550000",
        "preferred_slot": "any morning",
    })
    assert response.status_code == 200
    assert response.get_json()["success"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM waitlist WHERE email=?", ("client@example.com",)).fetchone()
    conn.close()
    assert row is not None
    assert row["event_id"] == _event_id()
    assert row["name"] == "Test Client"


def test_waitlist_duplicate_email_for_same_event_is_idempotent(client):
    c, db_path = client
    payload = {"event_id": _event_id(), "name": "Test Client", "email": "client@example.com"}

    first = c.post("/waitlist", json=payload)
    second = c.post("/waitlist", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["success"] is True
    assert second.get_json()["duplicate"] is True

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM waitlist WHERE event_id=? AND email=?", (_event_id(), "client@example.com")).fetchone()[0]
    conn.close()
    assert count == 1


def test_waitlist_frontend_posts_to_real_endpoint(client):
    c, _db = client
    response = c.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "fetch('/waitlist'" in html
    assert "API.waitlist" in html
    assert "waitlist_duplicate_msg" in html
