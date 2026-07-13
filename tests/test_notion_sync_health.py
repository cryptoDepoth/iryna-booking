import os
import tempfile

import app as booking_app
import pytest


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture()
def notion_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setattr(booking_app, "DB_PATH", path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "test-notion-key")
    monkeypatch.setattr(
        booking_app,
        "NOTION_HEADERS",
        {"Authorization": "Bearer test-notion-key", "Notion-Version": "2022-06-28"},
    )
    booking_app.init_db()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_notion_sync_uses_booking_event_duration_and_prices(notion_db, monkeypatch):
    event = {
        "id": "mountains-test",
        "date": "2026-08-01",
        "session_length": 30,
        "deposit": 120.75,
        "full_price": 241.50,
    }
    monkeypatch.setattr(booking_app, "EVENTS", [event])
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(json)
        return _Response(200, {"id": "notion-page-1"})

    monkeypatch.setattr(booking_app.requests, "post", fake_post)
    conn = booking_app.db_conn()
    cur = conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,session_type,status,event_id,
              deposit_amount,full_price,confirmed,paid,paid_amount)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "2026-08-01", "16:00", "Notion Client", "notion@example.com", "",
            "mini", "confirmed", event["id"], 120.75, 241.50, 1, 1, 120.75,
        ),
    )
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()

    assert booking_app.sync_to_notion(booking_id) is True
    props = captured["properties"]
    assert props["Time Slot"]["rich_text"][0]["text"]["content"] == "16:00–16:30"
    assert props["Deposit (CAD)"]["number"] == pytest.approx(120.75)
    assert props["Total (CAD)"]["number"] == pytest.approx(241.50)
    conn = booking_app.db_conn()
    assert conn.execute(
        "SELECT notion_page_id FROM bookings WHERE id=?", (booking_id,)
    ).fetchone()[0] == "notion-page-1"
    conn.close()


def test_admin_health_detects_rejected_notion_token(notion_db, monkeypatch):
    booking_app._notion_health_cache.update({
        "checked_at": 0.0,
        "token_marker": None,
        "ok": False,
        "warning": None,
    })
    monkeypatch.setattr(
        booking_app.requests,
        "get",
        lambda *args, **kwargs: _Response(401, text="unauthorized"),
    )
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as client:
        response = client.get(
            "/admin/health", headers={"X-Admin-Key": "test-admin-key"}
        )

    notion = response.get_json()["checks"]["notion"]
    assert notion["ok"] is False
    assert notion["configured"] is True
    assert notion["warning"] == "Notion API rejected credentials (401)"


def test_notion_health_probe_is_cached_for_admin_navigation(notion_db, monkeypatch):
    booking_app._notion_health_cache.update({
        "checked_at": 0.0,
        "token_marker": None,
        "ok": False,
        "warning": None,
    })
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        return _Response(200)

    monkeypatch.setattr(booking_app.requests, "get", fake_get)

    assert booking_app._probe_notion_health() == (True, None)
    assert booking_app._probe_notion_health() == (True, None)
    assert calls["count"] == 1


def test_notion_health_coverage_excludes_admin_slot_blocks(notion_db, monkeypatch):
    booking_app._notion_health_cache.update({
        "checked_at": 0.0,
        "token_marker": None,
        "ok": False,
        "warning": None,
    })
    monkeypatch.setattr(
        booking_app.requests, "get", lambda *args, **kwargs: _Response(200)
    )
    conn = booking_app.db_conn()
    conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,session_type,status,confirmed)
           VALUES ('2026-08-01','12:00','Real Client','real@example.com','',
                   'mini','reserved',0)"""
    )
    conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,session_type,status,confirmed)
           VALUES ('2026-08-01','12:40','⛔ Closed by admin','','',
                   'internal_block','reserved',0)"""
    )
    conn.commit()
    conn.close()

    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as client:
        response = client.get(
            "/admin/health", headers={"X-Admin-Key": "test-admin-key"}
        )

    notion = response.get_json()["checks"]["notion"]
    assert notion["linked_bookings"] == 0
    assert notion["unlinked_bookings"] == 1
