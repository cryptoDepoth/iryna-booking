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
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "send_confirmation_email", lambda booking_id: True, raising=False)
    monkeypatch.setattr(booking_app, "_send_client_email", lambda **kwargs: True, raising=False)
    monkeypatch.setattr(booking_app, "_notify_admin", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "create_calendar_event_for_booking", lambda booking_id: "", raising=False)
    booking_app._rate_limits.clear()
    booking_app._analytics_attempts.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c, db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _first_event_and_slot(c):
    ev = [e for e in booking_app.EVENTS if e.get("status") in ("active", "upcoming") and not e.get("hidden")][0]
    resp = c.get(f"/slots/{ev['date']}?event_id={ev['id']}")
    assert resp.status_code == 200
    slot = resp.get_json()["slots"][0]["time"]
    return ev, slot


def _reserve_payload(ev, slot, **overrides):
    payload = {
        "event_id": ev["id"],
        "date": ev["date"],
        "time": slot,
        "name": "Analytics Test Client",
        "email": "analytics-test@example.com",
        "phone": "4035551111",
        "instagram": "@analytics_test",
        "session_type": ev.get("session_type", "mini"),
        "terms_accepted": True,
        "agreement_name": "Analytics Test Client",
        "marketing_consent": "no",
    }
    payload.update(overrides)
    return payload


def _admin_headers():
    return {"X-Admin-Key": "test-admin-key"}


def test_track_endpoint_persists_visitor_session_and_funnel_event(client):
    c, db_path = client

    resp = c.post("/track", json={
        "visitor_id": "v_test_123",
        "event_name": "drawer_open",
        "event_id": "family-mini",
        "page": "/?utm_source=meta&utm_campaign=site_ab&utm_content=creative_a&fbclid=fb123",
        "utm": {
            "source": "meta",
            "medium": "paid",
            "campaign": "site_ab",
            "content": "creative_a",
        },
        "fbclid": "fb123",
        "metadata": {"slot_count": 3},
    }, headers={"Referer": "https://instagram.com/"})

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    session = conn.execute("SELECT * FROM visitor_sessions WHERE visitor_id=?", ("v_test_123",)).fetchone()
    event = conn.execute("SELECT * FROM analytics_events WHERE visitor_id=?", ("v_test_123",)).fetchone()
    conn.close()

    assert session["utm_source"] == "meta"
    assert session["utm_campaign"] == "site_ab"
    assert session["utm_content"] == "creative_a"
    assert session["fbclid"] == "fb123"
    assert session["referrer"] == "https://instagram.com/"
    assert event["event_name"] == "drawer_open"
    assert event["event_id"] == "family-mini"
    assert '"slot_count": 3' in event["metadata_json"]


