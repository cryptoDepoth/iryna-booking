"""Regression tests for the June 2026 Interac e-Transfer audit fixes.

F1  double-confirm: confirmed rows can't be re-confirmed; watcher batch filters
    the pending list after each confirmation.
F2  silent orphan: a fresh payment with ZERO eligible pending bookings alerts
    the admin instead of being skipped silently.
F3  sender spoofing: strict domain check on the parsed sender address.
F4  dead grace window: removed — reserved rows stop matching the moment
    reserved_until passes (expiry sweep would have killed them anyway).
F5  cross-band collision: exact-for-one + overpaid-for-another is ambiguous.
F6  alert spam: ambiguity/orphan Telegram alerts are deduped/throttled.
F7  $95 fallback: unknown deposit -> None -> booking excluded from matching.
F8  mixed timezones: all watcher/pending-window comparisons happen in UTC.
F9  legacy footgun: v1 checker deleted; timed_cron delegates to v2.
"""
import os
import sqlite3
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

import check_etransfer_v2 as checker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDMONTON = timezone(timedelta(hours=-6))


def _utcnow():
    """Naive UTC, matching bookings.created_at (SQLite CURRENT_TIMESTAMP)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _init_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            time TEXT,
            name TEXT,
            email TEXT,
            phone TEXT,
            instagram TEXT,
            session_type TEXT,
            status TEXT,
            paid INTEGER DEFAULT 0,
            confirmed INTEGER DEFAULT 0,
            created_at TEXT,
            reserved_until TEXT,
            paid_amount REAL,
            deposit_amount REAL,
            full_price REAL,
            event_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE processed_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            booking_id INTEGER,
            amount REAL,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE etransfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_number TEXT UNIQUE,
            message_id TEXT,
            sender_name TEXT,
            amount REAL,
            memo TEXT,
            direction TEXT DEFAULT 'in',
            email_date TEXT,
            matched_booking_id INTEGER,
            status TEXT DEFAULT 'unmatched',
            source TEXT DEFAULT 'email',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def _insert_booking(conn, *, name="Client", email="client@example.com",
                    status="reserved", created_at=None, reserved_until=None,
                    event_id="test-event", session_type="mini",
                    date="2026-06-20", slot_time="10:00",
                    deposit_amount=None, full_price=None,
                    confirmed=0, paid=0, paid_amount=None):
    cur = conn.execute("""
        INSERT INTO bookings(date,time,name,email,session_type,status,paid,confirmed,
                             created_at,reserved_until,deposit_amount,full_price,
                             paid_amount,event_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (date, slot_time, name, email, session_type, status, paid, confirmed,
          created_at, reserved_until, deposit_amount, full_price, paid_amount, event_id))
    return cur.lastrowid


@pytest.fixture(autouse=True)
def _fresh_alert_throttle():
    checker._ALERT_LAST_SENT.clear()
    yield
    checker._ALERT_LAST_SENT.clear()


# ── F1: double-confirm ────────────────────────────────────────────────────────

def test_confirm_booking_refuses_already_confirmed_row(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    conn = _init_db(db_path)
    now = _utcnow()
    booking_id = _insert_booking(
        conn, status="confirmed", confirmed=1, paid=1, paid_amount=120.75,
        created_at=now.strftime('%Y-%m-%d %H:%M:%S'),
    )
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", db_path)
    assert checker.confirm_booking(booking_id, 999.0) is False

    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT paid_amount, confirmed FROM bookings WHERE id=?",
                       (booking_id,)).fetchone()
    conn.close()
    assert row["confirmed"] == 1
    assert row["paid_amount"] == 120.75  # first payment not overwritten


