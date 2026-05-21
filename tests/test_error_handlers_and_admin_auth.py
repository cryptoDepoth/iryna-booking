"""Regression tests for the production-hardening pass:

1. Global error handlers — Flask must never leak its default debug 500 HTML
   to a real visitor, and a JSON consumer must get JSON back even on errors.
2. admin_required redirect policy — HTML admin pages bounce to /admin/login
   with a `next` query so the operator returns to where they were; API
   endpoints stay 401 JSON so XHR can detect and redirect itself.
3. Portfolio URL constant — wired into every template via context processor.
"""
import os
import tempfile
import pytest

import app as booking_app  # noqa: E402


# Register synthetic "boom" routes at import time, before Flask has handled
# any request. Attempting `app.route` after the first request raises an
# AssertionError in modern Flask.
def _register_boom_routes():
    rules = {r.rule for r in booking_app.app.url_map.iter_rules()}

    if "/__boom_html__" not in rules:
        def _boom_html():
            raise RuntimeError("synthetic test failure")
        booking_app.app.add_url_rule("/__boom_html__", "_boom_html", _boom_html)

    if "/admin/api/__boom_json__" not in rules:
        def _boom_json():
            raise RuntimeError("synthetic test failure")
        booking_app.app.add_url_rule(
            "/admin/api/__boom_json__", "_boom_json", _boom_json
        )


_register_boom_routes()


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kw: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kw: None, raising=False)
    monkeypatch.setattr(booking_app, "send_confirmation_email", lambda booking_id: True, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()
    # Disable PROPAGATE so error handlers actually fire (Flask's TESTING mode
    # otherwise re-raises). We want to assert on the response Flask returns
    # to a real client, not the raised exception.
    booking_app.app.config["TESTING"] = False
    booking_app.app.config["PROPAGATE_EXCEPTIONS"] = False
    with booking_app.app.test_client() as c:
        yield c
    try:
        os.unlink(db_path)
    except OSError:
        pass


# ── 404 handler ──────────────────────────────────────────────────────────────

def test_404_html_returns_friendly_page_not_stock_flask(client):
    """A bare 404 should be a styled card with a 'Back to bookings' link, not
    the Werkzeug default plain-text page."""
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert "Page not found" in body
    assert "Back to bookings" in body
    # No traceback / Werkzeug default tells.
    assert "werkzeug" not in body.lower()


def test_404_on_api_path_returns_json(client):
    """/api/* and /admin/api/* are consumed by XHR — a JSON 404 keeps the
    frontend's `await res.json()` from blowing up."""
    resp = client.get("/admin/api/does-not-exist", headers={"X-Admin-Key": "test-admin-key"})
    assert resp.status_code == 404
    assert resp.mimetype == "application/json"
    body = resp.get_json()
    assert body["error"] == "not_found"
    assert body["path"] == "/admin/api/does-not-exist"


def test_404_respects_accept_json_header(client):
    """A client that sends only `Accept: application/json` is signalling it
    won't be able to render HTML; serve JSON."""
    resp = client.get("/nope", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    assert resp.mimetype == "application/json"


# ── 500 / uncaught exception handler ─────────────────────────────────────────

def test_uncaught_exception_returns_safe_html_not_traceback(client):
    """If something inside a route raises, the visitor must see the friendly
    card — never the Werkzeug debug page (which would leak code + env)."""
    resp = client.get("/__boom_html__")
    assert resp.status_code == 500
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert "Something went wrong" in body
    # Crucial: no traceback / no leaked exception text.
    assert "synthetic test failure" not in body
    assert "Traceback" not in body


def test_uncaught_exception_on_api_path_returns_json(client):
    """Same idea for an API route — JSON shape, no traceback. We register
    the synthetic route at the top of this module (before app handles its
    first request); it's not behind admin_required, mirroring how an
    internal handler could raise even after auth passes."""
    resp = client.get("/admin/api/__boom_json__")
    assert resp.status_code == 500
    assert resp.mimetype == "application/json"
    body = resp.get_json()
    assert body["error"] == "server_error"
    assert "synthetic test failure" not in str(body)


# ── admin_required redirect policy ───────────────────────────────────────────

def test_unauthorised_admin_html_page_redirects_to_login_with_next(client, monkeypatch):
    """When the session expires, an operator hitting /admin/clients should
    see the login page, not raw 401 JSON."""
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "")  # require browser session
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key", raising=False)
    resp = client.get("/admin/clients")  # no auth
    assert resp.status_code in (302, 303)
    loc = resp.headers.get("Location", "")
    assert "/admin/login" in loc, f"unexpected redirect: {loc}"
    # `next=` carries the original path so the operator returns there after
    # logging back in. Flask's url_for doesn't %-encode slashes, so accept
    # either raw or url-encoded form.
    assert "next=" in loc
    assert "/admin/clients" in loc or "%2Fadmin%2Fclients" in loc


def test_unauthorised_admin_api_returns_json_401(client, monkeypatch):
    """XHR endpoints stay JSON so the frontend can detect the auth failure
    and bounce to /admin/login itself."""
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key", raising=False)
    resp = client.get("/admin/api/clients")  # no auth
    assert resp.status_code == 401
    assert resp.mimetype == "application/json"
    assert resp.get_json() == {"error": "Unauthorized"}


# ── portfolio URL wiring ─────────────────────────────────────────────────────

def test_portfolio_url_rendered_in_home(client):
    """The home page should link to the portfolio (apex pashynskaphoto.com),
    not to the booking subdomain or anything else."""
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert booking_app.PORTFOLIO_URL in body, "PORTFOLIO_URL not injected into home"
    # Sanity: it's the apex, not the booking subdomain.
    assert booking_app.PORTFOLIO_URL.startswith("https://pashynskaphoto.com")
    assert booking_app.CANONICAL_SITE_HOST != "pashynskaphoto.com"  # they're distinct