def test_reserve_links_booking_to_visitor_and_attribution(client):
    c, db_path = client
    ev, slot = _first_event_and_slot(c)

    c.post("/track", json={
        "visitor_id": "v_booking_456",
        "event_name": "slot_selected",
        "event_id": ev["id"],
        "utm": {"source": "meta", "medium": "paid", "campaign": "dm_vs_site", "content": "winning_post"},
        "fbclid": "fb456",
        "landing_url": "https://book.pashynskaphoto.com/?utm_source=meta&utm_campaign=dm_vs_site&utm_content=winning_post&fbclid=fb456",
    })

    resp = c.post("/reserve", json=_reserve_payload(ev, slot,
        visitor_id="v_booking_456",
        utm_source="meta",
        utm_medium="paid",
        utm_campaign="dm_vs_site",
        utm_content="winning_post",
        fbclid="fb456",
        landing_url="https://book.pashynskaphoto.com/?utm_source=meta&utm_campaign=dm_vs_site&utm_content=winning_post&fbclid=fb456",
    ))

    assert resp.status_code == 200
    booking_id = resp.get_json()["booking_id"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    reserved_event = conn.execute("SELECT * FROM analytics_events WHERE booking_id=? AND event_name='booking_reserved'", (booking_id,)).fetchone()
    conn.close()

    assert booking["visitor_id"] == "v_booking_456"
    assert booking["utm_source"] == "meta"
    assert booking["utm_medium"] == "paid"
    assert booking["utm_campaign"] == "dm_vs_site"
    assert booking["utm_content"] == "winning_post"
    assert booking["fbclid"] == "fb456"
    assert reserved_event is not None
    assert reserved_event["visitor_id"] == "v_booking_456"


def test_frontend_contains_tracking_hooks_and_utm_forwarding(client):
    c, _ = client
    resp = c.get("/?utm_source=meta&utm_campaign=dm_vs_site&utm_content=creative_a&fbclid=fb789")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "function trackBookingEvent" in html
    assert "captureAttribution" in html
    assert "visitor_id:" in html
    assert "utm_campaign:" in html
    assert "trackSessionView" in html
    assert "trackBookingEvent('form_started'" in html
    assert "trackBookingEvent('drawer_open'" in html
    assert "trackBookingEvent('slot_selected'" in html
    assert "trackBookingEvent('payment_view'" in html
    assert "trackBookingEvent('payment_sent_clicked'" in html


def test_admin_analytics_report_and_csv_include_confirmed_campaign_booking(client):
    c, db_path = client
    ev, slot = _first_event_and_slot(c)

    c.post("/track", json={
        "visitor_id": "v_admin_report",
        "event_name": "page_view",
        "event_id": ev["id"],
        "utm": {"source": "meta", "medium": "paid", "campaign": "dm_vs_site", "content": "creative_report"},
    })
    resp = c.post("/reserve", json=_reserve_payload(ev, slot,
        visitor_id="v_admin_report",
        utm_source="meta",
        utm_medium="paid",
        utm_campaign="dm_vs_site",
        utm_content="creative_report",
    ))
    assert resp.status_code == 200
    booking_id = resp.get_json()["booking_id"]

    confirm = c.post("/admin/confirm", json={"booking_id": booking_id}, headers=_admin_headers())
    assert confirm.status_code == 200
    assert confirm.get_json()["success"] is True

    html_resp = c.get("/admin/analytics?date_from=2020-01-01&date_to=2035-01-01", headers=_admin_headers())
    html = html_resp.get_data(as_text=True)
    assert html_resp.status_code == 200
    assert "dm_vs_site" in html
    assert "creative_report" in html
    assert "Confirmed" in html

    csv_resp = c.get("/admin/analytics.csv?date_from=2020-01-01&date_to=2035-01-01", headers=_admin_headers())
    csv_text = csv_resp.get_data(as_text=True)
    assert csv_resp.status_code == 200
    assert "campaign,content,visits" in csv_text
    assert "dm_vs_site,creative_report" in csv_text

    # Synthetic QA/smoke rows stay out of the default business report,
    # but remain available with include_internal=1 for debugging.
    c.post("/track", json={
        "visitor_id": "v_codex_smoke",
        "event_name": "page_view",
        "event_id": ev["id"],
        "utm": {"source": "qa", "medium": "test", "campaign": "codex_live_smoke", "content": "creative_a"},
    })
    default_csv = c.get("/admin/analytics.csv?date_from=2020-01-01&date_to=2035-01-01", headers=_admin_headers()).get_data(as_text=True)
    debug_csv = c.get("/admin/analytics.csv?date_from=2020-01-01&date_to=2035-01-01&include_internal=1", headers=_admin_headers()).get_data(as_text=True)
    assert "codex_live_smoke" not in default_csv
    assert "codex_live_smoke" in debug_csv

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    confirmed_event = conn.execute(
        "SELECT * FROM analytics_events WHERE booking_id=? AND event_name='booking_confirmed'",
        (booking_id,),
    ).fetchone()
    conn.close()
    assert confirmed_event is not None


def test_admin_analytics_groups_mountain_mini_campaigns(client):
    c, _ = client

    for visitor, campaign in (
        ("v_mm_legacy", "mountain_mini_booking_ab_202606"),
        ("v_mm_jun20", "mountain_mini_jun20"),
        ("v_other", "dm_vs_site"),
    ):
        c.post("/track", json={
            "visitor_id": visitor,
            "event_name": "page_view",
            "utm": {"source": "meta", "medium": "paid", "campaign": campaign, "content": "creative_a"},
        })

    html = c.get(
        "/admin/analytics?date_from=2020-01-01&date_to=2035-01-01",
        headers=_admin_headers(),
    ).get_data(as_text=True)
    # Both mountain campaigns roll up under one display group...
    assert "Mountain Mini" in html
    # ...while raw campaign rows stay visible untouched.
    assert "mountain_mini_booking_ab_202606" in html
    assert "mountain_mini_jun20" in html

    csv_text = c.get(
        "/admin/analytics.csv?date_from=2020-01-01&date_to=2035-01-01",
        headers=_admin_headers(),
    ).get_data(as_text=True)
    # Existing column order is preserved; campaign_group is appended last.
    assert csv_text.startswith("campaign,content,visits")
    header = csv_text.splitlines()[0]
    assert header.endswith("campaign_group")
    for line in csv_text.splitlines()[1:]:
        if line.startswith("mountain_mini_"):
            assert line.endswith("Mountain Mini")
        elif line.startswith("dm_vs_site"):
            assert not line.endswith("Mountain Mini")


def test_mountain_mini_qa_campaign_hidden_from_default_report(client):
    c, _ = client

    c.post("/track", json={
        "visitor_id": "v_mm_qa",
        "event_name": "page_view",
        "utm": {"source": "qa", "medium": "manual", "campaign": "mountain_mini_qa", "content": "claude_code_verify"},
    })

    default_csv = c.get(
        "/admin/analytics.csv?date_from=2020-01-01&date_to=2035-01-01",
        headers=_admin_headers(),
    ).get_data(as_text=True)
    debug_csv = c.get(
        "/admin/analytics.csv?date_from=2020-01-01&date_to=2035-01-01&include_internal=1",
        headers=_admin_headers(),
    ).get_data(as_text=True)
    assert "claude_code_verify" not in default_csv
    assert "claude_code_verify" in debug_csv


def test_expired_booking_records_funnel_event(client):
    c, db_path = client
    ev, slot = _first_event_and_slot(c)

    resp = c.post("/reserve", json=_reserve_payload(ev, slot,
        visitor_id="v_expired_report",
        utm_source="meta",
        utm_medium="paid",
        utm_campaign="expiry_test",
        utm_content="creative_expiry",
    ))
    assert resp.status_code == 200
    booking_id = resp.get_json()["booking_id"]

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE bookings SET reserved_until='2020-01-01T00:00:00', status='reserved', confirmed=0, paid=0 WHERE id=?",
        (booking_id,),
    )
    conn.commit()
    conn.close()

    assert booking_app.expire_reservations() == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    expired_event = conn.execute(
        "SELECT * FROM analytics_events WHERE booking_id=? AND event_name='booking_expired'",
        (booking_id,),
    ).fetchone()
    booking = conn.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()

    assert booking["status"] == "expired"
    assert expired_event is not None
    assert expired_event["visitor_id"] == "v_expired_report"
