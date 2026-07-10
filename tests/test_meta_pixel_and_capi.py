"""Meta Pixel single-source-of-truth + server-side Conversions API (Purchase).

Guards two conversion-critical invariants that had no test before:

1. Every funnel surface fires ONE pixel id (the configured `META_PIXEL_ID`),
   so the id can never silently drift between pages again. Regression context:
   index_v2.html shipped pixel 1806486840358828 while the ad account, analytics
   layer and ad-landing template all used 1335137335347797 — paid traffic landed
   on the main page and reported to a pixel the campaign never optimized against.

2. Purchase is actually delivered. The browser pixel cannot be trusted to fire
   Purchase for e-Transfer (confirmed asynchronously after the client leaves), so
   it is sent server-side via the Conversions API from the single confirmation
   hook `_record_booking_funnel_event(..., "booking_confirmed", ...)`.
"""
import hashlib
from pathlib import Path

import app as appmod
from app import app, _meta_capi_purchase, _record_booking_funnel_event

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
OLD_WRONG_PIXEL = "1806486840358828"


# ── 1. Pixel single source of truth ───────────────────────────────────────────

def test_homepage_renders_configured_pixel_and_not_the_old_wrong_one():
    html = app.test_client().get("/").get_data(as_text=True)
    assert appmod.META_PIXEL_ID in html
    assert "fbq('init', '%s')" % appmod.META_PIXEL_ID in html
    # The stale, mismatched pixel must never come back on the main landing page.
    assert OLD_WRONG_PIXEL not in html


def test_funnel_templates_use_the_shared_pixel_variable_not_a_hardcoded_id():
    for name in (
        "index_v2.html", "payment.html", "success.html", "base_landing.html",
        "events_landing.html",
    ):
        src = (TEMPLATES / name).read_text()
        assert "fbq('init', '{{ meta_pixel_id }}')" in src, name
        assert OLD_WRONG_PIXEL not in src, name


def test_session_browser_renders_pixel_and_view_content():
    html = app.test_client().get("/book?type=family").get_data(as_text=True)
    assert "fbq('init', '%s')" % appmod.META_PIXEL_ID in html
    assert "fbq('track', 'PageView')" in html
    assert "fbq('track', 'ViewContent'" in html
    assert 'content_category: "family"' in html


def test_payment_and_success_pages_carry_the_pixel_and_their_funnel_event():
    payment = (TEMPLATES / "payment.html").read_text()
    success = (TEMPLATES / "success.html").read_text()
    assert "InitiateCheckout" in payment
    assert "gtag('event', 'begin_checkout'" in payment
    assert "AW-610866068/DNSFCPCKxr8cEJSnpKMC" not in payment
    assert "'Purchase'" in success
    assert "AW-610866068/DNSFCPCKxr8cEJSnpKMC" in success
    # Browser Purchase must be deduped against the server CAPI event.
    assert "eventID" in success


def test_frontend_google_ads_purchase_is_not_fired_on_payment_view():
    homepage = (TEMPLATES / "index_v2.html").read_text()
    analytics = (TEMPLATES.parent / "static" / "js" / "analytics.js").read_text()

    assert "'payment_view':   { event: 'begin_checkout' }" in homepage
    assert "'payment_view':   { event: 'begin_checkout' }" in analytics
    assert "'booking_confirmed': { event: 'conversion', send_to: 'AW-610866068/DNSFCPCKxr8cEJSnpKMC' }" in homepage
    assert "'booking_confirmed': { event: 'conversion', send_to: 'AW-610866068/DNSFCPCKxr8cEJSnpKMC'" in analytics


# ── 2. Conversions API (server-side Purchase) ──────────────────────────────────

def _booking():
    return {
        "id": 4321,
        "event_id": "mountain_mini",
        "name": "Olena Koval",
        "email": "Olena@Example.com",
        "phone": "(403) 555-0199",
        "paid_amount": 95.0,
        "fbclid": "abc123",
        "landing_url": "https://book.pashynskaphoto.com/?event=mountain_mini",
    }


