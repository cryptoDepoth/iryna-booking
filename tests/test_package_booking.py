"""Package booking flow: landing packages -> deposit checkout (2026-07-09)."""

import sys
import urllib.request
import json
import hashlib
from types import SimpleNamespace

import pytest

import app as booking_app


@pytest.fixture
def client():
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        yield c


def test_packages_catalog_matches_canonical_pricing():
    """PRICING.md is the source of truth — guard the ladder."""
    p = booking_app.PACKAGES
    assert p["small-wedding"]["price"] == 640.0
    assert p["wedding-day-4h"]["price"] == 1280.0
    assert p["wedding-day-6h"]["price"] == 1920.0
    assert p["full-day-premium"]["price"] == 2600.0
    assert p["engagement"]["price"] == 320.0
    assert p["family-session"]["price"] == 340.0
    assert p["extended-family"]["price"] == 510.0
    assert p["maternity-session"]["price"] == 340.0
    assert p["bump-to-baby"]["price"] == 600.0
    for slug, pkg in p.items():
        assert pkg["deposit"] > 0 and pkg["deposit"] < pkg["price"], slug
        assert pkg["includes"], slug


@pytest.mark.parametrize("slug", [
    "small-wedding", "wedding-day-4h", "wedding-day-6h", "full-day-premium",
    "engagement", "family-session", "extended-family",
    "maternity-session", "bump-to-baby",
])
def test_package_page_renders(client, slug):
    resp = client.get(f"/book/package/{slug}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    pkg = booking_app.PACKAGES[slug]
    assert pkg["name"] in html
    assert f"${pkg['deposit']:.0f}" in html
    assert "deposit" in html.lower()
    assert "GST" in html


def test_unknown_package_is_404(client):
    assert client.get("/book/package/no-such-package").status_code == 404


def test_checkout_validates_before_stripe(client):
    # Unknown package -> 400 (validation happens before the Stripe key check)
    r = client.post("/book/package/checkout", json={"package": "nope", "name": "A", "email": "a@b.co"})
    assert r.status_code == 400
    # Missing/invalid email -> 400
    r = client.post("/book/package/checkout", json={"package": "small-wedding", "name": "A", "email": "not-an-email"})
    assert r.status_code == 400
    # Bad preferred date -> 400
    r = client.post("/book/package/checkout", json={
        "package": "small-wedding", "name": "A", "email": "a@b.co", "preferred_date": "July 12"})
    assert r.status_code == 400


def test_checkout_returns_503_without_stripe_key(client, monkeypatch):
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "")
    r = client.post("/book/package/checkout", json={
        "package": "small-wedding", "name": "Test Client", "email": "test@example.com"})
    assert r.status_code == 503
    assert "error" in r.get_json()


def test_checkout_with_attribution_returns_503_without_stripe_key(client, monkeypatch):
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "")
    r = client.post("/book/package/checkout", json={
        "package": "small-wedding",
        "name": "Test Client",
        "email": "test@example.com",
        "gclid": "google-click",
        "fbclid": "facebook-click",
        "fbp": "fb.1.123.abc",
        "fbc": "fb.1.123.def",
        "utm_source": "google",
        "utm_medium": "cpc",
        "utm_campaign": "weddings",
        "utm_content": "hero",
    })
    assert r.status_code == 503
    assert "error" in r.get_json()


def test_checkout_carries_persisted_attribution_and_match_data_to_stripe(client, monkeypatch):
    captured = {}

    class _Session:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id="cs_test_created", url="https://checkout.stripe.test/session")

    fake_stripe = SimpleNamespace(checkout=SimpleNamespace(Session=_Session), api_key=None)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "sk_test_fake")

    r = client.post(
        "/book/package/checkout",
        json={
            "package": "family-session",
            "name": "Olena Koval",
            "email": "olena@example.com",
            "phone": "403-555-0199",
            "utm_source": "meta",
            "utm_campaign": "summer_minis_2026",
            "utm_term": "calgary family",
            "fbclid": "fb-click",
            "fbc": "fb.1.123.fb-click",
            "landing_url": "https://book.pashynskaphoto.com/family?utm_source=meta",
            "referrer": "https://instagram.com/",
        },
        headers={"User-Agent": "pytest-browser", "X-Forwarded-For": "203.0.113.8, 10.0.0.1"},
    )
    assert r.status_code == 200
    metadata = captured["metadata"]
    assert metadata["utm_source"] == "meta"
    assert metadata["utm_campaign"] == "summer_minis_2026"
    assert metadata["utm_term"] == "calgary family"
    assert metadata["landing_url"].startswith("https://book.pashynskaphoto.com/family")
    assert metadata["client_ip_address"] == "203.0.113.8"
    assert metadata["client_user_agent"] == "pytest-browser"


