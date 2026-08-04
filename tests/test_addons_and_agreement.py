"""Add-ons, agreement, and optional questionnaire regression coverage."""
import json
import os
import sqlite3
import tempfile

import pytest
import yaml

import app as booking_app


def _event(**overrides):
    ev = {
        "id": "test-mini-2026-08-01",
        "title": "Test Mini Session",
        "date": "2099-08-01",
        "start_time": "10:00",
        "end_time": "11:00",
        "session_length": 20,
        "break_length": 10,
        "slot_interval": 30,
        "deposit": 100.0,
        "full_price": 250.0,
        "location": "Test Park",
        "status": "active",
        "session_type": "mini",
        "booking_type": "fixed_slots",
        "photos": ["/static/images/placeholder.jpg"],
    }
    ev.update(overrides)
    return ev


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    monkeypatch.setattr(booking_app, "EVENTS", [_event()])
    monkeypatch.setattr(booking_app, "SETTINGS", {})
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "_send_client_email", lambda *a, **kw: True, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app._assistant_attempts.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _reserve(c, event_id="test-mini-2026-08-01", time="10:00", **extra):
    payload = {
        "event_id": event_id,
        "date": "2099-08-01",
        "time": time,
        "name": "Addon Client",
        "email": "addon@example.com",
        "phone": "4035550000",
        "instagram": "@addon",
    }
    payload.update(extra)
    return c.post("/reserve", json=payload)


def test_event_active_addons_filters_inactive_and_defaults_missing():
    ev = _event(addons=[
        {"id": "extra-10-edited-images", "title": "10 Extra Edited Images", "price": 50, "active": True},
        {"id": "inactive", "title": "Hidden", "price": 99, "active": False},
    ])

    active = booking_app._event_active_addons(ev)

    assert [a["id"] for a in active] == ["extra-10-edited-images"]
    assert active[0]["price"] == 50.0
    assert booking_app._event_active_addons(_event()) == []


def test_validate_selected_addons_rejects_unknown_and_inactive():
    ev = _event(addons=[
        {"id": "extra-10-edited-images", "title": "10 Extra Edited Images", "price": 50, "active": True},
        {"id": "short-vertical-reel", "title": "Short Vertical Behind-the-Scenes Reel", "price": 50, "active": False},
    ])

    selected, total = booking_app._validate_selected_addons(ev, ["extra-10-edited-images"])
    assert total == 50.0
    assert selected[0]["title"] == "10 Extra Edited Images"

    with pytest.raises(ValueError, match="Unknown or inactive add-on"):
        booking_app._validate_selected_addons(ev, ["missing"])

    with pytest.raises(ValueError, match="Unknown or inactive add-on"):
        booking_app._validate_selected_addons(ev, ["short-vertical-reel"])


def test_booking_total_and_balance_include_addon_snapshot():
    ev = {"full_price": 250.0, "deposit": 100.0}
    booking = {
        "full_price": 300.0,
        "deposit_amount": 100.0,
        "paid_amount": 100.0,
        "addons_total": 50.0,
    }

    assert booking_app._booking_addons_total(booking) == 50.0
    assert booking_app._booking_total_price(booking, ev) == 300.0
    assert booking_app._booking_balance_due(booking, ev) == 200.0


def test_init_db_adds_addon_agreement_questionnaire_columns(client):
    _, db_path = client
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()}
    conn.close()

    assert {
        "selected_addons_json",
        "addons_total",
        "marketing_consent",
        "agreement_name",
        "agreement_accepted_at",
        "terms_version",
        "questionnaire_answers_json",
    }.issubset(cols)


def test_reserve_legacy_event_without_agreement_still_succeeds(client):
    c, db_path = client

    res = _reserve(c)

    assert res.status_code == 200
    body = res.get_json()
    assert body["success"] is True
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT full_price, addons_total, selected_addons_json FROM bookings WHERE id=?", (body["booking_id"],)).fetchone()
    conn.close()
    assert row[0] == 250.0
    assert row[1] in (0, 0.0, None)
    assert row[2] in (None, "", "[]")


