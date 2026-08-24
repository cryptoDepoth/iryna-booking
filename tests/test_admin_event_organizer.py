"""Regression coverage for the event-first admin organizer."""
import os
import sqlite3
import sys
import tempfile
import types
import yaml
from datetime import datetime as real_datetime
from pathlib import Path

import pytest

import app as booking_app


@pytest.fixture()
def admin_client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kw: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kw: None, raising=False)
    monkeypatch.setattr(booking_app, "_send_client_email", lambda *a, **kw: True, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _headers():
    return {"X-Admin-Key": "test-admin-key"}


def _yaml_event(**overrides):
    event = {
        "id": "addon-event",
        "title": "Addon Event",
        "date": "2026-08-01",
        "start_time": "10:00",
        "end_time": "11:00",
        "session_length": 20,
        "break_length": 10,
        "slot_interval": 30,
        "deposit": 100.0,
        "full_price": 250.0,
        "location": "Test Park",
        "session_type": "mini",
        "booking_type": "fixed_slots",
        "featured": False,
        "status": "active",
        "included": [],
        "photos": ["/static/images/placeholder.jpg"],
    }
    event.update(overrides)
    return event


def _patch_events_yaml(monkeypatch, tmp_path, events):
    path = tmp_path / "events.yaml"
    path.write_text(yaml.safe_dump({"events": events, "settings": {}}, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(booking_app, "_EVENTS_PATH", str(path))
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(path))
    monkeypatch.setattr(booking_app, "EVENTS", events)
    monkeypatch.setattr(booking_app, "SETTINGS", {})
    return path


def _insert_booking(db_path, **overrides):
    fields = {
        "date": overrides.get("date", "2026-06-07"),
        "time": overrides.get("time", "15:30"),
        "name": overrides.get("name", "Organizer Client"),
        "email": overrides.get("email", "organizer@example.com"),
        "phone": overrides.get("phone", "4035550000"),
        "instagram": overrides.get("instagram", ""),
        "session_type": overrides.get("session_type", "mini"),
        "status": overrides.get("status", "pending_payment"),
        "confirmed": overrides.get("confirmed", 0),
        "paid": overrides.get("paid", 0),
        "paid_amount": overrides.get("paid_amount", 0.0),
        "deposit_amount": overrides.get("deposit_amount", 250.0),
        "full_price": overrides.get("full_price", 500.0),
        "event_id": overrides.get("event_id", "lilac-jun7"),
    }
    conn = sqlite3.connect(db_path)
    cols = ",".join(fields)
    placeholders = ",".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO bookings ({cols}) VALUES ({placeholders})", list(fields.values()))
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()
    return booking_id


def test_admin_dashboard_is_event_first_and_uses_real_next_event(admin_client, monkeypatch):
    c, _db_path = admin_client

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            value = real_datetime(2026, 5, 30, 12, 0, 0)
            return value.replace(tzinfo=tz) if tz else value

    monkeypatch.setattr(booking_app, "datetime", FixedDateTime)

    resp = c.get("/admin", headers=_headers())
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Photosession Organizer" in html
    assert 'data-next-event-id="lilac-jun7"' in html
    assert 'data-next-event-id="blossom-may3"' not in html


def test_admin_event_slots_include_client_roster(admin_client):
    c, db_path = admin_client
    booking_id = _insert_booking(db_path, name="Roster Client", phone="4035551234")

    resp = c.get("/admin/api/event/lilac-jun7/slots", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["summary"]["pending"] == 1
    roster = body["bookings"]
    assert any(row["id"] == booking_id and row["name"] == "Roster Client" for row in roster)
    assert any(slot["booking_id"] == booking_id for slot in body["slots"])


def test_admin_dashboard_hides_internal_slot_guards_from_client_table(admin_client):
    c, db_path = admin_client
    _insert_booking(
        db_path,
        name="TECHNICAL SLOT GUARD",
        session_type="internal_block",
        status="reserved",
    )

    response = c.get("/admin", headers=_headers())
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "TECHNICAL SLOT GUARD" not in html
    assert '<option value="internal_block">' not in html


def test_admin_bookings_are_grouped_by_day_and_individuals_are_distinct(admin_client):
    c, db_path = admin_client
    _insert_booking(
        db_path,
        date="2026-08-23",
        time="15:00",
        name="Photo Day One",
        status="confirmed",
        confirmed=1,
    )
    _insert_booking(
        db_path,
        date="2026-08-23",
        time="16:00",
        name="Photo Day Two",
        status="confirmed",
        confirmed=1,
    )
    _insert_booking(
        db_path,
        date="2026-09-19",
        time="12:00",
        name="Individual Client",
        session_type="individual",
        status="confirmed",
        confirmed=1,
    )

    response = c.get("/admin", headers=_headers())
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.count('📅 2026-08-23') == 1
    assert html.count('📅 2026-09-19') == 1
    assert 'row-confirmed row-individual' in html


def test_reschedule_deep_link_embeds_target_even_outside_visible_filters(admin_client):
    c, db_path = admin_client
    booking_id = _insert_booking(
        db_path,
        date="2027-12-31",
        time="17:40",
        name="Deep Link Client",
        status="confirmed",
        confirmed=1,
    )

    response = c.get(
        f"/admin?reschedule_id={booking_id}&date_to=2026-01-01",
        headers=_headers(),
    )
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert f'"id": {booking_id}' in html
    assert '"name": "Deep Link Client"' in html
    assert 'const autoRescheduleTarget' in html


def test_reschedule_event_list_includes_admin_event_without_public_photos(
    admin_client, monkeypatch, tmp_path
):
    c, _db_path = admin_client
    event = _yaml_event(
        id="no-photo-admin-event",
        title="No Photo Admin Event",
        date="2026-10-10",
        photos=[],
        status="active",
    )
    _patch_events_yaml(monkeypatch, tmp_path, [event])

    response = c.get("/admin", headers=_headers())
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'const adminRescheduleEvents' in html
    assert '"id": "no-photo-admin-event"' in html
    assert '"title": "No Photo Admin Event"' in html


def test_admin_browser_schedule_preview_matches_backend_and_local_calendar():
    html = Path("templates/admin.html").read_text(encoding="utf-8")
    assert "while (cur + sessionLen <= end)" in html
    assert "getTimezoneOffset() * 60000" in html
    assert "new Date().toISOString().split('T')[0]" not in html


def test_contact_edit_requires_admin(admin_client):
    c, db_path = admin_client
    booking_id = _insert_booking(db_path)

    resp = c.post(
        f"/admin/booking/{booking_id}/contact",
        json={"phone": "4035559999", "instagram": "new.handle"},
    )
    assert resp.status_code == 401


def test_contact_edit_updates_booking_and_client_profile_only(admin_client):
    c, db_path = admin_client
    email = "contact@example.com"
    booking_id = _insert_booking(
        db_path,
        email=email,
        phone="4035550000",
        instagram="old.handle",
        paid_amount=125.0,
        status="confirmed",
        confirmed=1,
        paid=1,
    )
    other_booking_id = _insert_booking(
        db_path,
        email=email,
        time="16:00",
        phone="4035551111",
        instagram="history.handle",
        status="confirmed",
        confirmed=1,
        paid=1,
        paid_amount=250.0,
    )
    booking_app.sync_client(email, "Contact Client", "4035550000", "old.handle")

    resp = c.post(
        f"/admin/booking/{booking_id}/contact",
        headers=_headers(),
        json={"email": "corrected@example.com", "phone": "+1 587 555 2222", "instagram": "@fresh.handle"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    target = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    other = conn.execute("SELECT * FROM bookings WHERE id=?", (other_booking_id,)).fetchone()
    client = conn.execute("SELECT * FROM clients WHERE LOWER(email)=LOWER(?)", (email,)).fetchone()
    conn.close()

    assert target["phone"] == "+1 587 555 2222"
    assert target["instagram"] == "fresh.handle"
    assert target["email"] == "corrected@example.com"
    assert target["status"] == "confirmed"
    assert target["paid_amount"] == 125.0
    assert other["email"] == email
    assert other["phone"] == "4035551111"
    assert other["instagram"] == "history.handle"
    corrected_client = sqlite3.connect(db_path)
    corrected_client.row_factory = sqlite3.Row
    new_client = corrected_client.execute(
        "SELECT * FROM clients WHERE LOWER(email)=LOWER(?)",
        ("corrected@example.com",),
    ).fetchone()
    old_client = corrected_client.execute(
        "SELECT * FROM clients WHERE LOWER(email)=LOWER(?)",
        (email,),
    ).fetchone()
    corrected_client.close()
    assert new_client["phone"] == "+1 587 555 2222"
    assert new_client["instagram"] == "fresh.handle"
    assert old_client is not None


def test_clients_template_keeps_local_date_and_mobile_back_control():
    template = os.path.join(os.path.dirname(__file__), "..", "templates", "admin_clients.html")
    with open(template, encoding="utf-8") as f:
        html = f.read()
    assert "mobile-back-btn" in html
    assert 'layout").classList.add("detail-open")' in html
    assert r"^(\d{4})-(\d{2})-(\d{2})$" in html


def test_public_drawer_hides_assistant_and_toast_has_loading_copy():
    template = os.path.join(os.path.dirname(__file__), "..", "templates", "index_v2.html")
    with open(template, encoding="utf-8") as f:
        html = f.read()
    assert ".drawer-bg.open ~ .assistant-widget{display:none}" in html
    assert "if (!msg) return;" in html
    assert "toast_loading_slots: 'Loading available times" in html


def test_admin_stripe_link_creates_custom_checkout_session(admin_client, monkeypatch):
    c, _db_path = admin_client
    captured = {}

    class FakeSession:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(url="https://checkout.stripe.test/custom", id="cs_custom")

    fake_stripe = types.SimpleNamespace(
        checkout=types.SimpleNamespace(Session=FakeSession),
        error=types.SimpleNamespace(StripeError=Exception),
    )
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setattr(booking_app, "BASE_URL", "https://book.pashynskaphoto.com")

    resp = c.post(
        "/admin/stripe-link",
        headers=_headers(),
        json={
            "amount": "120.50",
            "description": "Boho photoshoot",
            "email": "client@example.com",
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["checkout_url"] == "https://checkout.stripe.test/custom"
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 12050
    assert captured["metadata"]["payment_type"] == "custom_admin_link"
    assert captured["metadata"]["description"] == "Boho photoshoot"
    assert captured["customer_email"] == "client@example.com"


def test_admin_stripe_link_requires_admin(admin_client, monkeypatch):
    c, _db_path = admin_client
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "sk_test")
    resp = c.post("/admin/stripe-link", json={"amount": "25"})
    assert resp.status_code == 401


def test_admin_event_update_saves_builtin_and_custom_addons(admin_client, monkeypatch, tmp_path):
    c, _db_path = admin_client
    path = _patch_events_yaml(monkeypatch, tmp_path, [_yaml_event()])

    resp = c.post(
        "/admin/events/addon-event/update",
        headers=_headers(),
        json={
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "break_length": 10,
            "deposit": 100,
            "full_price": 250,
            "addons": [
                {"id": "extra-10-edited-images", "title": "10 Extra Edited Images", "price": 50, "active": True},
                {"id": "short-vertical-reel", "title": "Short Vertical Behind-the-Scenes Reel", "price": 75, "active": True},
                {"id": "custom-reel-note", "title": "<b>Custom Keepsake</b>", "description": "<script>bad()</script>Safe", "price": 25, "active": True},
            ],
        },
    )

    assert resp.status_code == 200
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["events"][0]
    assert [a["id"] for a in saved["addons"]] == [
        "extra-10-edited-images",
        "short-vertical-reel",
        "custom-reel-note",
    ]
    assert saved["addons"][1]["price"] == 99.0
    assert saved["addons"][1]["title"] == "Short Vertical Highlight Video — Up to 2 Minutes"
    assert "up to 2 minutes" in saved["addons"][1]["description"]
    assert saved["addons"][2]["title"] == "Custom Keepsake"
    assert "<script>" not in saved["addons"][2]["description"]


def test_admin_event_update_with_all_addons_disabled_omits_addons_key(admin_client, monkeypatch, tmp_path):
    c, _db_path = admin_client
    path = _patch_events_yaml(monkeypatch, tmp_path, [_yaml_event(addons=[
        {"id": "extra-10-edited-images", "title": "10 Extra Edited Images", "price": 50, "active": True},
    ])])

    resp = c.post(
        "/admin/events/addon-event/update",
        headers=_headers(),
        json={
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "break_length": 10,
            "deposit": 100,
            "full_price": 250,
            "addons": [
                {"id": "extra-10-edited-images", "title": "10 Extra Edited Images", "price": 50, "active": False},
            ],
        },
    )

    assert resp.status_code == 200
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["events"][0]
    assert "addons" not in saved


def test_admin_event_create_persists_addons(admin_client, monkeypatch, tmp_path):
    c, _db_path = admin_client
    path = _patch_events_yaml(monkeypatch, tmp_path, [])

    resp = c.post(
        "/admin/events/create",
        headers=_headers(),
        json={
            "title": "Individual Portraits",
            "date": "2026-08-10",
            "start_time": "10:00",
            "end_time": "12:00",
            "session_length": 60,
            "break_length": 15,
            "deposit": 150,
            "full_price": 500,
            "booking_type": "rolling_availability",
            "session_type": "individual",
            "addons": [
                {"id": "short-vertical-reel", "title": "Short Vertical Behind-the-Scenes Reel", "price": 125, "active": True},
            ],
        },
    )

    assert resp.status_code == 200
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["events"][0]
    assert saved["session_type"] == "individual"
    assert saved["addons"][0]["id"] == "short-vertical-reel"
    assert saved["addons"][0]["price"] == 99.0
    assert saved["addons"][0]["title"] == "Short Vertical Highlight Video — Up to 2 Minutes"
