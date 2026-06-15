"""Regression tests for the reCAPTCHA gate on /reserve.

These guard against the 2026-05-16 incident where the production CSP omitted
www.google.com/www.gstatic.com from script-src — grecaptcha never loaded in
the browser, the client posted an empty recaptcha_token, and the server
hard-rejected with "Verification failed".

We cover three angles:

1. Server policy: with a secret configured and an empty token, /reserve must
   refuse. (Documents the intended security posture.)
2. Server policy: with the verifier monkey-patched to "pass" (simulating a
   real grecaptcha token that scored >0.5), /reserve succeeds — i.e. the
   reCAPTCHA hop is the *only* thing in the way for legitimate clients.
3. CSP regression: the live security headers must list every host required
   by grecaptcha (www.google.com + www.gstatic.com) in script-src, plus
   www.google.com in frame-src for the challenge iframe.
"""
import os
import tempfile
import pytest

import app as booking_app  # noqa: E402


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
        yield c

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _first_event():
    active = [e for e in booking_app.EVENTS if e.get("status") in ("active", "upcoming") and not e.get("hidden")]
    assert active, "No active events configured"
    return active[0]


def _first_slot(c):
    ev = _first_event()
    resp = c.get(f"/slots/{ev['date']}?event_id={ev['id']}")
    assert resp.status_code == 200
    slots = resp.get_json().get("slots", [])
    assert slots, f"No slots for event {ev['id']}"
    return slots[0]["time"], ev["date"], ev["id"]


def _payload(slot_time, event_id, **over):
    base = {
        "event_id": event_id,
        "time": slot_time,
        "name": "Test Client",
        "email": "recaptcha-test@example.com",
        "phone": "4035550000",
        "instagram": "@test",
        "terms_accepted": True,
        "agreement_name": "Test Client",
        "marketing_consent": "no",
    }
    base.update(over)
    return base


def test_reserve_accepts_when_recaptcha_secret_set_and_token_empty(client, monkeypatch):
    """When RECAPTCHA_SECRET_KEY is set but token is empty — soft-fallback allows."""

    monkeypatch.setattr(booking_app, "RECAPTCHA_SECRET_KEY", "fake-secret-for-test")
    slot_time, _, event_id = _first_slot(client)

    resp = client.post("/reserve", json=_payload(slot_time, event_id=event_id, recaptcha_token="", website=""))
    # 200 — soft-fallback allows empty tokens
    assert resp.status_code == 200


def test_reserve_succeeds_when_recaptcha_verifier_passes(client, monkeypatch):
    """If grecaptcha loads and the verifier passes, /reserve must succeed.

    This guards against regressions where reCAPTCHA accidentally becomes a
    hard wall (e.g. CSP blocks grecaptcha → empty token → forced rejection).
    The verifier is stubbed to simulate a legitimate token that scored >0.5.
    """
    monkeypatch.setattr(booking_app, "RECAPTCHA_SECRET_KEY", "fake-secret-for-test")
    monkeypatch.setattr(booking_app, "_verify_recaptcha", lambda token, ip=None: (True, ""))

    slot_time, _, event_id = _first_slot(client)
    resp = client.post(
        "/reserve",
        json=_payload(slot_time, event_id, recaptcha_token="stub-token-from-grecaptcha"),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is True
    assert body.get("booking_id") is not None


def test_csp_allows_recaptcha_script_and_frame_hosts(client):
    """CSP must list grecaptcha's hosts so the script can load in the browser.

    Without these, grecaptcha never initialises, the client posts an empty
    token, and the test above would have to be paired with a hard 400 — which
    is what bit us on 2026-05-16. Keep both checks together.
    """
    resp = client.get("/")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    assert csp, "CSP header missing on /"

    # script-src must whitelist Google reCAPTCHA hosts
    script_clause = next((p for p in csp.split(";") if p.strip().startswith("script-src")), "")
    assert "https://www.google.com" in script_clause, f"script-src missing www.google.com: {script_clause}"
    assert "https://www.gstatic.com" in script_clause, f"script-src missing www.gstatic.com: {script_clause}"

    # frame-src must whitelist www.google.com for the challenge iframe
    frame_clause = next((p for p in csp.split(";") if p.strip().startswith("frame-src")), "")
    assert "https://www.google.com" in frame_clause, f"frame-src missing www.google.com: {frame_clause}"


def test_healthz_returns_ok(client):
    """Fly.io HTTP health-checks need /healthz returning 200."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True


def test_meta_threads_oauth_and_compliance_callbacks_are_valid(client):
    """Meta Developer settings validate these URLs before accepting Threads OAuth config."""
    callback = client.get("/callback?code=test_code_123")
    assert callback.status_code == 200
    assert b"THREADS_CODE=test_code_123" in callback.data

    deauth = client.post("/meta/deauthorize", data={"signed_request": "dummy"})
    assert deauth.status_code == 200
    assert deauth.get_json()["ok"] is True

    deletion = client.post("/meta/data-deletion", data={"signed_request": "dummy"})
    assert deletion.status_code == 200
    deletion_body = deletion.get_json()
    assert deletion_body["url"] == "https://book.pashynskaphoto.com/meta/data-deletion/status"
    assert deletion_body["confirmation_code"]

    status = client.get("/meta/data-deletion/status")
    assert status.status_code == 200
    assert status.get_json()["status"] == "completed"