def test_capi_is_noop_without_token_and_makes_no_network_call(monkeypatch):
    monkeypatch.setattr(appmod, "META_CAPI_TOKEN", "")

    def _boom(*a, **k):
        raise AssertionError("CAPI must not call the network when token is unset")

    monkeypatch.setattr(appmod.requests, "post", _boom)
    assert _meta_capi_purchase(_booking(), value=95.0) is False


def test_capi_purchase_payload_is_correct_and_hashes_pii(monkeypatch):
    monkeypatch.setattr(appmod, "META_CAPI_TOKEN", "EAAtest-token")
    monkeypatch.setattr(appmod, "META_PIXEL_ID", "1335137335347797")
    monkeypatch.setattr(appmod, "_meta_visitor_match", lambda _visitor_id: {})

    captured = {}

    class _Resp:
        status_code = 200
        text = "ok"

    def _fake_post(url, params=None, json=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(appmod.requests, "post", _fake_post)

    assert _meta_capi_purchase(_booking(), value=95.0) is True

    assert "1335137335347797/events" in captured["url"]
    assert captured["params"]["access_token"] == "EAAtest-token"

    event = captured["json"]["data"][0]
    assert event["event_name"] == "Purchase"
    assert event["event_id"] == "purchase.4321"          # stable → dedup with browser
    assert event["action_source"] == "website"
    assert event["custom_data"]["currency"] == "CAD"
    assert event["custom_data"]["value"] == 95.0

    ud = event["user_data"]
    # Email is sha256 of the normalized (lowercased/trimmed) value — never raw.
    assert ud["em"] == [hashlib.sha256(b"olena@example.com").hexdigest()]
    assert "Olena@Example.com" not in str(captured["json"])
    # Phone normalized to digits + country code, then hashed.
    assert ud["ph"] == [hashlib.sha256(b"14035550199").hexdigest()]
    assert ud["fbc"].endswith(".abc123")


def test_capi_uses_first_party_visitor_match_data(monkeypatch):
    monkeypatch.setattr(appmod, "META_CAPI_TOKEN", "EAAtest-token")
    monkeypatch.setattr(appmod, "META_PIXEL_ID", "1335137335347797")
    monkeypatch.setattr(appmod, "_meta_visitor_match", lambda visitor_id: {
        "first_seen": "2026-07-01T12:00:00+00:00",
        "ip_address": "203.0.113.10",
        "user_agent": "pytest-browser",
    })
    captured = {}

    class _Resp:
        status_code = 200
        text = "ok"

    def _fake_post(url, params=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(appmod.requests, "post", _fake_post)
    booking = _booking()
    booking["visitor_id"] = "visitor-123"
    assert _meta_capi_purchase(booking, value=95.0) is True

    user = captured["json"]["data"][0]["user_data"]
    assert user["external_id"] == [hashlib.sha256(b"visitor-123").hexdigest()]
    assert user["client_ip_address"] == "203.0.113.10"
    assert user["client_user_agent"] == "pytest-browser"
    assert user["fbc"] == "fb.1.1782907200.abc123"


def test_confirmed_funnel_event_triggers_capi_purchase(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod, "_meta_capi_purchase",
                        lambda booking, value=None, **k: calls.append((booking, value)))

    _record_booking_funnel_event(_booking(), "booking_confirmed",
                                 {"source": "etransfer_auto", "paid_amount": 95.0})
    assert len(calls) == 1
    assert calls[0][0]["id"] == 4321
    assert calls[0][1] == 95.0


def test_non_confirmed_funnel_events_do_not_fire_purchase(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod, "_meta_capi_purchase",
                        lambda *a, **k: calls.append(a))

    for ev in ("reserve_attempt", "payment_view", "drawer_open"):
        _record_booking_funnel_event(_booking(), ev, {})
    assert calls == []
