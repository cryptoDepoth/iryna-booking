"""Booking flow regression tests — current v2 drawer flow.

Tests the client flow: reserve → confirm payment submitted → status polling → admin confirm.
Uses a temp SQLite database and mocks external services.
"""
import os
import tempfile
import pytest

import app as booking_app  # noqa: E402
import assistant_engine  # noqa: E402


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


def _first_event():
    active_events = [e for e in booking_app.EVENTS if e.get("status") in ("active", "upcoming") and not e.get("hidden")]
    assert active_events, "No active events configured"
    return active_events[0]


def _first_slot(client_tuple):
    """Get the first available slot time from /slots/<date>?event_id=<id>."""
    c, _ = client_tuple
    ev = _first_event()
    resp = c.get(f"/slots/{ev['date']}?event_id={ev['id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    slots = data.get("slots", [])
    assert slots, f"No slots returned for event {ev['id']}"
    return slots[0]["time"], ev["date"], ev["id"]


def _reserve(c, slot_time, event_id, *, name="Test Client", email="test@example.com"):
    return c.post("/reserve", json={
        "event_id": event_id,
        "time": slot_time,
        "name": name,
        "email": email,
        "phone": "4035550000",
        "instagram": "@test",
    })


def test_public_home_renders_without_undefined_template_config(client):
    """Public landing page must render JS config values instead of Jinja Undefined.

    Regression: index_v2.html uses `stripe_enabled | tojson`; if the route does
    not pass stripe_enabled, Jinja raises TypeError: Undefined is not JSON
    serializable and the public site returns HTTP 500.
    """
    c, _ = client

    resp = c.get("/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "const STRIPE_ENABLED =" in html
    assert "Undefined" not in html


def test_public_privacy_page_discloses_storage_without_cookie_banner(client):
    """Best current UX: no intrusive cookie popup, but transparent privacy/storage disclosure."""
    c, _ = client

    resp = c.get("/privacy")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Privacy" in html
    assert "No cookie banner" in html
    assert "localStorage" in html
    assert "sessionStorage" in html
    assert "Stripe" in html
    assert "Google Analytics" in html
    assert "Meta Pixel" in html
    assert "Set-Cookie" not in resp.headers


def test_public_pages_link_to_privacy_without_cookie_consent_friction(client):
    """Client-facing booking pages should expose privacy info but not block conversion with a consent popup."""
    c, client_db = client

    home = c.get("/")
    assert home.status_code == 200
    home_html = home.get_data(as_text=True)
    assert 'href="/privacy"' in home_html
    assert "Accept cookies" not in home_html
    assert "Reject cookies" not in home_html
    assert "Set-Cookie" not in home.headers

    slot_time, date, event_id = _first_slot((c, client_db))
    reserve = _reserve(c, slot_time, event_id, email="privacy-link@test.com")
    booking_id = reserve.get_json()["booking_id"]
    token = reserve.get_json()["confirmation_token"]

    payment = c.get(f"/payment?booking_id={booking_id}&token={token}")
    assert payment.status_code == 200
    payment_html = payment.get_data(as_text=True)
    assert 'href="/privacy"' in payment_html
    assert "Accept cookies" not in payment_html
    assert "Reject cookies" not in payment_html

    success = c.get(f"/success?booking_id={booking_id}")
    assert success.status_code == 200
    success_html = success.get_data(as_text=True)
    assert 'href="/privacy"' in success_html
    assert "Accept cookies" not in success_html
    assert "Reject cookies" not in success_html


def test_reserve_and_confirm_flow(client):
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    # Step 1: Reserve slot with the current drawer API contract
    reserve = _reserve(c, slot_time, event_id)
    assert reserve.status_code == 200
    data = reserve.get_json()
    assert data["success"] is True
    booking_id = data["booking_id"]
    token = data["confirmation_token"]
    assert booking_id is not None
    assert token

    # Step 2: Client clicks "I've Sent Payment"
    confirm = c.post("/confirm", json={
        "booking_id": booking_id,
        "confirmation_token": token,
    })
    assert confirm.status_code == 200
    confirm_data = confirm.get_json()
    assert confirm_data["success"] is True

    # Step 3: Check identity-safe booking-status API
    status = c.get(f"/booking-status?booking_id={booking_id}&token={token}").get_json()
    assert status["status"] == "pending_payment"
    assert status["confirmed"] is False

    # Step 4: Admin confirms
    admin = c.post("/admin/confirm", headers={"X-Admin-Key": "test-admin-key"}, json={
        "booking_id": booking_id,
        "paid_amount": 95.00,
    })
    assert admin.status_code == 200

    # Step 5: Booking should now be confirmed
    status2 = c.get(f"/booking-status?booking_id={booking_id}&token={token}").get_json()
    assert status2["status"] == "confirmed"
    assert status2["confirmed"] is True
    assert status2["paid"] is True
    assert status2["paid_amount"] == 95.00


def test_reserve_hides_slot(client):
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    # Reserve
    resp = _reserve(c, slot_time, event_id, email="hide@test.com")
    assert resp.status_code == 200

    # Slot should not be available for the same event
    slots = c.get(f"/slots/{date}?event_id={event_id}").get_json()
    slot_times = [s["time"] for s in slots.get("slots", [])]
    assert slot_time not in slot_times


def test_cancel_frees_slot(client):
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    # Reserve
    resp = _reserve(c, slot_time, event_id, email="cancel@test.com")
    assert resp.status_code == 200
    booking_id = resp.get_json()["booking_id"]

    # Cancel
    cancel = c.post("/admin/cancel", headers={"X-Admin-Key": "test-admin-key"}, json={"booking_id": booking_id})
    assert cancel.status_code == 200

    # Slot should be available again
    slots = c.get(f"/slots/{date}?event_id={event_id}").get_json()
    slot_times = [s["time"] for s in slots.get("slots", [])]
    assert slot_time in slot_times

    # Can reserve again — gets a new booking ID
    resp2 = _reserve(c, slot_time, event_id, email="cancel2@test.com")
    assert resp2.status_code == 200
    new_data = resp2.get_json()
    assert new_data["success"] is True
    assert new_data["booking_id"] != booking_id


def test_booking_status_not_found(client):
    c, _ = client
    resp = c.get("/booking-status?booking_id=99999&token=missing")
    assert resp.status_code == 404


def test_booking_status_rejects_missing_or_wrong_token(client):
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)
    resp = _reserve(c, slot_time, event_id, email="secure@test.com")
    booking_id = resp.get_json()["booking_id"]

    assert c.get(f"/booking-status?booking_id={booking_id}").status_code == 403
    assert c.get(f"/booking-status?booking_id={booking_id}&token=wrong").status_code == 403


def test_booking_status_shows_paid_amount(client):
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    # Full flow
    resp = _reserve(c, slot_time, event_id, email="amount@test.com")
    booking_id = resp.get_json()["booking_id"]
    token = resp.get_json()["confirmation_token"]

    c.post("/confirm", json={
        "booking_id": booking_id,
        "confirmation_token": token,
    })

    # Auto-confirm with specific amount (simulating e-Transfer)
    c.post("/admin/confirm", headers={"X-Admin-Key": "test-admin-key"}, json={
        "booking_id": booking_id,
        "paid_amount": 97.50,
    })

    status = c.get(f"/booking-status?booking_id={booking_id}&token={token}").get_json()
    assert status["paid_amount"] == 97.50
    assert status["status"] == "confirmed"


def test_admin_confirm_sends_telegram_confirmation_notification(client, monkeypatch):
    """Manual confirmation in admin should send the same confirmed signal to Telegram."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)
    sent_messages = []
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, reply_markup=None: sent_messages.append(message), raising=False)

    resp = _reserve(c, slot_time, event_id, name="Telegram Test", email="telegram-confirm@test.com")
    booking_id = resp.get_json()["booking_id"]
    token = resp.get_json()["confirmation_token"]
    c.post("/confirm", json={"booking_id": booking_id, "confirmation_token": token})

    admin = c.post("/admin/confirm", headers={"X-Admin-Key": "test-admin-key"}, json={
        "booking_id": booking_id,
        "paid_amount": 250.00,
    })

    assert admin.status_code == 200
    assert sent_messages, "admin confirmation must notify Telegram admins"
    message = sent_messages[-1]
    assert "Booking #" in message
    assert str(booking_id) in message
    assert "Telegram Test" in message
    assert "telegram-confirm@test.com" in message
    assert "$250.00" in message
    assert "Email confirmation sent to client" in message


def test_success_page_initially_renders_confirmed_state_for_confirmed_booking(client):
    """A confirmed e-Transfer booking should not show stale pending copy on page load."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)
    resp = _reserve(c, slot_time, event_id, email="success-confirmed@test.com")
    booking_id = resp.get_json()["booking_id"]
    token = resp.get_json()["confirmation_token"]

    c.post("/confirm", json={"booking_id": booking_id, "confirmation_token": token})
    c.post("/admin/confirm", headers={"X-Admin-Key": "test-admin-key"},
           json={"booking_id": booking_id, "paid_amount": 100.0})

    page = c.get(f"/success?booking_id={booking_id}")
    assert page.status_code == 200
    html = page.data.decode()
    assert 'id="main-title" data-i18n="confirmed_title"' in html
    assert 'id="detail-status" data-i18n="confirmed_status"' in html
    assert 'id="msg-pending" style="display:none"' in html
    assert 'id="msg-confirmed" style="display:block"' in html


def test_payment_page_polls_and_redirects_when_booking_auto_confirmed(client):
    """If client stays on payment page, auto-confirm should move them to success page."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)
    resp = _reserve(c, slot_time, event_id, email="payment-poll@test.com")
    booking_id = resp.get_json()["booking_id"]
    token = resp.get_json()["confirmation_token"]

    page = c.get(f"/payment?booking_id={booking_id}&token={token}")
    assert page.status_code == 200
    html = page.data.decode()
    assert "/booking-status?booking_id=" in html
    assert "redirectIfConfirmed" in html
    assert "/success?booking_id=" in html


# ── Phone validation tests (Fix: international numbers) ──────────────────────

def test_international_phone_accepted(client):
    """Ukrainian/Russian/other international numbers should be accepted."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    for phone in ["+380501234567", "+78252888500", "+447911123456", "+12125551234"]:
        resp = c.post("/reserve", json={
            "event_id": event_id,
            "time": slot_time,
            "name": "Test User",
            "email": f"intl_{phone[-4:]}@test.com",
            "phone": phone,
            "instagram": "@test",
        })
        data = resp.get_json()
        assert resp.status_code == 200, f"Phone {phone} rejected: {data}"
        assert data.get("success") is True, f"Phone {phone} failed: {data.get('error')}"
        # Free slot for next iteration
        if data.get("booking_id"):
            c.post("/admin/cancel", headers={"X-Admin-Key": "test-admin-key"},
                   json={"booking_id": data["booking_id"]})


def test_canadian_phone_formats_accepted(client):
    """Standard Canadian phone formats should still work."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    for phone in ["4035550001", "403-555-0002", "(403) 555-0003", "14035550004"]:
        resp = c.post("/reserve", json={
            "event_id": event_id,
            "time": slot_time,
            "name": "Test User",
            "email": f"ca_{phone[-4:]}@test.com",
            "phone": phone,
            "instagram": "@test",
        })
        data = resp.get_json()
        assert resp.status_code == 200, f"CA phone {phone} rejected: {data}"
        assert data.get("success") is True, f"CA phone {phone} failed: {data.get('error')}"
        if data.get("booking_id"):
            c.post("/admin/cancel", headers={"X-Admin-Key": "test-admin-key"},
                   json={"booking_id": data["booking_id"]})


def test_invalid_phone_rejected(client):
    """Garbage phone numbers should still fail."""
    c, _ = client
    for bad in ["123", "notaphone", "000-000-0000"]:
        resp = c.post("/reserve", json={
            "event_id": _first_event()["id"],
            "time": "99:99",  # bad time too, but validation fires first
            "name": "Test User",
            "email": "bad@test.com",
            "phone": bad,
            "instagram": "@test",
        })
        data = resp.get_json()
        assert data.get("success") is not True, f"Bad phone {bad!r} was incorrectly accepted"


# ── Slots API tests ───────────────────────────────────────────────────────────

def test_slots_response_includes_instagram_fallback(client):
    """Slots API response should always include instagram_url and instagram_handle."""
    c, _ = client
    ev = _first_event()
    resp = c.get(f"/slots/{ev['date']}?event_id={ev['id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "instagram_url" in data, "slots response missing instagram_url"
    assert "instagram_handle" in data, "slots response missing instagram_handle"


def test_health_endpoint_returns_json(client):
    """Admin health check should return valid JSON with 'healthy' key."""
    c, _ = client
    resp = c.get("/admin/health", headers={"X-Admin-Key": "test-admin-key"})
    assert resp.status_code in (200, 503)
    data = resp.get_json()
    assert "healthy" in data
    assert "checks" in data
    assert "database" in data["checks"]
    assert data["checks"]["database"]["ok"] is True


def test_public_events_exclude_past_and_hidden_sessions(client, monkeypatch):
    """The public /events API must not show expired sessions or draft/test events."""
    c, _ = client
    monkeypatch.setattr(booking_app, "EVENTS", [
        {
            "id": "past-event",
            "title": "Past Event",
            "date": "2026-05-03",
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "break_length": 10,
            "slot_interval": 30,
            "deposit": 200,
            "full_price": 400,
            "status": "active",
        },
        {
            "id": "hidden-test-event",
            "title": "Hidden Test Event",
            "date": "2099-06-07",
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "break_length": 10,
            "slot_interval": 30,
            "deposit": 1,
            "full_price": 1,
            "status": "active",
            "hidden": True,
        },
        {
            "id": "future-public-event",
            "title": "Future Public Event",
            "date": "2099-06-07",
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "break_length": 10,
            "slot_interval": 30,
            "deposit": 100,
            "status": "active",
            "photos": ["/images/future-public.jpg"],
        },
        {
            "id": "future-no-photo-test-event",
            "title": "Future No Photo Test Event",
            "date": "2099-06-07",
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "break_length": 10,
            "slot_interval": 30,
            "deposit": 1,
            "full_price": 1,
            "status": "active",
        },
    ], raising=False)

    resp = c.get("/events")

    assert resp.status_code == 200
    ids = [event["id"] for event in resp.get_json()["events"]]
    assert ids == ["future-public-event"]


def test_assistant_chat_fallback_works_without_openai_key(client, monkeypatch):
    """Assistant endpoint should still return a useful local answer without LLM keys."""
    c, _ = client
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    resp = c.post("/assistant/chat", json={
        "message": "How much is the deposit and what is included?",
        "lang": "en",
        "history": [],
    })

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["answer"]
    assert data["source"] in {"fallback", "openai", "zai", "openrouter"}


def test_assistant_chat_uses_openrouter_provider(client, monkeypatch):
    """Assistant can use OpenRouter as the cheap production LLM provider."""
    c, _ = client

    def fake_post(url, headers=None, json=None, timeout=None):
        class FakeResponse:
            status_code = 200
            text = "{}"

            def json(self):
                return {"choices": [{"message": {"content": "OpenRouter answer"}}]}

        assert "openrouter.ai" in url
        assert json["model"] == "google/gemini-2.5-flash-lite"
        return FakeResponse()

    monkeypatch.setenv("AI_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite")
    monkeypatch.setattr(assistant_engine.requests, "post", fake_post)

    resp = c.post("/assistant/chat", json={"message": "How much is the deposit?", "lang": "en", "history": []})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["source"] == "openrouter"
    assert data["answer"] == "OpenRouter answer"


def test_assistant_event_context_excludes_past_active_sessions():
    """Past active events must not be fed to the public assistant as bookable facts."""
    events = [
        {
            "title": "Past Blossom Mini Sessions",
            "date": "2026-05-03",
            "start_time": "10:00",
            "end_time": "16:00",
            "deposit": 200,
            "full_price": 400,
            "location": "Calgary",
            "included": ["old package"],
            "status": "active",
        },
        {
            "title": "Future Lilac Mini Sessions",
            "date": "2026-06-07",
            "start_time": "15:00",
            "end_time": "19:00",
            "deposit": 100,
            "full_price": 500,
            "location": "Calgary",
            "included": ["new package"],
            "status": "active",
            "photos": ["/images/future-lilac.jpg"],
        },
    ]

    lines = assistant_engine._event_lines(events, today="2026-05-10")

    assert len(lines) == 1
    assert "Future Lilac Mini Sessions" in lines[0]
    assert "Past Blossom" not in "\n".join(lines)


def test_assistant_slot_info_uses_requested_event(monkeypatch):
    """If a visitor asks about Lilac, the assistant must not link to a different earlier event."""
    monkeypatch.setenv("ASSISTANT_SITE_URL", "https://example.test")
    events = [
        {
            "id": "summer-minis-2099-06-01",
            "title": "Summer minis",
            "date": "2099-06-01",
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "slot_interval": 30,
            "deposit": 1,
            "full_price": 300,
            "status": "upcoming",
            "photos": ["/images/summer.jpg"],
        },
        {
            "id": "lilac-jun7",
            "title": "Lilac Mini Sessions",
            "date": "2099-06-07",
            "start_time": "15:00",
            "end_time": "16:00",
            "session_length": 20,
            "slot_interval": 30,
            "deposit": 100,
            "full_price": 500,
            "status": "active",
            "photos": ["/images/lilac.jpg"],
        },
    ]

    context = assistant_engine.build_context(
        "Какие свободные слоты есть на Lilac Mini Sessions?",
        events,
        {"photographer_email": "iryna@example.test"},
    )

    assert context["facts"]["booking_url"] == "https://example.test/?event=lilac-jun7"
    assert context["facts"]["deposit"] == 100
    assert "15:00" in context["facts"]["available_slots"]


def test_ics_uses_local_timezone(client):
    """Calendar .ics file should use TZID (local time) not Z (UTC) to prevent drift."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    resp = _reserve(c, slot_time, event_id, email="ics@test.com")
    booking_id = resp.get_json()["booking_id"]
    token = resp.get_json()["confirmation_token"]

    # Confirm the booking so ICS is accessible
    c.post("/confirm", json={"booking_id": booking_id, "confirmation_token": token})
    c.post("/admin/confirm", headers={"X-Admin-Key": "test-admin-key"},
           json={"booking_id": booking_id, "paid_amount": 100.0})

    ics_resp = c.get(f"/calendar-ics/{booking_id}?token={token}")
    assert ics_resp.status_code == 200
    ics_text = ics_resp.data.decode()

    # Must have VTIMEZONE block
    assert "BEGIN:VTIMEZONE" in ics_text, "ICS missing VTIMEZONE block"
    # DTSTART must use TZID, not end with Z (UTC)
    assert "DTSTART;TZID=" in ics_text, "DTSTART should use TZID= not bare UTC Z"
    assert "DTSTART:2" not in ics_text, "DTSTART must not be bare UTC format"
    assert f"UID:{booking_id}@book.pashynskaphoto.com" in ics_text
    assert "@pashynska.agency" not in ics_text


def test_success_page_confirmed_has_animated_calendar_cta(client):
    """Confirmed success page should make Google/Apple calendar actions obvious."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    resp = _reserve(c, slot_time, event_id, email="calendar-cta@test.com")
    booking_id = resp.get_json()["booking_id"]
    token = resp.get_json()["confirmation_token"]

    c.post("/confirm", json={"booking_id": booking_id, "confirmation_token": token})
    c.post("/admin/confirm", headers={"X-Admin-Key": "test-admin-key"},
           json={"booking_id": booking_id, "paid_amount": 100.0})

    page = c.get(f"/success?booking_id={booking_id}")
    assert page.status_code == 200
    html = page.data.decode()

    assert 'id="calendar-buttons" class="calendar-panel show"' in html
    assert 'id="google-cal-link"' in html
    assert 'id="apple-cal-link"' in html
    assert 'data-i18n="calendar_title"' in html
    assert 'setupCalendarLinks()' in html
    assert 'sessionLength: 20' in html
    assert "ctz=" in html
    assert "/calendar-ics/" in html


def test_success_page_calendar_cta_hidden_until_confirmation(client):
    """Pending bookings should not show the calendar CTA before payment confirmation."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)

    resp = _reserve(c, slot_time, event_id, email="calendar-pending@test.com")
    booking_id = resp.get_json()["booking_id"]

    page = c.get(f"/success?booking_id={booking_id}")
    assert page.status_code == 200
    html = page.data.decode()

    assert 'id="calendar-buttons" class="calendar-panel"' in html
    assert 'class="calendar-panel show"' not in html


def test_public_booking_pages_include_premium_design_layer(client):
    """Public booking pages should share the 21st-inspired premium CSS layer."""
    c, db_path = client_tuple = client
    stylesheet = '/static/css/booking-premium.css'

    landing = c.get('/')
    assert landing.status_code == 200
    assert stylesheet in landing.data.decode()

    slot_time, date, event_id = _first_slot(client_tuple)
    resp = _reserve(c, slot_time, event_id, email="premium-css@test.com")
    booking_id = resp.get_json()["booking_id"]
    token = resp.get_json()["confirmation_token"]

    payment = c.get(f'/payment?booking_id={booking_id}&token={token}')
    assert payment.status_code == 200
    assert stylesheet in payment.data.decode()

    success = c.get(f'/success?booking_id={booking_id}')
    assert success.status_code == 200
    assert stylesheet in success.data.decode()


def test_admin_confirm_telegram_notification_escapes_html(client, monkeypatch):
    """Manual confirmation Telegram HTML must escape client-provided fields."""
    c, db_path = client_tuple = client
    slot_time, date, event_id = _first_slot(client_tuple)
    sent_messages = []
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, reply_markup=None: sent_messages.append(message), raising=False)

    resp = _reserve(c, slot_time, event_id, name="Escape Test", email="escape@test.com")
    booking_id = resp.get_json()["booking_id"]
    token = resp.get_json()["confirmation_token"]

    conn = booking_app.db_conn()
    conn.execute("UPDATE bookings SET name=? WHERE id=?", ("<b>Eve & Co</b>", booking_id))
    conn.commit()
    conn.close()

    c.post("/confirm", json={"booking_id": booking_id, "confirmation_token": token})

    admin = c.post("/admin/confirm", headers={"X-Admin-Key": "test-admin-key"}, json={
        "booking_id": booking_id,
        "paid_amount": 95.00,
    })

    assert admin.status_code == 200
    message = sent_messages[-1]
    assert "&lt;b&gt;Eve &amp; Co&lt;/b&gt;" in message
    assert "<b>Eve & Co</b>" not in message
