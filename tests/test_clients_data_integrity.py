"""Regression tests for /admin/clients data shape.

The 2026-05-16/17 audit caught two ways the clients API and UI got out of sync
with the schema, both invisible until you opened the page:

1. tags TEXT DEFAULT '[]' (JSON-looking) vs the rest of the codebase treating
   tags as CSV. Every new client got a fake "[]" chip and tag filters silently
   missed them.
2. first_booking_at / last_booking_at were added as columns but sync_client
   never wrote to them, so the sort/display dates were always NULL.

These tests pin both fixes so a future backup-restore doesn't quietly bring
the bugs back.
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
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(booking_app, "send_confirmation_email", lambda booking_id: True, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()

    with booking_app.app.test_client() as c:
        yield c

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _insert_booking(email, date_str, time_str, **over):
    """Insert a confirmed booking directly so we can test sync_client wiring."""
    conn = booking_app.db_conn()
    fields = {
        "date": date_str,
        "time": time_str,
        "name": over.get("name", "Test " + email[:3]),
        "email": email.lower(),
        "phone": over.get("phone", ""),
        "instagram": over.get("instagram", ""),
        "session_type": over.get("session_type", "mini"),
        "status": over.get("status", "confirmed"),
        "confirmed": over.get("confirmed", 1),
        "paid": over.get("paid", 1),
        "paid_amount": over.get("paid_amount", 95.0),
    }
    cols = ",".join(fields.keys())
    placeholders = ",".join("?" for _ in fields)
    conn.execute(f"INSERT INTO bookings ({cols}) VALUES ({placeholders})", list(fields.values()))
    conn.commit()
    conn.close()


def _admin_headers():
    return {"X-Admin-Key": "test-admin-key"}


def test_new_client_tags_default_to_empty_csv_not_brackets(client):
    """A freshly-created client must have tags='' (empty CSV), never '[]'.

    `[]` is a literal string from a legacy DEFAULT '[]' migration; if the
    column drifts back to that default, the /admin/clients UI renders a
    fake `tag-[]` chip per row and the LIKE-based tag filter never matches.
    """
    booking_app.sync_client("legacy-default@example.com", "Legacy Default")
    conn = booking_app.db_conn()
    row = conn.execute(
        "SELECT tags FROM clients WHERE email='legacy-default@example.com'"
    ).fetchone()
    conn.close()
    assert row is not None, "sync_client should have inserted the row"
    # NOT the literal "[]"; empty string is the contract.
    assert row["tags"] == "", f"expected empty CSV, got {row['tags']!r}"


def test_sync_client_populates_first_and_last_booking_dates(client):
    """sync_client must update first_booking_at / last_booking_at on every call.

    Without this, the /admin/clients list sort + the "Client Since"/"Last
    Booking" UI rows are blank even though bookings exist.
    """
    email = "history@example.com"
    _insert_booking(email, "2026-03-10", "10:00")
    _insert_booking(email, "2026-05-22", "14:00")
    _insert_booking(email, "2026-01-05", "09:00")
    booking_app.sync_client(email, "History Tester")

    conn = booking_app.db_conn()
    row = conn.execute(
        "SELECT first_booking_at, last_booking_at, total_bookings, total_confirmed FROM clients WHERE email=?",
        (email,),
    ).fetchone()
    conn.close()

    assert row["first_booking_at"] == "2026-01-05"
    assert row["last_booking_at"] == "2026-05-22"
    assert row["total_bookings"] == 3
    assert row["total_confirmed"] == 3


def test_sync_client_excludes_cancelled_and_expired_from_date_aggregates(client):
    """Cancelled / expired bookings shouldn't shift the first/last dates —
    they're noise from clients who never actually showed up."""
    email = "cancelled@example.com"
    _insert_booking(email, "2026-04-01", "10:00")  # real
    _insert_booking(email, "2026-12-31", "10:00", status="cancelled", confirmed=0, paid=0)
    _insert_booking(email, "2025-01-01", "10:00", status="expired", confirmed=0, paid=0)
    booking_app.sync_client(email, "Cancelled Tester")

    conn = booking_app.db_conn()
    row = conn.execute(
        "SELECT first_booking_at, last_booking_at FROM clients WHERE email=?",
        (email,),
    ).fetchone()
    conn.close()
    assert row["first_booking_at"] == "2026-04-01"
    assert row["last_booking_at"] == "2026-04-01"


def test_admin_api_clients_returns_array_with_expected_fields(client):
    """Contract test for /admin/api/clients — the UI parses by index/key, so
    a field rename would break the page silently for the operator."""
    booking_app.sync_client("contract@example.com", "Contract Test")
    resp = client.get("/admin/api/clients", headers=_admin_headers())
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert isinstance(body, list), "API must return a JSON array"
    assert body, "should include the client we just synced"
    row = next((r for r in body if r["email"] == "contract@example.com"), None)
    assert row is not None
    # These keys are read directly by templates/admin_clients.html — keep stable.
    for key in ("id", "name", "email", "phone", "instagram", "tags",
                "total_bookings", "total_confirmed", "total_paid",
                "first_booking_at", "last_booking_at", "created_at"):
        assert key in row, f"missing field {key!r}"
    # Tags must be a (possibly empty) CSV string, never the literal "[]".
    assert row["tags"] != "[]"


def test_refresh_all_client_stats_repairs_stale_paid_totals(client):
    email = "stale-total@example.com"
    _insert_booking(email, "2026-08-01", "15:00", paid_amount=120.75)
    booking_app.sync_client(email, "Stale Total")
    conn = booking_app.db_conn()
    conn.execute("UPDATE clients SET total_paid=0, total_confirmed=0 WHERE email=?", (email,))
    conn.commit()
    conn.close()

    booking_app.refresh_all_client_stats()

    conn = booking_app.db_conn()
    row = conn.execute(
        "SELECT total_paid, total_confirmed FROM clients WHERE email=?", (email,)
    ).fetchone()
    conn.close()
    assert row["total_paid"] == pytest.approx(120.75)
    assert row["total_confirmed"] == 1


def test_canonical_site_url_constant_matches_csp(client):
    """If the canonical host ever changes, CSP frame-ancestors needs to follow.
    Catch the drift early."""
    assert booking_app.CANONICAL_SITE_HOST in booking_app._CSP
    assert booking_app.CANONICAL_SITE_URL.endswith(booking_app.CANONICAL_SITE_HOST)