def test_second_same_amount_email_cannot_reconfirm_same_booking(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    conn = _init_db(db_path)
    now = _utcnow()
    booking_id = _insert_booking(
        conn,
        created_at=(now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),
        reserved_until=(now + timedelta(minutes=13)).strftime('%Y-%m-%d %H:%M:%S'),
        deposit_amount=120.75, full_price=240.0,
    )
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", db_path)
    monkeypatch.setattr(checker, "read_message_body",
                        lambda _mid: "You've received $120.75 from A Client")

    pending = checker.get_pending_bookings(within_minutes=30)
    email_date = now.strftime('%Y-%m-%d %H:%M+00:00')
    confirmed_id, _ = checker.check_single_email({"id": "pay-1", "date": email_date}, pending)
    assert confirmed_id == booking_id

    # Second same-amount email in the same batch, deliberately passed the now
    # STALE pending list — the confirmed=0 guard must refuse the re-confirm.
    confirmed_id2, ambiguous2 = checker.check_single_email(
        {"id": "pay-2", "date": email_date}, pending)
    assert confirmed_id2 is None
    assert ambiguous2 is None

    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT paid_amount FROM bookings WHERE id=?", (booking_id,)).fetchone()
    linked = conn.execute(
        "SELECT booking_id FROM processed_emails WHERE message_id='pay-2'").fetchone()
    conn.close()
    assert row["paid_amount"] == 120.75
    assert linked is None  # second payment stays visible for manual handling


def test_watcher_batch_filters_pending_after_each_confirmation(monkeypatch):
    import app as booking_app

    seen_pending = []

    def fake_check(email, pending, reconciliation):
        seen_pending.append([b["id"] for b in pending])
        return (1, None) if email["id"] == "m1" else (None, None)

    monkeypatch.setattr(checker, "is_etransfer_email", lambda e: True)
    monkeypatch.setattr(checker, "check_single_email", fake_check)

    confirmed = booking_app._process_etransfer_email_batch(
        [{"id": "m1"}, {"id": "m2"}],
        [{"id": 1}, {"id": 2}],
        [],
    )
    assert confirmed == [1]
    # second email must no longer see booking #1
    assert seen_pending == [[1, 2], [2]]


# ── F2: silent orphan ─────────────────────────────────────────────────────────

def test_fresh_payment_with_zero_eligible_pending_alerts_admin(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    _init_db(db_path).close()
    monkeypatch.setattr(checker, "DB_PATH", db_path)

    alerts = []
    monkeypatch.setattr(checker, "_send_admin_alert", lambda text: alerts.append(text))
    monkeypatch.setattr(checker, "read_message_body",
                        lambda _mid: "You've received $120.75 from Ghost Client")

    now = _utcnow()
    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "orphan-no-pending", "date": now.strftime('%Y-%m-%d %H:%M+00:00')}, [], [])

    assert confirmed_id is None and ambiguous is None
    assert len(alerts) == 1
    assert "ZERO eligible pending bookings" in alerts[0]
    assert "$120.75" in alerts[0]

    # NOT marked processed: a booking completed right after the payment
    # (pay-first flow) must still be able to match on a later pass.
    conn = sqlite3.connect(db_path)
    processed = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id='orphan-no-pending'").fetchone()
    conn.close()
    assert processed is None


def test_stale_payment_with_zero_eligible_pending_stays_silent(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    _init_db(db_path).close()
    monkeypatch.setattr(checker, "DB_PATH", db_path)

    alerts = []
    monkeypatch.setattr(checker, "_send_admin_alert", lambda text: alerts.append(text))
    monkeypatch.setattr(checker, "read_message_body",
                        lambda _mid: "You've received $120.75 from Old History")

    stale_date = (_utcnow() - timedelta(days=3)).strftime('%Y-%m-%d %H:%M+00:00')
    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "orphan-stale", "date": stale_date}, [], [])

    assert confirmed_id is None and ambiguous is None
    assert alerts == []  # old unmatched inbox history must not page anyone


# ── F3: sender spoofing ───────────────────────────────────────────────────────

def test_is_etransfer_email_requires_strict_interac_domain():
    subject = "Interac e-Transfer: You've received $120.75 and it has been automatically deposited."

    def envelope(addr):
        return {"subject": subject, "from": {"addr": addr}}

    # legitimate senders
    assert checker.is_etransfer_email(envelope("notify@payments.interac.ca")) is True
    assert checker.is_etransfer_email(envelope("catch@interac.ca")) is True
    assert checker.is_etransfer_email(envelope("Interac <notify@notify.interac.ca>")) is True

    # spoofed senders that the old substring check accepted
    assert checker.is_etransfer_email(envelope("notify@interac.ca.evil.com")) is False
    assert checker.is_etransfer_email(envelope("interac.ca@evil.com")) is False
    assert checker.is_etransfer_email(envelope("payments.interac.ca")) is False  # no @
    assert checker.is_etransfer_email(envelope("notify@notinterac.ca")) is False
    assert checker.is_etransfer_email(envelope("")) is False


# ── F4: dead grace window removed ─────────────────────────────────────────────

