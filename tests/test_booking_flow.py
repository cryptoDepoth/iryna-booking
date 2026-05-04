import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as booking_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    old_db = booking_app.DB_PATH
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(booking_app, "start_active_payment_checker", lambda booking_id: False, raising=False)
    booking_app.init_db()

    with booking_app.app.test_client() as client:
        yield client, db_path

    booking_app.DB_PATH = old_db
    if os.path.exists(db_path):
        os.unlink(db_path)


def rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute("SELECT * FROM bookings ORDER BY id")]
    conn.close()
    return out


def test_confirm_requires_existing_active_reservation(client):
    c, db_path = client

    response = c.post(
        "/confirm",
        json={
            "booking_id": 999,
            "time": "10:00",
            "name": "Direct User",
            "email": "direct@example.com",
            "phone": "4035550000",
            "instagram": "@direct",
        },
    )

    assert response.status_code == 404
    assert response.get_json()["success"] is False
    assert rows(db_path) == []


def test_confirm_does_not_overwrite_another_booking(client):
    c, db_path = client

    first = c.post("/reserve", json={"time": "10:00"}).get_json()
    assert first["success"] is True

    ok = c.post(
        "/confirm",
        json={
            "booking_id": first["booking_id"],
            "name": "Alice Test",
            "email": "alice@example.com",
            "phone": "4035550001",
            "instagram": "@alice",
        },
    )
    assert ok.status_code == 200

    overwrite = c.post(
        "/confirm",
        json={
            "booking_id": first["booking_id"],
            "name": "Mallory Override",
            "email": "mallory@example.com",
            "phone": "4035559999",
            "instagram": "@mallory",
        },
    )

    assert overwrite.status_code == 409
    saved = rows(db_path)[0]
    assert saved["name"] == "Alice Test"
    assert saved["email"] == "alice@example.com"
    assert saved["status"] == "pending_payment"


def test_rejects_fake_slot_times(client):
    c, db_path = client

    reserve = c.post("/reserve", json={"time": "99:99"})
    confirm = c.post(
        "/confirm",
        json={
            "booking_id": 123,
            "time": "99:99",
            "name": "Fake",
            "email": "fake@example.com",
            "phone": "4035550002",
            "instagram": "@fake",
        },
    )

    assert reserve.status_code == 400
    assert confirm.status_code in (400, 404)
    assert rows(db_path) == []


def test_successful_flow_hides_slot_after_confirm(client):
    c, db_path = client

    reserve = c.post("/reserve", json={"time": "10:00"})
    assert reserve.status_code == 200
    booking_id = reserve.get_json()["booking_id"]

    after_reserve = c.get("/slots/2026-05-03").get_json()
    assert "10:00" not in [s["time"] for s in after_reserve["slots"]]

    confirm = c.post(
        "/confirm",
        json={
            "booking_id": booking_id,
            "name": "Client One",
            "email": "client@example.com",
            "phone": "4035550003",
            "instagram": "@client",
            "session_type": "Blossom Mini",
        },
    )
    assert confirm.status_code == 200
    assert confirm.get_json()["booking_id"] == booking_id

    after_confirm = c.get("/slots/2026-05-03").get_json()
    assert "10:00" not in [s["time"] for s in after_confirm["slots"]]

    saved = rows(db_path)[0]
    assert saved["status"] == "pending_payment"
    assert saved["name"] == "Client One"
    assert saved["email"] == "client@example.com"


def test_cancelled_slot_can_be_reserved_again(client):
    c, db_path = client

    first = c.post("/reserve", json={"time": "10:00"})
    assert first.status_code == 200
    first_id = first.get_json()["booking_id"]

    cancel = c.post("/admin/cancel", json={"booking_id": first_id})
    assert cancel.status_code == 200

    slots_after_cancel = c.get("/slots/2026-05-03").get_json()
    assert "10:00" in [s["time"] for s in slots_after_cancel["slots"]]

    second = c.post("/reserve", json={"time": "10:00"})
    assert second.status_code == 200
    assert second.get_json()["booking_id"] != first_id

    current_rows = rows(db_path)
    assert len(current_rows) == 1
    assert current_rows[0]["status"] == "reserved"
