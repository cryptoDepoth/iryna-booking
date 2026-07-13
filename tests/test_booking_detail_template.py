"""Regression tests for the /admin/booking/<id> page rebuild.

Before this pass, the route returned an inline 3-button HTML stub even though
templates/booking_detail.html existed on disk — Iryna saw a broken-looking
page with just the client name and three unstyled buttons. The route now
renders the full template. These tests pin every section we expect to find,
plus the visual coherence with admin.html / admin_clients.html.
"""
import os
import json
import sqlite3
import tempfile

import pytest

import app as booking_app  # noqa: E402


@pytest.fixture()
def admin_client(monkeypatch):
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
    monkeypatch.setattr(booking_app, "_send_email_with_attachment", lambda *a, **k: True, raising=False)
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


def _insert_booking(**over):
    """Insert a confirmed booking and return its id."""
    fields = {
        "date": "2026-06-07", "time": "15:00",
        "name": "Juliana Delavin", "email": "j@example.com",
        "phone": "4035550123", "instagram": "juliana",
        "session_type": "mini", "status": "confirmed",
        "confirmed": 1, "paid": 1, "paid_amount": 250.0,
        "event_id": "lilac-jun7", "deposit_amount": 250.0,
    }
    fields.update(over)
    cols = ",".join(fields.keys())
    placeholders = ",".join("?" for _ in fields)
    conn = booking_app.db_conn()
    conn.execute(f"INSERT INTO bookings ({cols}) VALUES ({placeholders})", list(fields.values()))
    bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return bid


def _hdrs():
    return {"X-Admin-Key": "test-admin-key"}


def test_booking_detail_renders_template_not_stub(admin_client):
    """The route used to return a 4-line inline HTML stub. Now it must
    render the full booking_detail.html template — i.e. the response must
    contain identifying CSS class names from the template."""
    bid = _insert_booking()
    resp = admin_client.get(f"/admin/booking/{bid}", headers=_hdrs())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Hallmarks of the full template, not the stub.
    assert "booking-hero" in body
    assert "card-title" in body
    assert "Pashynska Admin" in body  # shared administration shell brand
    assert "Administration navigation" in body
    assert len(body) > 5000, "stub was ~400 chars; full template is ~15k"


def test_booking_detail_has_all_six_action_sections(admin_client):
    """Premium-UX rebuild: the page must surface every operator action —
    Client, Session, Payment, Invoice, Gallery, Review, Reschedule —
    each in its own card."""
    bid = _insert_booking()
    body = admin_client.get(f"/admin/booking/{bid}", headers=_hdrs()).get_data(as_text=True)
    for marker in ["👤 Client", "📸 Session", "💰 Payment",
                   "📄 Invoice", "🖼️ Gallery", "⭐", "📅 Reschedule"]:
        assert marker in body, f"missing section: {marker!r}"


def test_booking_detail_uses_premium_design_system(admin_client):
    """Visual coherence: the page must use the same palette + typography
    as admin.html / admin_clients.html (light theme, orange accent),
    not the old beige/brown one."""
    bid = _insert_booking()
    body = admin_client.get(f"/admin/booking/{bid}", headers=_hdrs()).get_data(as_text=True)
    # Light-theme background + Inter font + orange accent are the markers.
    assert "--bg: #f0f2f5" in body, "light theme background missing"
    assert "Inter" in body
    assert "#f97316" in body, "orange accent missing"
    # Old beige theme markers MUST be gone.
    assert "#fdf2ed" not in body, "stale beige background still present"
    assert "Cormorant Garamond" not in body, "stale serif heading still present"


def test_booking_detail_payment_math_is_correct(admin_client):
    """Payment card should show deposit, full price, paid and remaining
    balance computed from the event + booking row. With deposit 250 and
    full_price 500 (lilac-jun7) and paid 250, remaining = $250."""
    bid = _insert_booking()
    body = admin_client.get(f"/admin/booking/{bid}", headers=_hdrs()).get_data(as_text=True)
    assert "$250.00" in body  # deposit + paid
    assert "$500.00" in body  # full_price for lilac-jun7


def test_booking_detail_renders_addons_agreement_and_questionnaire_safely(admin_client):
    """Admin detail should show new booking snapshot fields without raw HTML."""
    bid = _insert_booking(
        full_price=300.0,
        deposit_amount=100.0,
        paid_amount=100.0,
        addons_total=50.0,
        selected_addons_json=json.dumps([
            {
                "id": "extra-10-edited-images",
                "title": "<script>Bad Addon</script>10 Extra Edited Images",
                "description": "Safe description",
                "price": 50.0,
            }
        ]),
        marketing_consent="no",
        agreement_name='<img src=x onerror=alert(1)> Client Name',
        agreement_accepted_at="2026-08-01T10:05:00",
        terms_version="booking-terms-v1",
        questionnaire_answers_json=json.dumps({
            "session_goals": "<script>alert(1)</script>Natural photos",
        }),
    )

    body = admin_client.get(f"/admin/booking/{bid}", headers=_hdrs()).get_data(as_text=True)

    assert "Selected add-ons" in body
    assert "Add-ons total" in body
    assert "10 Extra Edited Images" in body
    assert "Marketing consent" in body
    assert "Keep gallery private" in body
    assert "Electronic signature" in body
    assert "Questionnaire answers" in body
    assert "Natural photos" in body
    assert "<script>Bad Addon</script>" not in body
    assert "<script>alert(1)</script>" not in body
    assert '<img src=x onerror=alert(1)>' not in body


