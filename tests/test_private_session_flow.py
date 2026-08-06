"""Private session = full deposit mechanics (2026-06-11 rework).

Pins down the new flow:
1. Admin creates a private session → booking stores deposit_amount=price,
   a /payment?booking_id&token URL is returned and emailed to the client.
2. already_paid=true → confirmed/paid, no email, no payment_url.
3. The /payment page renders for private bookings WITHOUT the countdown
   timer (no reserved_until → nothing to expire) and with the full price.
4. /confirm ("I've sent the e-Transfer") moves a private booking to
   pending_payment but keeps reserved_until NULL so the expiry sweep can
   never release the dedicated slot.
5. check_etransfer_v2.get_pending_bookings keeps private bookings matchable
   for weeks (payment link is emailed; clients pay days later).
6. /payment redirects already-confirmed bookings to /success and
   expired/cancelled ones to the landing page.
"""
import os
import tempfile

import pytest

import app as booking_app  # noqa: E402
import check_etransfer_v2 as etv2  # noqa: E402


@pytest.fixture()
def env(monkeypatch, tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(etv2, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    events_file = tmp_path / "events.yaml"
    events_file.write_text("events: []\n")
    monkeypatch.setattr(booking_app, "_EVENTS_PATH", str(events_file))
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(events_file), raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_client", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_admin", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(booking_app, "_emit_n8n_event", lambda *a, **k: None, raising=False)

    sent_emails = []

    def _fake_send_raw(to_email, client_name, subject, plain, html, **kw):
        sent_emails.append({"to": to_email, "subject": subject, "plain": plain})
        return True

    monkeypatch.setattr(booking_app, "_send_email_raw", _fake_send_raw)
    booking_app._rate_limits.clear()
    booking_app.init_db()
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as client:
        yield client, sent_emails
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _hdrs():
    return {"X-Admin-Key": "test-admin-key"}


def _create(client, **overrides):
    payload = {
        "date": "2026-09-20", "start_time": "14:00", "end_time": "15:30",
        "client_name": "Jane Privat", "email": "jane.privat@example.com",
        "price": "350", "photos": 25, "location": "Fish Creek Provincial Park",
        "send_email": True, "already_paid": False,
    }
    payload.update(overrides)
    return client.post("/admin/api/private-session", json=payload, headers=_hdrs())


# ── 1. Create + email ─────────────────────────────────────────────────────────

def test_create_unpaid_emails_payment_link(env):
    client, sent = env
    resp = _create(client)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["paid"] is False
    assert data["email_sent"] is True
    assert "/payment?booking_id=" in data["payment_url"]
    assert "token=" in data["payment_url"]

    conn = booking_app.db_conn()
    row = conn.execute(
        "SELECT deposit_amount, full_price, status, reserved_until, session_type "
        "FROM bookings WHERE id=?", (data["booking_id"],)).fetchone()
    conn.close()
    assert row["deposit_amount"] == 350.0   # drives /payment, Stripe and e-Transfer match
    assert row["full_price"] == 350.0
    assert row["status"] == "reserved"
    assert row["reserved_until"] is None    # dedicated slot: sweep must never touch it
    assert row["session_type"] == "private"

    assert len(sent) == 1
    assert sent[0]["to"] == "jane.privat@example.com"
    assert sent[0]["subject"] == "Your Individual Photoshoot — 2026-09-20 · Booking & Payment"
    assert "Your individual photoshoot with Iryna Pashynska is reserved!" in sent[0]["plain"]
    assert "private photo session" not in sent[0]["plain"].lower()
    assert data["payment_url"] in sent[0]["plain"]
    assert "350.00" in sent[0]["plain"]
    assert "Fish Creek Provincial Park" in sent[0]["plain"]
    assert "25 professionally edited photos" in sent[0]["plain"]


def test_already_paid_records_settled_session(env):
    client, sent = env
    resp = _create(client, already_paid=True, send_email=False)
    data = resp.get_json()
    assert data["paid"] is True
    assert data["payment_url"] is None
    assert data["email_sent"] is False
    assert sent == []

    conn = booking_app.db_conn()
    row = conn.execute("SELECT status, paid, paid_amount FROM bookings WHERE id=?",
                       (data["booking_id"],)).fetchone()
    conn.close()
    assert row["status"] == "confirmed" and row["paid"] == 1
    assert row["paid_amount"] == 350.0


def test_send_email_requires_email(env):
    client, _ = env
    resp = _create(client, email="", send_email=True)
    assert resp.status_code == 400


def test_legacy_semantics_without_new_flags(env):
    """Old callers (no send_email/already_paid keys): empty link => settled."""
    client, sent = env
    payload = {
        "date": "2026-09-21", "start_time": "10:00", "end_time": "11:00",
        "client_name": "Old Caller", "email": "old@example.com",
        "price": "200", "payment_link": "",
    }
    resp = client.post("/admin/api/private-session", json=payload, headers=_hdrs())
    data = resp.get_json()
    assert data["paid"] is True
    assert sent == []  # legacy path never emails


# ── 2. /payment page for private bookings ─────────────────────────────────────

def _payment_path(data):
    return data["payment_url"].split("pashynskaphoto.com")[-1].split("pashynska.agency")[-1]


def test_payment_page_private_no_timer_full_price(env):
    client, _ = env
    data = _create(client).get_json()
    resp = client.get(_payment_path(data))
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="timer-box"' not in body     # no countdown div for private sessions
    assert "350.00" in body                  # full price due today
    assert "TIMER_SECONDS = null" in body
    assert "Individual Photoshoot — Jane Privat" in body
    assert "25 professionally edited photos" in body
    assert "All original photos included" in body
    assert "Fish Creek Provincial Park" in body
    assert "15 edited photos + all originals" not in body


def test_payment_page_uses_exact_custom_photo_count_and_location(env):
    client, _ = env
    data = _create(
        client,
        photos=37,
        location="Reader Rock Garden, Calgary",
        email="exact-offer@example.com",
    ).get_json()

    resp = client.get(_payment_path(data))

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "37 professionally edited photos" in body
    assert "Reader Rock Garden, Calgary" in body
    assert "15 edited photos + all originals" not in body


def test_private_session_rejects_invalid_photo_count(env):
    client, _ = env

    response = _create(client, photos="not-a-number", email="bad-count@example.com")

    assert response.status_code == 400
    assert "должно быть числом" in response.get_json()["error"]


def test_payment_page_normal_booking_has_timer(env):
    client, _ = env
    conn = booking_app.db_conn()
    cur = conn.execute(
        "INSERT INTO bookings (date, time, name, email, phone, instagram, status, "
        "confirmation_token, reserved_until, created_at) "
        "VALUES ('2026-09-22','10:00','Timer Guy','t@example.com','','','reserved','tok-timer', "
        "datetime('now','+15 minutes'), datetime('now'))")
    conn.commit()
    bid = cur.lastrowid
    conn.close()
    resp = client.get(f"/payment?booking_id={bid}&token=tok-timer")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="timer-box"' in body
    assert "TIMER_SECONDS = null" not in body


def test_payment_redirects_confirmed_to_success(env):
    client, _ = env
    data = _create(client, already_paid=True, send_email=False).get_json()
    conn = booking_app.db_conn()
    token = conn.execute("SELECT confirmation_token FROM bookings WHERE id=?",
                         (data["booking_id"],)).fetchone()["confirmation_token"]
    conn.close()
    resp = client.get(f"/payment?booking_id={data['booking_id']}&token={token}")
    assert resp.status_code == 302
    assert "/success" in resp.headers["Location"]


def test_payment_redirects_cancelled_home(env):
    client, _ = env
    data = _create(client).get_json()
    conn = booking_app.db_conn()
    conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (data["booking_id"],))
    conn.commit()
    conn.close()
    resp = client.get(_payment_path(data))
    assert resp.status_code == 302
    assert "/success" not in resp.headers["Location"]


# ── 3. "I've sent the payment" keeps private slot protected ───────────────────

def test_confirm_private_keeps_null_reserved_until(env):
    client, _ = env
    data = _create(client).get_json()
    conn = booking_app.db_conn()
    token = conn.execute("SELECT confirmation_token FROM bookings WHERE id=?",
                         (data["booking_id"],)).fetchone()["confirmation_token"]
    conn.close()

    resp = client.post("/confirm", json={"booking_id": data["booking_id"],
                                         "confirmation_token": token})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    conn = booking_app.db_conn()
    row = conn.execute("SELECT status, reserved_until FROM bookings WHERE id=?",
                       (data["booking_id"],)).fetchone()
    conn.close()
    assert row["status"] == "pending_payment"
    assert row["reserved_until"] is None    # expiry sweep must skip it forever


# ── 4. e-Transfer watcher keeps private bookings matchable ────────────────────

def test_pending_bookings_include_private_reserved_and_pending(env):
    client, _ = env
    d1 = _create(client).get_json()
    d2 = _create(client, date="2026-09-23", start_time="09:00", end_time="10:00",
                 email="second@example.com").get_json()

    conn = booking_app.db_conn()
    token = conn.execute("SELECT confirmation_token FROM bookings WHERE id=?",
                         (d2["booking_id"],)).fetchone()["confirmation_token"]
    # Age both bookings well past the normal 90-minute mini-session window.
    conn.execute("UPDATE bookings SET created_at = datetime('now','-3 days')")
    conn.commit()
    conn.close()
    client.post("/confirm", json={"booking_id": d2["booking_id"],
                                  "confirmation_token": token})

    ids = {b["id"] for b in etv2.get_pending_bookings()}
    assert d1["booking_id"] in ids          # private + reserved, 3 days old
    assert d2["booking_id"] in ids          # private + pending_payment, 3 days old

    expected = etv2.get_expected_amount_for_booking(d1["booking_id"])
    assert expected == 350.0                # deposit_amount drives the match


def test_pending_bookings_still_exclude_stale_minis(env):
    client, _ = env
    conn = booking_app.db_conn()
    conn.execute(
        "INSERT INTO bookings (date, time, name, email, phone, instagram, status, "
        "session_type, confirmation_token, reserved_until, created_at) "
        "VALUES ('2026-09-25','10:00','Stale Mini','m@example.com','','','reserved', "
        "'blossom', 'tok-stale', datetime('now','-2 days'), datetime('now','-3 days'))")
    conn.commit()
    stale_id = conn.execute("SELECT id FROM bookings WHERE confirmation_token='tok-stale'").fetchone()["id"]
    conn.close()
    ids = {b["id"] for b in etv2.get_pending_bookings()}
    assert stale_id not in ids