def test_package_success_without_verified_session_does_not_fire_purchase(client, monkeypatch):
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "")
    resp = client.get("/payment/package/success?pkg=small-wedding")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "confirming your payment" in html.lower()
    assert "Small Wedding" in html
    assert "eventID" not in html
    assert "fbq('track', 'Purchase'" not in html


def test_package_success_rejects_unverified_stripe_session(client, monkeypatch):
    class _Session:
        @staticmethod
        def retrieve(_sid):
            raise RuntimeError("not found")

    fake_stripe = SimpleNamespace(checkout=SimpleNamespace(Session=_Session), api_key=None)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "sk_test_fake")

    resp = client.get("/payment/package/success?pkg=small-wedding&sid=cs_test_123")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "confirming your payment" in html.lower()
    assert "cs_test_123" not in html
    assert "fbq('track', 'Purchase'" not in html


def test_package_success_fires_deduped_purchase_only_for_paid_matching_session(client, monkeypatch):
    class _Session:
        @staticmethod
        def retrieve(sid):
            assert sid == "cs_test_123"
            return {
                "id": sid,
                "payment_status": "paid",
                "amount_total": 20000,
                "metadata": {
                    "payment_type": "package_deposit",
                    "package_slug": "small-wedding",
                },
            }

    fake_stripe = SimpleNamespace(checkout=SimpleNamespace(Session=_Session), api_key=None)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "sk_test_fake")

    html = client.get(
        "/payment/package/success?pkg=small-wedding&sid=cs_test_123"
    ).get_data(as_text=True)
    assert "Deposit received" in html
    assert '{eventID: "cs_test_123"}' in html
    assert "value: 200.0" in html


def test_package_success_rejects_slug_mismatch(client, monkeypatch):
    class _Session:
        @staticmethod
        def retrieve(sid):
            return {
                "id": sid,
                "payment_status": "paid",
                "amount_total": 10000,
                "metadata": {
                    "payment_type": "package_deposit",
                    "package_slug": "family-session",
                },
            }

    fake_stripe = SimpleNamespace(checkout=SimpleNamespace(Session=_Session), api_key=None)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "sk_test_fake")

    html = client.get(
        "/payment/package/success?pkg=small-wedding&sid=cs_test_123"
    ).get_data(as_text=True)
    assert "confirming your payment" in html.lower()
    assert "fbq('track', 'Purchase'" not in html


def test_send_package_capi_noops_without_token(monkeypatch):
    monkeypatch.setattr(booking_app, "META_CAPI_TOKEN", "")

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("urlopen should not be called without META_CAPI_TOKEN")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    assert booking_app._send_package_capi(
        {"id": "cs_test_123"},
        {"client_email": "client@example.com", "client_phone": "403-555-1212"},
        100.0,
    ) is False


def test_send_package_capi_uses_stripe_event_id_and_advanced_match_data(monkeypatch):
    monkeypatch.setattr(booking_app, "META_CAPI_TOKEN", "test-token")
    monkeypatch.setattr(booking_app, "META_PIXEL_ID", "pixel-123")
    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert booking_app._send_package_capi(
        {"id": "cs_test_123", "customer_details": {}},
        {
            "client_email": "Olena@Example.com",
            "client_phone": "403-555-0199",
            "client_name": "Olena Koval",
            "fbp": "fb.1.123.browser",
            "fbc": "fb.1.123.click",
            "client_ip_address": "203.0.113.8",
            "client_user_agent": "pytest-browser",
            "landing_url": "https://book.pashynskaphoto.com/family?utm_source=meta",
        },
        100.0,
    ) is True

    event = captured["payload"]["data"][0]
    assert event["event_id"] == "cs_test_123"
    assert event["event_source_url"].endswith("utm_source=meta")
    assert event["custom_data"] == {"currency": "CAD", "value": 100.0}
    user = event["user_data"]
    email_hash = hashlib.sha256(b"olena@example.com").hexdigest()
    assert user["em"] == [email_hash]
    assert user["external_id"] == [email_hash]
    assert user["ph"] == [hashlib.sha256(b"14035550199").hexdigest()]
    assert user["fn"] == [hashlib.sha256(b"olena").hexdigest()]
    assert user["ln"] == [hashlib.sha256(b"koval").hexdigest()]
    assert user["client_ip_address"] == "203.0.113.8"
    assert user["client_user_agent"] == "pytest-browser"


@pytest.mark.parametrize("path,slugs", [
    ("/wedding", ["small-wedding", "wedding-day-4h", "full-day-premium", "engagement"]),
    ("/family", ["family-session", "extended-family"]),
    ("/maternity", ["maternity-session", "bump-to-baby"]),
])
def test_landing_cards_link_to_package_checkout(client, path, slugs):
    html = client.get(path).get_data(as_text=True)
    for slug in slugs:
        assert f"/book/package/{slug}" in html, f"{path} missing link to {slug}"
    assert "deposit" in html.lower()
