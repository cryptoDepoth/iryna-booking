"""Regression tests for low-risk conversion and safety improvements."""

import pytest
import app as booking_app
from app import app


def test_landing_pages_show_real_testimonials():
    app.config["TESTING"] = True
    with app.test_client() as client:
        for path in ("/wedding", "/family", "/maternity"):
            response = client.get(path)
            assert response.status_code == 200
            html = response.data.decode("utf-8")
            assert "What Calgary clients say" in html
            assert "testimonial-card" in html
            assert "Calgary" in html


def test_events_api_is_not_indexed_by_search_engines():
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/events")
        assert response.status_code == 200
        assert response.headers.get("X-Robots-Tag") == "noindex, nofollow"


# ── Mountain Mini conversion callout ────────────────────────────────────────

_MOUNTAIN_MINI_EVENT = {
    "id": "mountain_mini",
    "title": "Mountain Mini Sessions",
    "subtitle": "Golden hour portraits near Calgary foothills",
    "date": "2026-07-11",
    "start_time": "17:00",
    "end_time": "20:00",
    "session_length": 30,
    "break_length": 10,
    "slot_interval": 40,
    "deposit": 100.0,
    "full_price": 250.0,
    "location": "near Calgary — foothills",
    "session_type": "mini",
    "featured": True,
    "status": "active",
    "included": [
        "30 minutes among the foothills",
        "20 retouched photos",
        "All original photos included",
        "Short video included",
    ],
    "photos": ["/images/placeholder.jpg"],
}


@pytest.fixture()
def client_with_mountain_mini(monkeypatch):
    """Test client where EVENTS contains the mountain_mini event."""
    monkeypatch.setattr(booking_app, "EVENTS", [_MOUNTAIN_MINI_EVENT])
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        yield c


def test_mountain_mini_deeplink_returns_200(client_with_mountain_mini):
    """?event=mountain_mini must resolve to a page, not 404."""
    r = client_with_mountain_mini.get("/?event=mountain_mini")
    assert r.status_code == 200


def test_mountain_mini_page_shows_price(client_with_mountain_mini):
    """Page must display the session price above the fold (exact $ amount, not a stray '250')."""
    r = client_with_mountain_mini.get("/?event=mountain_mini")
    html = r.data.decode("utf-8")
    assert "$250" in html, "Price not found in mountain mini landing page"
    assert "+ GST" in html


def test_mountain_mini_page_shows_session_duration(client_with_mountain_mini):
    """Page must display session duration."""
    r = client_with_mountain_mini.get("/?event=mountain_mini")
    html = r.data.decode("utf-8")
    assert "30 min" in html, "Session duration not found"


def test_mountain_mini_page_shows_human_readable_date(client_with_mountain_mini):
    """Callout must show 'July 11, 2026', not the raw ISO date."""
    r = client_with_mountain_mini.get("/?event=mountain_mini")
    html = r.data.decode("utf-8")
    assert "July 11, 2026" in html


def test_callout_hidden_for_event_without_price(monkeypatch):
    """Events with no price must not render a broken offer block."""
    no_price_event = {k: v for k, v in _MOUNTAIN_MINI_EVENT.items()
                      if k not in ("full_price", "deposit")}
    monkeypatch.setattr(booking_app, "EVENTS", [no_price_event])
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        r = c.get("/?event=mountain_mini")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert 'id="directCallout"' not in html


def test_mountain_mini_page_shows_included_deliverables(client_with_mountain_mini):
    """Callout block must list what clients get."""
    r = client_with_mountain_mini.get("/?event=mountain_mini")
    html = r.data.decode("utf-8")
    assert "20 retouched photos" in html
    assert "All original photos included" in html


def test_mountain_mini_page_shows_choose_a_time_cta(client_with_mountain_mini):
    """Above-fold CTA must be visible and use action-oriented language."""
    r = client_with_mountain_mini.get("/?event=mountain_mini")
    html = r.data.decode("utf-8")
    assert "Choose a time" in html


def test_mountain_mini_page_shows_limited_spots_urgency(client_with_mountain_mini):
    """Callout must mention limited availability."""
    r = client_with_mountain_mini.get("/?event=mountain_mini")
    html = r.data.decode("utf-8")
    assert "limited spots" in html.lower() or "Reserve your spot" in html


def test_mountain_mini_callout_block_present(client_with_mountain_mini):
    """The direct-callout div must be rendered server-side."""
    r = client_with_mountain_mini.get("/?event=mountain_mini")
    html = r.data.decode("utf-8")
    assert 'id="directCallout"' in html
    assert "Mountain Mini Sessions" in html


# ── Payment / reservation clarity ───────────────────────────────────────────

def test_booking_page_has_payment_clarity_copy():
    """The booking page must include copy explaining the two-step reserve-then-pay flow."""
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        r = c.get("/")
        html = r.data.decode("utf-8")
        # The pay_note i18n copy must be present in the page source
        assert "spot is held temporarily" in html or "complete the payment" in html


def test_booking_page_has_pending_not_confirmed_copy():
    """Page source must contain the 'reserved but not confirmed' pending-state copy."""
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        r = c.get("/")
        html = r.data.decode("utf-8")
        assert "reserved but not confirmed" in html.lower() or "awaiting payment confirmation" in html.lower()


def test_booking_page_has_instagram_contact_hint():
    """Booking page must include Instagram contact link for questions."""
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        r = c.get("/")
        html = r.data.decode("utf-8")
        assert "pashynska.photo" in html
        assert "instagram.com" in html