def test_reserved_booking_past_reserved_until_is_no_longer_matchable(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    conn = _init_db(db_path)
    now = _utcnow()
    # Under the old 60-min "grace window" this row was still a candidate.
    _insert_booking(
        conn,
        created_at=(now - timedelta(minutes=25)).strftime('%Y-%m-%d %H:%M:%S'),
        reserved_until=(now - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'),
        deposit_amount=120.75,
    )
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", db_path)
    assert checker.get_pending_bookings(within_minutes=30) == []


def test_admin_manual_block_with_one_hour_hold_stays_matchable(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    conn = _init_db(db_path)
    now = _utcnow()
    # Admin manual blocks reserve for 1h; 45 minutes in they must still match.
    _insert_booking(
        conn,
        created_at=(now - timedelta(minutes=45)).strftime('%Y-%m-%d %H:%M:%S'),
        reserved_until=(now + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S'),
        deposit_amount=95.0,
    )
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", db_path)
    assert len(checker.get_pending_bookings(within_minutes=30)) == 1


# ── F5: cross-band price collision ────────────────────────────────────────────

def test_exact_plus_overpaid_cross_band_is_ambiguous(monkeypatch):
    expected = {"a": 120.75, "b": 95.0}
    full = {"a": 240.0, "b": 190.0}
    monkeypatch.setattr(checker, "get_expected_amount_for_booking", lambda bid: expected[bid])
    monkeypatch.setattr(checker, "_get_booking_full_price", lambda bk: full[bk["id"]])

    # $120.75 is exact for a AND a plausible overpayment for b ($95 deposit,
    # $190 full) — the payer could be either client. Must not auto-confirm.
    matched, ambiguous, match_type = checker.match_by_amount_only(
        120.75, [{"id": "a"}, {"id": "b"}])

    assert matched is None and match_type is None
    assert sorted(x["id"] for x in ambiguous) == ["a", "b"]


def test_cross_band_collision_requires_disambiguation_end_to_end(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    conn = _init_db(db_path)
    now = _utcnow()
    common = dict(
        created_at=(now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),
        reserved_until=(now + timedelta(minutes=13)).strftime('%Y-%m-%d %H:%M:%S'),
    )
    _insert_booking(conn, name="Exact Client", deposit_amount=120.75, full_price=240.0, **common)
    _insert_booking(conn, name="Overpay Client", slot_time="11:00",
                    deposit_amount=95.0, full_price=190.0, **common)
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", db_path)
    monkeypatch.setattr(checker, "read_message_body",
                        lambda _mid: "You've received $120.75")  # no identifying details
    ambiguity_alerts = []
    monkeypatch.setattr(checker, "_notify_admin_ambiguity",
                        lambda amount, candidates: ambiguity_alerts.append((amount, len(candidates))))

    pending = checker.get_pending_bookings(within_minutes=30)
    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "cross-band", "date": now.strftime('%Y-%m-%d %H:%M+00:00')}, pending)

    assert confirmed_id is None
    assert ambiguous is not None and len(ambiguous) == 2
    assert ambiguity_alerts == [(120.75, 2)]

    conn = sqlite3.connect(db_path)
    confirmed_count = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE confirmed=1 OR paid=1").fetchone()[0]
    conn.close()
    assert confirmed_count == 0


# ── F6: alert throttling ──────────────────────────────────────────────────────

def _candidates():
    return [
        {"id": 1, "name": "A", "date": "2026-06-20", "time": "10:00", "email": "a@x.com"},
        {"id": 2, "name": "B", "date": "2026-06-20", "time": "11:00", "email": "b@x.com"},
    ]


def test_ambiguity_alert_dedupes_within_throttle_window(monkeypatch):
    sent = []
    monkeypatch.setattr(checker, "_send_admin_alert", lambda text: sent.append(text))

    checker._notify_admin_ambiguity(120.75, _candidates())
    checker._notify_admin_ambiguity(120.75, _candidates())  # same collision 60s later
    assert len(sent) == 1

    # a different collision (other amount) still alerts
    checker._notify_admin_ambiguity(95.0, _candidates())
    assert len(sent) == 2

    # once the throttle window has elapsed, the same collision fires again
    for key in list(checker._ALERT_LAST_SENT):
        checker._ALERT_LAST_SENT[key] -= checker.ETRANSFER_ALERT_THROTTLE_SECONDS + 1
    checker._notify_admin_ambiguity(120.75, _candidates())
    assert len(sent) == 3


def test_orphan_alert_dedupes_per_message_id(monkeypatch):
    sent = []
    monkeypatch.setattr(checker, "_send_admin_alert", lambda text: sent.append(text))

    checker._notify_admin_orphan(120.75, "body", "msg-1")
    checker._notify_admin_orphan(120.75, "body", "msg-1")
    assert len(sent) == 1

    checker._notify_admin_orphan(120.75, "body", "msg-2")
    assert len(sent) == 2


def test_admin_alert_escapes_raw_email_angle_brackets(monkeypatch):
    sent = []
    fake_app = SimpleNamespace(_notify_admin=lambda text: sent.append(text))
    monkeypatch.setitem(sys.modules, "app", fake_app)

    checker._send_admin_alert("Email snippet: Interac <notify@payments.interac.ca>")

    assert sent == ["Email snippet: Interac &lt;notify@payments.interac.ca&gt;"]


# ── F7: no hardcoded $95 fallback ─────────────────────────────────────────────

def test_unknown_deposit_returns_none_instead_of_95(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    conn = _init_db(db_path)
    now = _utcnow()
    booking_id = _insert_booking(
        conn, event_id="ghost-event-not-in-yaml",
        created_at=(now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),
        reserved_until=(now + timedelta(minutes=13)).strftime('%Y-%m-%d %H:%M:%S'),
        deposit_amount=None,
    )
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", db_path)
    monkeypatch.setattr(checker, "EVENTS_YAML_PATH", str(tmp_path / "missing-events.yaml"))

    assert checker.get_expected_amount_for_booking(booking_id) is None


def test_booking_with_unknown_deposit_never_matches_95_dollars(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    conn = _init_db(db_path)
    now = _utcnow()
    booking_id = _insert_booking(
        conn, event_id="ghost-event-not-in-yaml",
        created_at=(now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'),
        reserved_until=(now + timedelta(minutes=13)).strftime('%Y-%m-%d %H:%M:%S'),
        deposit_amount=None,
    )
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", db_path)
    monkeypatch.setattr(checker, "EVENTS_YAML_PATH", str(tmp_path / "missing-events.yaml"))

    pending = checker.get_pending_bookings(within_minutes=30)
    assert len(pending) == 1  # eligible window-wise…
    matched, ambiguous, match_type = checker.match_by_amount_only(95.0, pending)
    # …but with an unknown deposit the old code would have "exact"-matched $95.
    assert (matched, ambiguous, match_type) == (None, [], None)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT confirmed, paid FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    assert tuple(row) == (0, 0)


def test_interac_email_with_gift_code_confirms_pending_gift_certificate(tmp_path, monkeypatch):
    booking_db_path = str(tmp_path / "bookings.db")
    conn = _init_db(booking_db_path)
    conn.close()
    gift_db_path = str(tmp_path / "gift.db")
    gift_pdf_dir = str(tmp_path / "gift_pdfs")

    monkeypatch.setattr(checker, "DB_PATH", booking_db_path)
    monkeypatch.setenv("GIFT_REFERRAL_DB", gift_db_path)
    monkeypatch.setenv("GIFT_PDF_DIR", gift_pdf_dir)
    monkeypatch.setenv("TEST_MODE", "true")
    alerts = []
    monkeypatch.setattr(checker, "_send_admin_alert", lambda text: alerts.append(text))

    gift_dir = os.path.join(ROOT, "gift-referral")
    if gift_dir not in sys.path:
        sys.path.insert(0, gift_dir)
    import gift_referral_db as gift_db
    import gift_referral_email as gift_email

    monkeypatch.setattr(gift_db, "DB_PATH", gift_db_path)
    monkeypatch.setattr(gift_email, "TEST_MODE", True)
    gift_db.init_db(gift_db_path)
    code = gift_db.create_gift_certificate(
        purchaser_email="buyer@example.com",
        purchaser_name="Buyer",
        recipient_name="Recipient",
        recipient_email="",
        personal_message="Enjoy",
        session_type="mini",
        amount=210.0,
        amount_with_gst=220.50,
        payment_method="interac",
        payment_status="pending",
        paid_amount=0.0,
        status="pending_payment",
    )

    body = f"You've received $220.50 from Client. Message: Gift certificate {code}"
    monkeypatch.setattr(checker, "read_message_body", lambda message_id: body)

    confirmed_id, ambiguous = checker.check_single_email(
        {"id": "gift-pay-1", "date": datetime.now(timezone.utc).isoformat()},
        bookings=[],
    )

    assert confirmed_id is None
    assert ambiguous is None
    cert = gift_db.get_gift_certificate(code)
    assert cert["status"] == "active"
    assert cert["payment_status"] == "paid"
    assert cert["paid_amount"] == 220.50

    conn = sqlite3.connect(booking_db_path)
    conn.row_factory = sqlite3.Row
    transfer = conn.execute(
        "SELECT matched_gift_code, status FROM etransfers WHERE message_id='gift-pay-1'"
    ).fetchone()
    processed = conn.execute(
        "SELECT message_id FROM processed_emails WHERE message_id='gift-pay-1'"
    ).fetchone()
    conn.close()
    assert dict(transfer) == {"matched_gift_code": code, "status": "matched"}
    assert processed is not None
    assert any(code in alert for alert in alerts)


# ── F8: UTC everywhere in watcher / pending windows ───────────────────────────

def test_parse_db_datetime_normalizes_offsets_to_naive_utc():
    assert checker._parse_db_datetime("2026-06-11T10:00:00-06:00") == datetime(2026, 6, 11, 16, 0, 0)
    assert checker._parse_db_datetime("2026-06-11 16:00:00") == datetime(2026, 6, 11, 16, 0, 0)
    assert checker._parse_db_datetime(None) is None


def test_get_pending_bookings_compares_offset_reserved_until_as_instant(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bookings.db")
    conn = _init_db(db_path)
    now_utc = datetime.now(timezone.utc)
    created = (now_utc.replace(tzinfo=None) - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')
    # reserved_until written the way app.py writes it: America/Edmonton offset
    live = (now_utc + timedelta(minutes=13)).astimezone(EDMONTON).isoformat()
    expired = (now_utc - timedelta(minutes=5)).astimezone(EDMONTON).isoformat()
    live_id = _insert_booking(conn, name="Live", created_at=created,
                              reserved_until=live, deposit_amount=95.0)
    _insert_booking(conn, name="Expired", slot_time="11:00", created_at=created,
                    reserved_until=expired, deposit_amount=95.0)
    conn.commit(); conn.close()

    monkeypatch.setattr(checker, "DB_PATH", db_path)
    pending = checker.get_pending_bookings(within_minutes=30)

    assert [b["id"] for b in pending] == [live_id]


def test_expire_reservations_compares_instants_not_strings(tmp_path, monkeypatch):
    import app as booking_app

    db_path = str(tmp_path / "expire.db")
    conn = _init_db(db_path)
    now_utc = datetime.now(timezone.utc)
    created = (now_utc.replace(tzinfo=None) - timedelta(minutes=20)).strftime('%Y-%m-%d %H:%M:%S')
    past_edm = (now_utc - timedelta(minutes=1)).astimezone(EDMONTON).isoformat()
    future_edm = (now_utc + timedelta(minutes=10)).astimezone(EDMONTON).isoformat()
    past_naive_utc = (now_utc.replace(tzinfo=None) - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')

    expired_a = _insert_booking(conn, name="Past offset", created_at=created,
                                reserved_until=past_edm)
    kept = _insert_booking(conn, name="Future offset", slot_time="11:00",
                           created_at=created, reserved_until=future_edm)
    expired_b = _insert_booking(conn, name="Legacy naive", slot_time="12:00",
                                status="pending_payment", created_at=created,
                                reserved_until=past_naive_utc)
    conn.commit(); conn.close()

    def _conn():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(booking_app, "db_conn", _conn)
    monkeypatch.setattr(booking_app, "_record_booking_funnel_event", lambda *a, **k: None)
    monkeypatch.setattr(booking_app, "_emit_n8n_event", lambda *a, **k: False)

    released = booking_app.expire_reservations()
    assert released == 2

    conn = _conn()
    statuses = {row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM bookings")}
    conn.close()
    assert statuses[expired_a] == "expired"
    assert statuses[expired_b] == "expired"
    assert statuses[kept] == "reserved"


# ── F9: legacy v1 checker removed ─────────────────────────────────────────────

def test_legacy_v1_checker_is_gone():
    assert not os.path.exists(os.path.join(ROOT, "check_etransfer.py"))


def test_timed_cron_delegates_matching_to_v2():
    import timed_cron

    # the only matcher implementations are the v2 ones
    assert timed_cron.check_single_email is checker.check_single_email
    assert timed_cron.is_etransfer_email is checker.is_etransfer_email
    assert timed_cron.get_pending_bookings is checker.get_pending_bookings
    assert timed_cron.get_emails is checker.get_emails

    # the stale local copies (v1 parser/matcher, $95 fallback, DELETE-based
    # expiry) must not come back
    for legacy in ("match_payment_to_booking", "extract_payment_info",
                   "read_message_body", "expire_unpaid_bookings",
                   "expected_deposit_amount", "confirm_booking",
                   "get_expected_amount_for_booking", "sync_to_notion"):
        assert not hasattr(timed_cron, legacy), legacy
