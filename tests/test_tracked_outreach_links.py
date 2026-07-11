import os
import sqlite3
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest

import app as booking_app


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    booking_app.init_db()
    with booking_app.app.test_client() as c:
        yield c, db_path
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _admin_headers():
    return {"X-Admin-Key": "test-admin-key"}


def test_admin_creates_durable_dm_link_and_redirect_tracks_click(client):
    c, db_path = client
    response = c.post(
        "/admin/api/tracked-links",
        headers=_admin_headers(),
        json={
            "client_name": "@calgary_client",
            "source_key": "ig_dm",
            "landing_path": "/family",
            "campaign_label": "Canoe DM Follow-up",
            "ad_id": "120246459470790408",
            "notes": "Asked about July availability",
        },
    )
    assert response.status_code == 201
    data = response.get_json()
    assert data["ok"] is True
    assert "/go/" in data["short_url"]
    assert data["utm"] == {
        "source": "instagram",
        "medium": "dm",
        "campaign": "canoe_dm_follow_up",
        "content": "120246459470790408",
        "term": data["code"],
    }

    redirect_response = c.get(f"/go/{data['code']}")
    assert redirect_response.status_code == 302
    target = urlparse(redirect_response.headers["Location"])
    params = parse_qs(target.query)
    assert target.path == "/family"
    assert params["utm_source"] == ["instagram"]
    assert params["utm_medium"] == ["dm"]
    assert params["utm_campaign"] == ["canoe_dm_follow_up"]
    assert params["utm_content"] == ["120246459470790408"]
    assert params["utm_term"] == [data["code"]]
    assert redirect_response.headers["Cache-Control"] == "no-store"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tracked_links WHERE code=?", (data["code"],)).fetchone()
    conn.close()
    assert row["clicks"] == 1
    assert row["first_clicked_at"]
    assert row["last_clicked_at"]


def test_generator_reports_bookings_for_unique_link_code(client):
    c, db_path = client
    created = c.post(
        "/admin/api/tracked-links",
        headers=_admin_headers(),
        json={
            "client_name": "Sarah",
            "source_key": "ig_dm",
            "landing_path": "/wedding",
            "campaign_label": "Wedding post",
            "ad_id": "ad_123",
        },
    ).get_json()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO bookings
           (date, time, name, email, phone, session_type, status, paid, confirmed, utm_term)
           VALUES (?, ?, ?, ?, ?, ?, 'confirmed', 1, 1, ?)""",
        ("2030-01-01", "10:00", "Sarah", "sarah@example.com", "4035550000", "wedding", created["code"]),
    )
    conn.commit()
    conn.close()

    page = c.get("/admin/link-generator", headers=_admin_headers())
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Sarah" in html
    assert created["code"] in html
    assert "ad_123" in html
    assert '"bookings": 1' in html
    assert '"confirmed_bookings": 1' in html


def test_tracked_link_api_rejects_unknown_landing_path(client):
    c, _ = client
    response = c.post(
        "/admin/api/tracked-links",
        headers=_admin_headers(),
        json={
            "client_name": "Unsafe redirect",
            "source_key": "ig_dm",
            "landing_path": "https://example.com/phishing",
        },
    )
    assert response.status_code == 400


def test_pinterest_domain_verification_is_present_on_homepage(client):
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    assert '<meta name="p:domain_verify" content="3e5fb25498ba763c59a7e3f30193a97c">' in html

