"""Regression tests for two follow-up fixes after the 2026-05-17 audit:

1. _canonical_redirect now references CANONICAL_SITE_URL (single source of
   truth) instead of two literal strings — a domain change would have
   required updating four places. This test pins the behaviour.
2. _validate_gallery_url replaces the previous .strip()/startswith('http')
   check on Wfolio URLs. The old code let 'javascript:...', 'http://internal'
   and 'httpfoo://bar' through into outbound emails. New rules: https only,
   real host, length cap, soft host whitelist.
"""
import os
import tempfile
import pytest

import app as booking_app  # noqa: E402


# ── shared fixture ───────────────────────────────────────────────────────────

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
    # Skip real email send during /wfolio test.
    monkeypatch.setattr(
        booking_app, "_send_gallery_email", lambda *a, **k: True, raising=False
    )
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()

    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        yield c

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _admin_headers():
    return {"X-Admin-Key": "test-admin-key"}


def _make_booking():
    """Insert a minimal confirmed booking and return its id."""
    conn = booking_app.db_conn()
    conn.execute(
        """INSERT INTO bookings (date, time, name, email, phone, instagram,
                                 session_type, status, confirmed, paid, paid_amount)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', 1, 1, 95.0)""",
        ("2026-06-01", "10:00", "Test", "wfolio@example.com", "", "", "mini"),
    )
    bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return bid


# ── canonical redirect ───────────────────────────────────────────────────────

def test_canonical_constants_are_used_by_redirect_handler(client):
    """The _canonical_redirect handler's source must reference the constant,
    not the literal — otherwise we re-introduce drift between the two."""
    import inspect
    src = inspect.getsource(booking_app._canonical_redirect)
    # The handler should compose the URL from the constants, not strings.
    assert "CANONICAL_SITE_URL" in src, "redirect handler no longer uses CANONICAL_SITE_URL"
    # The Fly internal hostname should also live in a constant for symmetry.
    assert "_FLY_INTERNAL_HOST" in src or "iryna-booking.fly.dev" in src


def test_canonical_redirect_rewrites_alt_domain_to_canonical(client):
    """When the Cloudflare worker forwards a request from pashynska.agency,
    we 301 to the canonical book.pashynskaphoto.com host."""
    resp = client.get("/", headers={"Cf-Worker": "pashynska.agency"}, base_url="https://iryna-booking.fly.dev")
    assert resp.status_code == 301
    loc = resp.headers.get("Location", "")
    assert loc.startswith("https://book.pashynskaphoto.com"), f"unexpected redirect target: {loc}"


def test_canonical_redirect_left_alone_on_canonical_host(client):
    """Requests without the Cf-Worker header (direct fly.dev or canonical
    domain) must NOT redirect — that would create a loop."""
    resp = client.get("/")
    # / returns 200 (homepage), not a 301.
    assert resp.status_code == 200


# ── gallery URL validator ────────────────────────────────────────────────────

@pytest.mark.parametrize("good_url", [
    "https://wfolio.com/iryna/family-smith-2026",
    "https://gallery.wfolio.com/abc123",
    "https://pic-time.com/iryna-pashynska/family",
    "https://pixieset.com/iryna/maternity-2026",
    "https://smugmug.com/Iryna/family-2026",
    "https://book.pashynskaphoto.com/gallery/123",  # own subdomain
])
def test_validate_gallery_url_accepts_https_whitelist_hosts(good_url):
    ok, err = booking_app._validate_gallery_url(good_url)
    assert ok, f"{good_url!r} should pass: {err}"
    assert err == ""


@pytest.mark.parametrize("bad_url,reason_fragment", [
    ("",                                        "required"),
    ("http://wfolio.com/gallery",               "https"),     # plain http
    ("javascript:alert('xss')",                 "https"),     # JS injection
    ("mailto:hi@evil.com",                      "https"),     # mailto
    ("data:text/html,<script>x</script>",       "https"),     # data URL
    ("httpfoo://wfolio.com/x",                  "https"),     # typo
    ("https:///path-only",                      "host"),      # no host
    ("https://localhost",                       "host"),      # no dot in host
    ("https://wfolio.com@evil.example/gallery", "credentials"), # misleading userinfo
    ("https://" + "a" * 600,                    "long"),      # length cap
])
def test_validate_gallery_url_rejects_unsafe_inputs(bad_url, reason_fragment):
    ok, err = booking_app._validate_gallery_url(bad_url)
    assert not ok, f"{bad_url!r} should have been rejected"
    assert reason_fragment.lower() in err.lower(), f"unexpected error {err!r}"


def test_validate_gallery_url_accepts_non_whitelist_host_but_logs(caplog):
    """Iryna sometimes uses a new provider; we don't hard-block, just warn."""
    import logging
    caplog.set_level(logging.WARNING, logger=booking_app.log.name)
    ok, err = booking_app._validate_gallery_url("https://shootproof.com/gallery/abc")
    assert ok, err
    assert any("shootproof.com" in rec.message for rec in caplog.records), \
        "expected a [wfolio] warning for non-whitelisted host"


def test_admin_wfolio_endpoint_rejects_javascript_url(client):
    """End-to-end: the route must refuse javascript: even with valid auth."""
    bid = _make_booking()
    resp = client.post(
        f"/admin/booking/{bid}/wfolio",
        headers=_admin_headers(),
        json={"wfolio_url": "javascript:alert(1)"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "https" in (body.get("error", "")).lower()


def test_admin_wfolio_endpoint_persists_good_url(client):
    """End-to-end: a clean https Wfolio URL saves to the bookings row."""
    bid = _make_booking()
    good = "https://wfolio.com/iryna/family-smith-2026"
    resp = client.post(
        f"/admin/booking/{bid}/wfolio",
        headers=_admin_headers(),
        json={"wfolio_url": good},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json().get("success") is True

    conn = booking_app.db_conn()
    row = conn.execute("SELECT wfolio_url FROM bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    assert row["wfolio_url"] == good
