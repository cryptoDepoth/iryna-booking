"""Regression tests for admin event price/deposit edits reaching public pages."""

import yaml

import app as booking_app
from tests.test_admin_event_organizer import admin_client, _headers, _patch_events_yaml, _yaml_event


def test_admin_event_price_update_survives_meta_save_and_public_events_api(admin_client, monkeypatch, tmp_path):
    c, _db_path = admin_client
    path = _patch_events_yaml(monkeypatch, tmp_path, [_yaml_event(
        id="price-event",
        title="Original Price Event",
        deposit=110.0,
        full_price=220.0,
        included=["Original include"],
    )])

    sched = c.post(
        "/admin/events/price-event/update",
        headers=_headers(),
        json={
            "start_time": "10:00",
            "end_time": "12:00",
            "session_length": 20,
            "break_length": 10,
            "deposit": 90,
            "full_price": 180,
            "status": "active",
            "location": "Updated Park",
            "booking_type": "fixed_slots",
            "session_type": "mini",
        },
    )
    assert sched.status_code == 200
    assert sched.get_json()["event"]["deposit"] == 90.0
    assert sched.get_json()["event"]["full_price"] == 180.0

    meta = c.post(
        "/admin/events/price-event/update-meta",
        headers=_headers(),
        json={
            "title": "Updated Price Event",
            "subtitle": "Updated subtitle",
            "date": "2026-08-01",
            "featured": True,
            "included": ["Updated include"],
        },
    )
    assert meta.status_code == 200

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["events"][0]
    assert saved["deposit"] == 90.0
    assert saved["full_price"] == 180.0
    assert saved["title"] == "Updated Price Event"
    assert saved["included"] == ["Updated include"]

    public = c.get("/events").get_json()["events"][0]
    assert public["deposit"] == 90.0
    assert public["price"] == 90.0
    assert public["full_price"] == 180.0

    html = c.get("/?event=price-event").get_data(as_text=True)
    assert '"deposit": 90.0' in html
    assert '"full_price": 180.0' in html
    assert '"deposit": 110.0' not in html
    assert '"full_price": 220.0' not in html


def test_admin_save_event_does_not_parallel_write_schedule_and_meta():
    # Frontend regression: update and update-meta both write events.yaml. Running
    # them with Promise.all caused the later writer to save a stale YAML snapshot
    # and silently revert deposit/full_price while still showing “Saved”.
    html = open("templates/admin.html", encoding="utf-8").read()
    save_fn_start = html.index("async function saveAllEventSettings")
    save_fn_end = html.index("function toggleBookingTypeFields", save_fn_start)
    save_fn = html[save_fn_start:save_fn_end]
    assert "Promise.all" not in save_fn
    assert save_fn.index("/update`") < save_fn.index("/update-meta`")