def test_reserve_stores_valid_addons_and_price_snapshot(client, monkeypatch):
    c, db_path = client
    monkeypatch.setattr(booking_app, "EVENTS", [_event(addons=[
        {
            "id": "extra-10-edited-images",
            "title": "10 Extra Edited Images",
            "description": "Add 10 additional professionally edited images.",
            "price": 50,
            "active": True,
        },
        {
            "id": "short-vertical-reel",
            "title": "Short Vertical Behind-the-Scenes Reel",
            "description": "A short vertical video up to 1 minute.",
            "price": 50,
            "active": True,
        },
    ])])

    res = _reserve(c, addons=["extra-10-edited-images"])

    assert res.status_code == 200
    booking_id = res.get_json()["booking_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
    conn.close()
    selected = json.loads(row["selected_addons_json"])
    assert row["addons_total"] == 50.0
    assert row["full_price"] == 300.0
    assert row["deposit_amount"] == 100.0
    assert selected[0]["id"] == "extra-10-edited-images"


def test_reserve_rejects_unknown_addon(client, monkeypatch):
    c, _db_path = client
    monkeypatch.setattr(booking_app, "EVENTS", [_event(addons=[
        {"id": "extra-10-edited-images", "title": "10 Extra Edited Images", "price": 50, "active": True},
    ])])

    res = _reserve(c, addons=["missing-addon"])

    assert res.status_code == 400
    assert "add-on" in res.get_json()["error"].lower()


def test_public_events_payload_exposes_only_active_addons(client, monkeypatch):
    c, _db_path = client
    monkeypatch.setattr(booking_app, "EVENTS", [_event(addons=[
        {"id": "extra-10-edited-images", "title": "10 Extra Edited Images", "price": 50, "active": True},
        {"id": "short-vertical-reel", "title": "Short Vertical Behind-the-Scenes Reel", "price": 50, "active": False},
    ])])

    res = c.get("/events")

    assert res.status_code == 200
    ev = res.get_json()["events"][0]
    assert [a["id"] for a in ev["addons"]] == ["extra-10-edited-images"]
    assert ev["addons"][0]["price"] == 50.0
    assert "agreement" not in ev
    assert "questionnaire" not in ev


def test_reserve_requires_agreement_fields_only_when_enabled(client, monkeypatch):
    c, db_path = client
    monkeypatch.setattr(booking_app, "EVENTS", [_event(agreement={
        "enabled": True,
        "require_terms": True,
        "require_marketing_choice": True,
        "terms_version": "booking-terms-v1",
    })])

    assert _reserve(c).status_code == 400
    assert _reserve(c, terms_accepted=True, marketing_consent="maybe", agreement_name="Client Name").status_code == 400
    assert _reserve(c, terms_accepted=True, marketing_consent="no", agreement_name="Client Name").status_code == 200

    booking_id = _reserve(c, time="10:30", terms_accepted=True, marketing_consent="yes", agreement_name="Second Client").get_json()["booking_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
    conn.close()
    assert row["marketing_consent"] == "yes"
    assert row["agreement_name"] == "Second Client"
    assert row["agreement_accepted_at"]
    assert row["terms_version"] == "booking-terms-v1"


def test_mini_session_ignores_questionnaire_answers_during_reserve(client):
    c, db_path = client

    res = _reserve(c, questionnaire_answers={"session_goals": "<script>bad()</script>"})

    assert res.status_code == 200
    booking_id = res.get_json()["booking_id"]
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT questionnaire_answers_json FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    assert row[0] in (None, "", "{}")


def test_questionnaire_endpoint_is_token_protected_and_non_mini_only(client, monkeypatch):
    c, db_path = client
    non_mini = _event(
        id="test-individual-2026-08-01",
        session_type="individual",
        booking_type="fixed_slots",
        questionnaire={
            "enabled": True,
            "timing": "after_confirmed_payment",
            "session_types": ["individual", "custom", "private"],
            "optional": True,
            "fields": [
                {"id": "session_goals", "label": "What would you love to capture?", "type": "textarea", "required": False},
            ],
        },
    )
    monkeypatch.setattr(booking_app, "EVENTS", [non_mini])
    res = _reserve(c, event_id=non_mini["id"])
    booking_id = res.get_json()["booking_id"]
    token = res.get_json()["confirmation_token"]
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE bookings SET status='confirmed', confirmed=1, paid=1 WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()

    assert c.get(f"/questionnaire?booking_id={booking_id}").status_code in (302, 403, 404)
    assert c.get(f"/questionnaire?booking_id={booking_id}&token=wrong").status_code in (302, 403, 404)
    ok = c.get(f"/questionnaire?booking_id={booking_id}&token={token}")
    assert ok.status_code == 200
    assert "What would you love to capture?" in ok.get_data(as_text=True)

    post = c.post(
        f"/questionnaire?booking_id={booking_id}&token={token}",
        data={"session_goals": "<b>Natural family photos</b>"},
    )
    assert post.status_code in (200, 302)
    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT questionnaire_answers_json FROM bookings WHERE id=?", (booking_id,)).fetchone()[0]
    conn.close()
    assert "Natural family photos" in stored
    assert "<b>" not in stored


def test_client_email_context_links_questionnaire_only_for_confirmed_non_mini():
    event = _event(
        session_type="individual",
        questionnaire={
            "enabled": True,
            "timing": "after_confirmed_payment",
            "session_types": ["individual", "custom", "private"],
            "optional": True,
            "fields": [{"id": "session_goals", "label": "Goals", "type": "textarea"}],
        },
    )
    booking = {
        "id": 77,
        "status": "confirmed",
        "confirmed": 1,
        "paid": 1,
        "confirmation_token": "tok",
        "full_price": 300,
        "deposit_amount": 100,
        "paid_amount": 100,
    }

    assert "/questionnaire?" in booking_app._client_email_context(booking, event)["questionnaire_url"]
    mini_event = dict(event, session_type="mini")
    assert booking_app._client_email_context(booking, mini_event)["questionnaire_url"] is None


def test_success_page_shows_addons_and_amount_wording(client, monkeypatch):
    c, _db_path = client
    monkeypatch.setattr(booking_app, "EVENTS", [_event(addons=[
        {"id": "extra-10-edited-images", "title": "10 Extra Edited Images", "price": 50, "active": True},
    ])])
    res = _reserve(c, addons=["extra-10-edited-images"])
    booking_id = res.get_json()["booking_id"]
    token = res.get_json()["confirmation_token"]

    page = c.get(f"/success?booking_id={booking_id}&token={token}")

    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Selected add-ons" in html
    assert "10 Extra Edited Images" in html
    assert "Amount due today" in html
    assert "Remaining balance" in html


def test_admin_create_mini_event_defaults_to_addons_and_agreement(client, monkeypatch, tmp_path):
    c, _db_path = client
    events_path = tmp_path / "events.yaml"
    events_path.write_text(yaml.dump({"events": [], "settings": {}}, sort_keys=False))
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(events_path))

    res = c.post("/admin/events/create", headers={"X-Admin-Key": "test-admin-key"}, json={
        "title": "New Summer Mini",
        "date": "2026-08-15",
        "start_time": "15:00",
        "end_time": "17:00",
        "deposit": 100,
        "full_price": 200,
        "location": "Test Park",
        "session_type": "mini",
        "booking_type": "fixed_slots",
        "included": ["20-minute photo session"],
    })

    assert res.status_code == 200
    stored = yaml.safe_load(events_path.read_text())["events"][0]
    assert [a["id"] for a in stored["addons"]] == ["extra-10-edited-images", "short-vertical-reel"]
    assert all(a["active"] is True for a in stored["addons"])
    assert stored["agreement"] == {
        "enabled": True,
        "require_terms": True,
        "require_marketing_choice": True,
        "terms_version": "mini-session-terms-v1",
    }


def test_admin_update_mini_event_can_enable_agreement(client, monkeypatch, tmp_path):
    c, _db_path = client
    events_path = tmp_path / "events.yaml"
    events_path.write_text(yaml.dump({"events": [_event(addons=[])], "settings": {}}, sort_keys=False))
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(events_path))

    res = c.post("/admin/events/test-mini-2026-08-01/update", headers={"X-Admin-Key": "test-admin-key"}, json={
        "agreement": {
            "enabled": True,
            "require_terms": True,
            "require_marketing_choice": True,
            "terms_version": "mini-session-terms-v1",
        },
        "addons": booking_app._default_mini_addons(),
    })

    assert res.status_code == 200
    stored = yaml.safe_load(events_path.read_text())["events"][0]
    assert stored["agreement"]["enabled"] is True
    assert [a["id"] for a in stored["addons"]] == ["extra-10-edited-images", "short-vertical-reel"]