def test_booking_detail_payment_marks_zero_balance_paid(admin_client):
    """A booking with paid_amount >= full_price shows $0.00 remaining
    with the paid (green) class, not the unpaid (red) class."""
    bid = _insert_booking(paid_amount=500.0)
    body = admin_client.get(f"/admin/booking/{bid}", headers=_hdrs()).get_data(as_text=True)
    assert 'class="val paid"' in body


def test_booking_detail_has_exactly_one_h1(admin_client):
    """The booking name is the single h1 — no duplicates."""
    import re
    bid = _insert_booking()
    body = admin_client.get(f"/admin/booking/{bid}", headers=_hdrs()).get_data(as_text=True)
    h1_open = len(re.findall(r"<h1[\s/>]", body, flags=re.IGNORECASE))
    assert h1_open == 1, f"expected exactly 1 <h1>, got {h1_open}"


def test_booking_detail_back_link_points_to_clients_db(admin_client):
    """Iryna usually arrives at the booking from /admin/clients — the
    back link should send her there, not into the dashboard."""
    bid = _insert_booking()
    body = admin_client.get(f"/admin/booking/{bid}", headers=_hdrs()).get_data(as_text=True)
    assert 'href="/admin/clients"' in body


def test_booking_detail_unauth_user_redirects_to_login(admin_client, monkeypatch):
    """admin_required redirects HTML pages to /admin/login with next=…
    so the operator returns here after re-auth."""
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key", raising=False)
    bid = _insert_booking()
    resp = admin_client.get(f"/admin/booking/{bid}")
    assert resp.status_code in (302, 303)
    loc = resp.headers.get("Location", "")
    assert "/admin/login" in loc
    assert f"/admin/booking/{bid}" in loc or f"%2Fadmin%2Fbooking%2F{bid}" in loc


# ── End-to-end button wiring ─────────────────────────────────────────────────
# These guard against the 2026-05-21 incident where the "⬇ Download PDF"
# button was rendered as a plain <a href> but the route was POST-only — the
# browser hit it with GET and got HTTP 405 "Method Not Allowed" in Iryna's
# face. Every action button on the detail page now has a passing E2E test.

def test_invoice_download_button_works_via_get(admin_client):
    """The Download PDF link in the template uses a plain <a href>, which
    is a GET request. Route must accept GET (was POST-only)."""
    bid = _insert_booking()
    resp = admin_client.get(f"/admin/booking/{bid}/invoice", headers=_hdrs())
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.mimetype == "application/pdf"
    assert resp.data[:4] == b"%PDF"
    # Content-Disposition should be present so the browser saves it.
    assert "attachment" in resp.headers.get("Content-Disposition", "").lower()


def test_invoice_pdf_still_works_via_post_legacy(admin_client):
    """Don't regress the old POST path that the previous test suite asserts."""
    bid = _insert_booking()
    resp = admin_client.post(f"/admin/booking/{bid}/invoice", headers=_hdrs())
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"


def test_email_to_client_button_sends_invoice_email(admin_client, monkeypatch):
    """'📧 Email to client' wires up to POST /send-invoice and must call
    _send_email_with_attachment with the PDF."""
    captured = {}
    def stub(*a, **k):
        captured["called"] = True
        captured["args"] = a
        return True
    monkeypatch.setattr(booking_app, "_send_email_with_attachment", stub, raising=False)
    bid = _insert_booking()
    resp = admin_client.post(f"/admin/booking/{bid}/send-invoice", headers=_hdrs(), json={})
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    assert captured.get("called") is True


def test_gallery_button_saves_url_and_emails(admin_client, monkeypatch):
    """'Save & email' button hits POST /wfolio with a valid https URL.
    Must persist + send + return success."""
    sent = {}
    monkeypatch.setattr(
        booking_app, "_send_gallery_email",
        lambda *a, **k: sent.setdefault("ok", True), raising=False
    )
    bid = _insert_booking()
    resp = admin_client.post(
        f"/admin/booking/{bid}/wfolio",
        headers=_hdrs(),
        json={"wfolio_url": "https://wfolio.com/iryna/family-test-2026"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body.get("wfolio_url") == "https://wfolio.com/iryna/family-test-2026"
    assert sent.get("ok") is True


def test_review_request_button_sends_and_stamps(admin_client, monkeypatch):
    """'Send review request' button → POST /send-review → email goes out
    and the booking row gets review_email_sent stamped."""
    captured = {}
    def fake_review_email(*args, **kwargs):
        captured["html"] = args[4]
        return True
    monkeypatch.setattr(booking_app, "_send_email_raw", fake_review_email, raising=False)
    bid = _insert_booking()
    resp = admin_client.post(f"/admin/booking/{bid}/send-review", headers=_hdrs(), json={})
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    assert "https://review.pashynskaphoto.com" in captured["html"]
    assert "Leave a Google Review" in captured["html"]
    # The DB row must now carry the timestamp.
    conn = booking_app.db_conn()
    row = conn.execute("SELECT review_email_sent FROM bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    assert row["review_email_sent"], "review_email_sent timestamp not written"


def test_reschedule_link_target_is_reachable(admin_client):
    """The Reschedule button is an <a href> to /admin?reschedule_id=… —
    that admin index page must load (used to 404 if a route changed)."""
    bid = _insert_booking()
    resp = admin_client.get(f"/admin?reschedule_id={bid}", headers=_hdrs())
    # Either renders directly or redirects to a sane place; never 4xx/5xx.
    assert resp.status_code < 400, f"reschedule landing returned {resp.status_code}"
