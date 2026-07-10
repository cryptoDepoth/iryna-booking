"""Package booking flow: landing packages -> deposit checkout (2026-07-09)."""

import urllib.request

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


def test_package_success_page_renders(client):
    resp = client.get("/payment/package/success?pkg=small-wedding")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Deposit received" in html
    assert "Small Wedding" in html
    assert "eventID" not in html


def test_package_success_page_uses_sid_as_purchase_event_id(client):
    resp = client.get("/payment/package/success?pkg=small-wedding&sid=cs_test_123")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "cs_test_123" in html
    assert '{eventID: "cs_test_123"}' in html


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
