#!/usr/bin/env python3
"""
e-Transfer Payment Checker v2 — Safe & Amount-only matching
Rules:
1. processed_emails ledger: never process same message twice
2. Amount-only matching: no name/email matching (sender may pay from another account)
3. Exact amount only (no ±3 tolerance)
4. Old emails won't match new bookings (check booking created_at vs email date)
5. Dynamic pricing: reads deposit from events.yaml via booking event_id
6. Ambiguity alert: if same amount matches multiple bookings → admin notification
7. Reconciliation: later Interac emails may correct paid_amount for an already
   confirmed booking when the email strongly matches name/date/time.
"""

import os
import sys
import re
import json
import sqlite3
import subprocess
import threading
import time
from html import escape as _html_escape
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "bookings.db"))
EVENTS_YAML_PATH = os.environ.get("EVENTS_YAML_PATH",
    os.path.join(os.path.dirname(__file__), "events.yaml"))
ETRANSFER_EMAIL_PAGE_SIZE = int(os.environ.get("ETRANSFER_EMAIL_PAGE_SIZE", "75"))
ETRANSFER_EMAIL_LOOKBACK_DAYS = int(os.environ.get("ETRANSFER_EMAIL_LOOKBACK_DAYS", "120"))
ETRANSFER_EMAIL_TIMEOUT = int(os.environ.get("ETRANSFER_EMAIL_TIMEOUT", "20"))
_EMAIL_FETCH_LOCK = threading.Lock()
_EMAIL_FETCH_CACHE = {"ts": 0.0, "page_size": None, "lookback_days": None, "emails": None}
_EMAIL_FETCH_CACHE_TTL = int(os.environ.get("ETRANSFER_EMAIL_CACHE_TTL", "60"))
_EMAIL_FETCH_STATUS = threading.local()
_BODY_READ_STATUS = threading.local()

# Admin alerts (ambiguity/orphan) are throttled: the watcher re-scans every
# ~60s and an unresolved collision would otherwise re-fire the same Telegram
# message on every pass.
ETRANSFER_ALERT_THROTTLE_SECONDS = int(os.environ.get("ETRANSFER_ALERT_THROTTLE_SECONDS", "3600"))
# A payment email with no eligible pending booking only alerts the admin while
# it is recent; old unmatched inbox history must not page anyone.
ETRANSFER_ORPHAN_FRESH_HOURS = int(os.environ.get("ETRANSFER_ORPHAN_FRESH_HOURS", "24"))
_ALERT_LAST_SENT = {}
_ALERT_LOCK = threading.Lock()


def _utc_now():
    """Naive UTC now.

    bookings.created_at is written by SQLite CURRENT_TIMESTAMP (UTC) and
    _parse_email_datetime/_parse_db_datetime normalize to naive UTC, so every
    time comparison in this module happens on the same clock. Never use
    datetime.now() here — server-local time differs between the Fly container
    (UTC) and a dev Mac (America/Edmonton).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _should_send_alert(key):
    """Return True at most once per ETRANSFER_ALERT_THROTTLE_SECONDS per key."""
    now = time.time()
    with _ALERT_LOCK:
        last = _ALERT_LAST_SENT.get(key)
        if last is not None and now - last < ETRANSFER_ALERT_THROTTLE_SECONDS:
            return False
        if len(_ALERT_LAST_SENT) > 512:
            cutoff = now - ETRANSFER_ALERT_THROTTLE_SECONDS
            for stale in [k for k, ts in _ALERT_LAST_SENT.items() if ts < cutoff]:
                _ALERT_LAST_SENT.pop(stale, None)
        _ALERT_LAST_SENT[key] = now
        return True


def _send_admin_alert(text):
    """Deliver an admin alert via the app's Telegram notifier."""
    from app import _notify_admin
    _notify_admin(_html_escape(str(text or ""), quote=False))


def _set_email_fetch_status(outcome):
    """Publish only a bounded outcome code for watcher observability."""
    _EMAIL_FETCH_STATUS.outcome = str(outcome or "unknown")


def get_last_email_fetch_status():
    return {"outcome": getattr(_EMAIL_FETCH_STATUS, "outcome", "never")}


def reset_email_fetch_status():
    _EMAIL_FETCH_STATUS.outcome = "never"


def _record_body_read_failure():
    _BODY_READ_STATUS.unavailable_count = (
        int(getattr(_BODY_READ_STATUS, "unavailable_count", 0)) + 1
    )


def consume_last_body_read_status():
    """Return and clear body-read degradation for the current scan thread."""
    unavailable_count = int(getattr(_BODY_READ_STATUS, "unavailable_count", 0))
    _BODY_READ_STATUS.unavailable_count = 0
    return {"unavailable_count": unavailable_count}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_etransfer_claim_schema(conn):
    """Keep standalone checker databases compatible with atomic ownership."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etransfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_number TEXT UNIQUE,
            message_id TEXT,
            sender_name TEXT,
            amount REAL,
            memo TEXT,
            direction TEXT DEFAULT 'in',
            email_date TEXT,
            matched_booking_id INTEGER,
            matched_gift_code TEXT,
            status TEXT DEFAULT 'unmatched',
            source TEXT DEFAULT 'email',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            archived_at TEXT,
            restored_at TEXT
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(etransfers)").fetchall()
    }
    migrations = {
        "matched_gift_code": "ALTER TABLE etransfers ADD COLUMN matched_gift_code TEXT",
        "archived_at": "ALTER TABLE etransfers ADD COLUMN archived_at TEXT",
        "restored_at": "ALTER TABLE etransfers ADD COLUMN restored_at TEXT",
    }
    for column, ddl in migrations.items():
        if column not in columns:
            conn.execute(ddl)


def is_message_processed(message_id):
    return get_processed_email(message_id) is not None


def get_processed_email(message_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM processed_emails WHERE message_id = ?", (message_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def is_etransfer_archived(message_id):
    """Return True when an unmatched ledger row was archived by an admin."""
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT 1
              FROM etransfers
             WHERE message_id=?
               AND status='ignored'
               AND matched_booking_id IS NULL
               AND COALESCE(matched_gift_code, '')=''
             LIMIT 1
            """,
            (message_id,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def mark_message_processed(message_id, booking_id, amount):
    conn = get_db()
    c = conn.cursor()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ledger_row = None
        try:
            ledger_row = c.execute(
                """
                SELECT id, status, matched_booking_id, matched_gift_code
                  FROM etransfers
                 WHERE message_id=?
                 LIMIT 1
                """,
                (message_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Older standalone checker databases may not have the ledger table.
            ledger_row = None

        if ledger_row is not None and str(ledger_row["status"] or "").lower() == "ignored":
            conn.rollback()
            return False

        try:
            c.execute("""
                INSERT INTO processed_emails (message_id, booking_id, amount)
                VALUES (?, ?, ?)
            """, (message_id, booking_id, amount))
        except sqlite3.IntegrityError:
            # A message can be first recorded as an orphan, then later become a
            # safe reconciliation match once the booking is confirmed/manual-fixed.
            # Only attach/update it when doing so cannot steal it from another row.
            if booking_id is not None:
                c.execute("""
                    UPDATE processed_emails
                       SET booking_id=?, amount=?, processed_at=CURRENT_TIMESTAMP
                     WHERE message_id=?
                       AND (booking_id IS NULL OR booking_id=?)
                """, (booking_id, amount, message_id, booking_id))

        # Auto-link the ledger entry for this email to the booking it confirmed.
        # The compare-and-set prevents an archived row from being reactivated
        # after check_single_email's final archive-state check.
        if booking_id is not None and ledger_row is not None:
            already_linked = (
                str(ledger_row["status"] or "").lower() == "matched"
                and ledger_row["matched_booking_id"] == booking_id
                and not ledger_row["matched_gift_code"]
            )
            if not already_linked:
                c.execute(
                    """
                    UPDATE etransfers
                       SET matched_booking_id=?, status='matched'
                     WHERE id=?
                       AND status='unmatched'
                       AND matched_booking_id IS NULL
                       AND COALESCE(matched_gift_code, '')=''
                    """,
                    (booking_id, ledger_row["id"]),
                )
                if c.rowcount != 1:
                    conn.rollback()
                    return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_processed_email(
    conn,
    message_id,
    booking_id,
    amount,
    *,
    allow_existing_orphan=False,
):
    """Claim one message inside the caller's open financial transaction."""
    existing = conn.execute(
        """
        SELECT booking_id
          FROM processed_emails
         WHERE message_id=?
         LIMIT 1
        """,
        (message_id,),
    ).fetchone()
    if existing is None:
        try:
            conn.execute(
                """
                INSERT INTO processed_emails (message_id, booking_id, amount)
                VALUES (?, ?, ?)
                """,
                (message_id, booking_id, amount),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    if not allow_existing_orphan or existing["booking_id"] is not None:
        return False
    cursor = conn.execute(
        """
        UPDATE processed_emails
           SET booking_id=?, amount=?, processed_at=CURRENT_TIMESTAMP
         WHERE message_id=?
           AND booking_id IS NULL
        """,
        (booking_id, amount, message_id),
    )
    return cursor.rowcount == 1


def _resolve_active_ledger_id(conn, message_id, ledger_id=None):
    """Return one exact active ledger row, rejecting duplicate ownership."""
    rows = conn.execute(
        """
        SELECT id
          FROM etransfers
         WHERE message_id=?
           AND direction='in'
           AND status='unmatched'
           AND matched_booking_id IS NULL
           AND COALESCE(matched_gift_code, '')=''
         ORDER BY id
         LIMIT 2
        """,
        (message_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    resolved_id = rows[0]["id"]
    if ledger_id is not None and resolved_id != ledger_id:
        return None
    return resolved_id


def _claim_etransfer_booking(conn, ledger_id, message_id, booking_id):
    """Atomically move one active unmatched ledger row to a booking."""
    cursor = conn.execute(
        """
        UPDATE etransfers
           SET matched_booking_id=?,
               matched_gift_code=NULL,
               status='matched'
         WHERE id=?
           AND message_id=?
           AND direction='in'
           AND status='unmatched'
           AND matched_booking_id IS NULL
           AND COALESCE(matched_gift_code, '')=''
        """,
        (booking_id, ledger_id, message_id),
    )
    return cursor.rowcount == 1


def _claim_etransfer_gift(conn, ledger_id, message_id, code):
    """Atomically move one active unmatched ledger row to a gift certificate."""
    cursor = conn.execute(
        """
        UPDATE etransfers
           SET matched_gift_code=?,
               matched_booking_id=NULL,
               status='matched'
         WHERE id=?
           AND message_id=?
           AND direction='in'
           AND status='unmatched'
           AND matched_booking_id IS NULL
           AND COALESCE(matched_gift_code, '')=''
        """,
        (code, ledger_id, message_id),
    )
    return cursor.rowcount == 1


def _apply_booking_payment_transaction(
    message_id,
    booking_id,
    amount,
    mutation,
    *,
    allow_existing_orphan=False,
    ledger_id=None,
):
    """Commit ledger ownership, message ownership, and one booking mutation."""
    conn = get_db()
    try:
        _ensure_etransfer_claim_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        ledger_id = _resolve_active_ledger_id(conn, message_id, ledger_id)
        if ledger_id is None:
            conn.rollback()
            return False
        if not _claim_etransfer_booking(
            conn,
            ledger_id,
            message_id,
            booking_id,
        ):
            conn.rollback()
            return False
        if not _claim_processed_email(
            conn,
            message_id,
            booking_id,
            amount,
            allow_existing_orphan=allow_existing_orphan,
        ):
            conn.rollback()
            return False

        if mutation == "confirm":
            cursor = conn.execute(
                """
                UPDATE bookings
                   SET confirmed=1, paid=1, status='confirmed', paid_amount=?
                 WHERE id=?
                   AND confirmed=0
                   AND paid=0
                   AND status IN ('reserved', 'pending_payment')
                """,
                (amount, booking_id),
            )
        elif mutation == "partial":
            cursor = conn.execute(
                """
                UPDATE bookings
                   SET paid_amount=?, status='partial_payment'
                 WHERE id=?
                   AND confirmed=0
                   AND paid=0
                   AND status IN ('reserved', 'pending_payment')
                """,
                (amount, booking_id),
            )
        elif mutation == "reconcile":
            cursor = conn.execute(
                """
                UPDATE bookings
                   SET paid=1, paid_amount=?
                 WHERE id=?
                   AND confirmed=1
                   AND status='confirmed'
                   AND (paid_amount IS NULL OR paid_amount < ?)
                """,
                (amount, booking_id, amount - 0.009),
            )
        else:
            raise ValueError(f"Unknown booking payment mutation: {mutation}")

        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_GIFT_CODE_RE = re.compile(r"\bGIFT-[A-Z0-9]{4}-[A-Z0-9]{4}\b", re.I)


def extract_gift_code(body_text):
    """Return a gift certificate code embedded in an Interac memo/body."""
    match = _GIFT_CODE_RE.search(body_text or "")
    return match.group(0).upper() if match else None


def _ensure_etransfer_gift_column(conn):
    _ensure_etransfer_claim_schema(conn)


def _mark_etransfer_gift_match(message_id, code):
    conn = get_db()
    try:
        _ensure_etransfer_gift_column(conn)
        cursor = conn.execute(
            """
            UPDATE etransfers
               SET matched_gift_code=?, status='matched'
             WHERE message_id=?
               AND status='unmatched'
               AND matched_booking_id IS NULL
               AND COALESCE(matched_gift_code, '')=''
            """,
            (code, message_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _gift_modules():
    gift_dir = os.path.join(os.path.dirname(__file__), "gift-referral")
    if gift_dir not in sys.path:
        sys.path.insert(0, gift_dir)
    import gift_referral_db as gift_db
    from gift_referral_pdf import save_gift_pdf
    from gift_referral_email import send_gift_purchaser_email, send_gift_recipient_email

    gift_db.init_db()
    return gift_db, save_gift_pdf, send_gift_purchaser_email, send_gift_recipient_email


def _gift_database_is_attach_compatible(conn, gift_db_path):
    """Verify SQLite's cross-database atomic-commit prerequisites."""
    if not DB_PATH or not gift_db_path:
        return False
    if DB_PATH == ":memory:" or gift_db_path == ":memory:":
        return False
    main_path = os.path.realpath(DB_PATH)
    gift_path = os.path.realpath(gift_db_path)
    if main_path == gift_path:
        return False
    if not os.path.isfile(main_path) or not os.path.isfile(gift_path):
        return False
    if os.stat(os.path.dirname(main_path)).st_dev != os.stat(os.path.dirname(gift_path)).st_dev:
        return False

    conn.execute("ATTACH DATABASE ? AS gift_atomic", (gift_path,))
    main_journal = str(conn.execute("PRAGMA main.journal_mode").fetchone()[0]).lower()
    gift_journal = str(
        conn.execute("PRAGMA gift_atomic.journal_mode").fetchone()[0]
    ).lower()
    main_synchronous = int(conn.execute("PRAGMA main.synchronous").fetchone()[0])
    gift_synchronous = int(
        conn.execute("PRAGMA gift_atomic.synchronous").fetchone()[0]
    )
    return (
        main_journal in {"delete", "persist", "truncate"}
        and gift_journal in {"delete", "persist", "truncate"}
        and main_synchronous > 0
        and gift_synchronous > 0
    )


def _apply_gift_payment_transaction(
    gift_db,
    message_id,
    code,
    amount,
    *,
    ledger_id=None,
    unique_amount_match=False,
    email_received_at=None,
):
    """Atomically own the message and activate a gift across attached DBs."""
    conn = get_db()
    attached = False
    try:
        _ensure_etransfer_claim_schema(conn)
        conn.commit()
        if not _gift_database_is_attach_compatible(conn, gift_db.DB_PATH):
            return False
        attached = True
        conn.execute("BEGIN IMMEDIATE")
        ledger_id = _resolve_active_ledger_id(conn, message_id, ledger_id)
        if ledger_id is None:
            conn.rollback()
            return False
        if unique_amount_match:
            candidates = conn.execute(
                """
                SELECT code, created_at
                  FROM gift_atomic.gift_certificates
                 WHERE status='pending_payment'
                   AND ABS(amount_with_gst - ?) < 0.01
                 ORDER BY created_at ASC
                """,
                (amount,),
            ).fetchall()
            if email_received_at is not None:
                candidates = [
                    candidate
                    for candidate in candidates
                    if (
                        _parse_db_datetime(candidate["created_at"]) is None
                        or _parse_db_datetime(candidate["created_at"])
                        <= email_received_at
                    )
                ]
            if len(candidates) != 1 or candidates[0]["code"] != code:
                conn.rollback()
                return False
        if not _claim_etransfer_gift(
            conn,
            ledger_id,
            message_id,
            code,
        ):
            conn.rollback()
            return False
        if not _claim_processed_email(conn, message_id, None, amount):
            conn.rollback()
            return False
        cursor = conn.execute(
            """
            UPDATE gift_atomic.gift_certificates
               SET status='active',
                   payment_status='paid',
                   paid_amount=?,
                   payment_reference=COALESCE(?, payment_reference),
                   activated_at=CURRENT_TIMESTAMP
             WHERE code=?
               AND status='pending_payment'
               AND ABS(amount_with_gst - ?) < 0.01
            """,
            (amount, message_id, code, amount),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        if attached and not conn.in_transaction:
            try:
                conn.execute("DETACH DATABASE gift_atomic")
            except sqlite3.Error:
                pass
        conn.close()


def try_confirm_gift_etransfer(
    amount,
    body,
    message_id,
    email_received_at=None,
    *,
    ledger_id=None,
):
    """Confirm a pending gift certificate from an Interac e-Transfer email.

    Two paths:
      1. Code path  — memo contains GIFT-XXXX-XXXX → exact code + amount match.
      2. Amount-only fallback — no code in memo → match by amount_with_gst across
         all pending certs.  Single match → auto-confirm.  Multiple matches →
         admin Telegram alert, no auto-confirm.

    Returns False when this is not a handled gift payment. Otherwise returns:
      - "committed" after durable atomic activation and ownership;
      - "handled" for non-financial gift outcomes that should be processed;
      - "conflict" when activation ownership or predicates did not commit.
    """
    code = extract_gift_code(body)

    if code:
        # ── Code-based path (unchanged) ──────────────────────────────────────
        gift_db, save_gift_pdf, send_buyer_email, send_recipient_email = _gift_modules()
        cert = gift_db.get_gift_certificate(code)
        if not cert:
            _send_admin_alert(
                f"Gift payment memo has unknown code {code}. Amount: ${amount:.2f}. Message ID: {message_id}"
            )
            return "handled"

        expected = float(cert.get("amount_with_gst") or 0)
        if cert.get("status") != "pending_payment":
            _send_admin_alert(
                f"Gift e-Transfer received for {code}, but status is {cert.get('status')}. "
                f"Amount: ${amount:.2f}; expected ${expected:.2f}."
            )
            _mark_etransfer_gift_match(message_id, code)
            return "handled"

        if abs(amount - expected) >= 0.01:
            _send_admin_alert(
                f"Gift e-Transfer amount mismatch for {code}. Received ${amount:.2f}; "
                f"expected ${expected:.2f}. Certificate is still pending."
            )
            return "handled"

        if not _apply_gift_payment_transaction(
            gift_db,
            message_id,
            code,
            amount,
            ledger_id=ledger_id,
        ):
            print(f"   [gift] Atomic activation did not claim {code}")
            return "conflict"

        cert = gift_db.get_gift_certificate(code)
        pdf_path = None
        try:
            pdf_path = save_gift_pdf(cert)
            gift_db.update_gift_pdf(code, pdf_path)
            cert = gift_db.get_gift_certificate(code) or cert
        except Exception as exc:
            print(f"   [gift] PDF generation failed for {code}: {exc}")

        send_buyer_email(cert, pdf_path=pdf_path)
        if cert.get("recipient_email"):
            send_recipient_email(cert)
        _send_admin_alert(
            f"Gift certificate paid and activated: {code}\n"
            f"Amount: ${amount:.2f}\n"
            f"Buyer: {cert.get('purchaser_name')} <{cert.get('purchaser_email')}>"
        )
        print(f"   ✅ CONFIRMED Gift certificate {code} — ${amount:.2f}")
        return "committed"

    # ── Amount-only fallback (no code in memo) ────────────────────────────────
    try:
        gift_db, save_gift_pdf, send_buyer_email, send_recipient_email = _gift_modules()
    except Exception:
        return False

    candidates = gift_db.get_pending_gift_certs_by_amount(amount)
    if not candidates:
        return False  # not a gift payment

    # Apply timing guard: skip certs created after the email arrived.
    if email_received_at is not None:
        filtered = []
        for cert in candidates:
            cert_created = _parse_db_datetime(cert.get("created_at"))
            if cert_created is None or cert_created <= email_received_at:
                filtered.append(cert)
        candidates = filtered

    if not candidates:
        return False

    if len(candidates) >= 2:
        codes_str = ", ".join(c["code"] for c in candidates)
        _send_admin_alert(
            f"⚠️ GIFT AMBIGUOUS: e-Transfer ${amount:.2f} received, "
            f"{len(candidates)} pending gift certs match — confirm manually:\n"
            f"{codes_str}\n"
            f"Message ID: {message_id}\n"
            f"Review: /admin/gift-pending"
        )
        print(f"   ⚠️ Gift amount-only fallback: ${amount:.2f} matches {len(candidates)} pending certs — admin alerted")
        # Return False so the email is NOT marked processed; booking pipeline continues.
        return False

    # Exactly one match — auto-confirm.
    cert_row = candidates[0]
    cert_code = cert_row["code"]
    if not _apply_gift_payment_transaction(
        gift_db,
        message_id,
        cert_code,
        amount,
        ledger_id=ledger_id,
        unique_amount_match=True,
        email_received_at=email_received_at,
    ):
        print(f"   [gift] Atomic activation did not claim {cert_code}")
        return "conflict"

    cert = gift_db.get_gift_certificate(cert_code)
    pdf_path = None
    try:
        pdf_path = save_gift_pdf(cert)
        gift_db.update_gift_pdf(cert_code, pdf_path)
        cert = gift_db.get_gift_certificate(cert_code) or cert
    except Exception as exc:
        print(f"   [gift] PDF generation failed for {cert_code}: {exc}")

    send_buyer_email(cert, pdf_path=pdf_path)
    if cert.get("recipient_email"):
        send_recipient_email(cert)
    _send_admin_alert(
        f"Gift certificate paid and activated (amount-only match): {cert_code}\n"
        f"Amount: ${amount:.2f}\n"
        f"Buyer: {cert.get('purchaser_name')} <{cert.get('purchaser_email')}>"
    )
    print(f"   ✅ CONFIRMED Gift certificate {cert_code} — ${amount:.2f} (amount-only fallback)")
    return "committed"


def get_emails(page_size=None, lookback_days=None):
    """Fetch recent incoming Interac emails via Himalaya CLI.

    Guardrails for production:
    - filter on the Gmail server instead of listing the entire INBOX;
    - only one Himalaya fetch may run at a time;
    - recent successful results are cached briefly;
    - timeout is short so Gmail slowness cannot make Fly mark the app unhealthy.
    """
    page_size = int(page_size or ETRANSFER_EMAIL_PAGE_SIZE)
    lookback_days = max(1, int(lookback_days or ETRANSFER_EMAIL_LOOKBACK_DAYS))
    now = time.time()
    cached = _EMAIL_FETCH_CACHE
    if (
        cached.get("emails") is not None
        and cached.get("page_size") == page_size
        and cached.get("lookback_days") == lookback_days
        and now - float(cached.get("ts") or 0) <= _EMAIL_FETCH_CACHE_TTL
    ):
        _set_email_fetch_status("cache_hit")
        return cached.get("emails")

    if not _EMAIL_FETCH_LOCK.acquire(blocking=False):
        print("[himalaya] envelope list skipped: another fetch is already running")
        _set_email_fetch_status(
            "busy_cached" if cached.get("emails") is not None else "busy_no_cache"
        )
        return cached.get("emails")

    try:
        after_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
        base_command = [
            "himalaya", "envelope", "list",
            "--quiet",
            "--folder", "INBOX",
            "--page-size", str(page_size),
            "-o", "json",
            "from", "interac.ca",
            "and", "after", after_date,
        ]
        commands = [
            base_command + ["order", "by", "date", "desc"],
            base_command,
        ]
        try:
            result = None
            for cmd in commands:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=ETRANSFER_EMAIL_TIMEOUT)
                if result.returncode == 0:
                    break
            if result is None or result.returncode != 0:
                rc = result.returncode if result else "?"
                detail = ((result.stderr if result else "") or "").strip()[-300:]
                print(f"[himalaya] filtered Interac envelope list failed: rc={rc} {detail}")
                _set_email_fetch_status("envelope_fetch_failed")
                return None
            raw = result.stdout.strip()
            if not raw:
                emails = []
                _EMAIL_FETCH_CACHE.update({
                    "ts": time.time(),
                    "page_size": page_size,
                    "lookback_days": lookback_days,
                    "emails": emails,
                })
                _set_email_fetch_status("fresh")
                return emails
            try:
                parsed = json.loads(raw)
                emails = parsed if isinstance(parsed, list) else []
                _EMAIL_FETCH_CACHE.update({
                    "ts": time.time(),
                    "page_size": page_size,
                    "lookback_days": lookback_days,
                    "emails": emails,
                })
                _set_email_fetch_status("fresh")
                return emails
            except json.JSONDecodeError:
                pass
            lines = raw.split("\n")
            json_line = None
            for line in lines:
                line = line.strip()
                if line.startswith("[") or line.startswith("{"):
                    json_line = line
                    break
            emails = [] if not json_line else json.loads(json_line)
            _EMAIL_FETCH_CACHE.update({
                "ts": time.time(),
                "page_size": page_size,
                "lookback_days": lookback_days,
                "emails": emails,
            })
            _set_email_fetch_status("fresh")
            return emails
        except Exception as e:
            print(f"[himalaya] Error fetching emails: {e}")
            _set_email_fetch_status("envelope_fetch_failed")
            return None
    finally:
        _EMAIL_FETCH_LOCK.release()


def _sender_domain(raw_sender):
    """Extract the domain of the actual email address from a sender string."""
    addr = parseaddr(str(raw_sender or ""))[1].strip().lower()
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[1]


def is_etransfer_email(email):
    """Check if an envelope is an incoming Interac payment notification.

    Interac also sends outgoing-transfer notifications. Those must never
    confirm a client booking merely because the amount happens to match.

    The sender check is strict: the parsed address domain must BE interac.ca
    or end with .interac.ca. A substring match would accept spoofed senders
    like notify@interac.ca.evil.com or "interac.ca"@evil.com.
    """
    subject = email.get("subject", "").lower()
    domain = _sender_domain(email.get("from", {}).get("addr", ""))

    incoming_markers = [
        "you've received",
        "you have received",
        "funds deposited",
        "funds have been deposited",
        "automatically deposited",
        "transfer received",
        "deposit received",
    ]

    is_incoming = any(marker in subject for marker in incoming_markers)
    from_interac = domain == "interac.ca" or domain.endswith(".interac.ca")

    return bool(from_interac and is_incoming)


def read_message_body(email_id):
    """Return normalized plain text for a Himalaya message id."""
    try:
        result = subprocess.run(
            ["himalaya", "message", "read", str(email_id), "--quiet", "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                return parsed
            if isinstance(parsed, dict):
                return parsed.get("text", {}).get("plain", "") or parsed.get("body", "") or str(parsed)
        except json.JSONDecodeError:
            pass
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith('"') or line.startswith("{"):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, str):
                        return parsed
                    if isinstance(parsed, dict):
                        return parsed.get("text", {}).get("plain", "") or parsed.get("body", "") or str(parsed)
                except json.JSONDecodeError:
                    continue
        return raw
    except Exception as e:
        print(f"Error reading email {email_id}: {e}")
        return None


def extract_payment_info(body_text):
    """Extract payment amount from email body."""
    if not body_text:
        return None

    patterns = [
        # Interac subjects/bodies often contain: "You've received $3.00" or
        # "Funds Deposited! $95.00". These are regexes, so use single
        # backslashes in raw strings. Double-escaping `\\$?` makes `$` an
        # end-of-string anchor and crashes with "nothing to repeat".
        r"\$([0-9,]+\.\d{2})",
        r"amount:\s*\$?([0-9,]+\.?\d*)",
        r"for the amount of\s*\$?([0-9,]+\.?\d*)",
        r"sent you\s*\$?([0-9,]+\.?\d*)",
        r"received\s*\$?([0-9,]+\.?\d*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, body_text, re.I)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except Exception:
                pass
    return None


def clean_sender_name(raw):
    """Normalize an Interac sender name, stripping subject-parse artifacts like
    a trailing 'and it' (from '...from NAME and it has been deposited')."""
    if not raw:
        return ""
    s = re.sub(r"\s+and\s+it\s*$", "", str(raw).strip(), flags=re.I)
    s = re.sub(r"\s+and\s*$", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip()
    # ALL-CAPS or all-lower bank names read nicer in Title Case; leave mixed case alone.
    return s.title() if (s.isupper() or s.islower()) else s


def extract_etransfer_details(body_text):
    """Pull reference number, sender name, and memo from an Interac e-Transfer body."""
    details = {"reference_number": None, "sender_name": None, "memo": None}
    if not body_text:
        return details
    m = re.search(r"Reference\s*Number[:\s]*([A-Za-z0-9]+)", body_text, re.I)
    if m:
        details["reference_number"] = m.group(1).strip()
    m = re.search(r"Sent\s*From[:\s]*([^\n\r]+)", body_text, re.I)
    if not m:
        m = re.search(r"received\s*\$?[0-9,]+\.?\d*\s*from\s+([^\n\r]+?)(?:\s+and\s+it\b|\.|\n|$)", body_text, re.I)
    if m:
        details["sender_name"] = clean_sender_name(m.group(1))
    m = re.search(r"Message[:\s]*([^\n\r]+)", body_text, re.I)
    if m:
        details["memo"] = m.group(1).strip()
    return details


def record_etransfer(reference_number=None, message_id=None, sender_name=None, amount=None,
                     memo=None, email_date=None, direction="in", source="email",
                     matched_booking_id=None, status=None):
    """Insert/UPSERT a transfer into the etransfers ledger.

    Keyed on reference_number (falls back to a message-id-based key) so repeated
    scans of the same email/CSV row never create duplicates. Returns the exact
    ledger row id so the financial transaction can claim that row, not a later
    message-id lookup.
    """
    ref = reference_number or (f"msg:{message_id}" if message_id else None)
    if not ref:
        return None
    if status is None:
        status = "matched" if matched_booking_id else "unmatched"
    conn = get_db()
    c = conn.cursor()
    try:
        _ensure_etransfer_claim_schema(conn)
        if message_id:
            existing = c.execute(
                """
                SELECT id
                  FROM etransfers
                 WHERE message_id=?
                 ORDER BY id
                 LIMIT 2
                """,
                (message_id,),
            ).fetchall()
            if len(existing) > 1:
                conn.rollback()
                return None
            if existing:
                ledger_id = existing[0]["id"]
                c.execute(
                    """
                    UPDATE etransfers
                       SET sender_name=COALESCE(?, sender_name),
                           amount=COALESCE(amount, ?),
                           memo=COALESCE(memo, ?),
                           email_date=COALESCE(email_date, ?)
                     WHERE id=?
                    """,
                    (sender_name, amount, memo, email_date, ledger_id),
                )
                conn.commit()
                return ledger_id
        try:
            c.execute("""
                INSERT INTO etransfers (
                    reference_number, message_id, sender_name, amount, memo,
                    direction, email_date, matched_booking_id, status, source
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                ref, message_id, sender_name, amount, memo, direction,
                email_date, matched_booking_id, status, source,
            ))
            ledger_id = c.lastrowid
        except sqlite3.IntegrityError:
            # Already in the ledger — fill in any blanks, never wipe existing data.
            c.execute("""
                UPDATE etransfers
                   SET message_id=COALESCE(?, message_id),
                       sender_name=COALESCE(?, sender_name),
                       amount=COALESCE(amount, ?),
                       memo=COALESCE(memo, ?),
                       email_date=COALESCE(email_date, ?)
                 WHERE reference_number=?
            """, (message_id, sender_name, amount, memo, email_date, ref))
            if matched_booking_id:
                c.execute("""
                    UPDATE etransfers SET matched_booking_id=?, status='matched'
                     WHERE reference_number=?
                       AND (matched_booking_id IS NULL OR matched_booking_id=?)
                """, (matched_booking_id, ref, matched_booking_id))
            row = c.execute(
                "SELECT id FROM etransfers WHERE reference_number=?",
                (ref,),
            ).fetchone()
            ledger_id = row["id"] if row else None
        conn.commit()
        return ledger_id
    finally:
        conn.close()


def get_expected_amount_for_booking(booking_id):
    """Get expected deposit amount for a specific booking.

    Priority:
    1. bookings.deposit_amount column (stored at reserve time — most reliable)
    2. events.yaml lookup by event_id (fallback for older rows)
    3. None — the booking must be skipped by amount matching.

    Earlier versions fell back to a hardcoded $95.00. With differently priced
    events that default could silently confirm the wrong booking, so an
    unknown deposit now logs a warning and excludes the booking instead.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT event_id, deposit_amount FROM bookings WHERE id=?", (booking_id,))
    row = c.fetchone()
    conn.close()

    if row:
        # Best case: amount was stored at booking time
        stored = row["deposit_amount"]
        if stored is not None:
            return float(stored)

    event_id = row["event_id"] if row else None

    try:
        import yaml
        if os.path.exists(EVENTS_YAML_PATH):
            with open(EVENTS_YAML_PATH) as f:
                data = yaml.safe_load(f)
            events = data.get("events", [])
            for ev in events:
                if ev.get("id") == event_id:
                    deposit = ev.get("deposit")
                    if deposit is not None:
                        return float(deposit)
    except Exception:
        pass

    print(f"   ⚠️ No deposit amount for booking #{booking_id} "
          f"(event_id={event_id!r} not in events.yaml) — excluded from amount matching")
    return None


def get_pending_bookings(within_minutes=30, pending_payment_hours=24, private_days=45):
    """Get bookings awaiting payment, created within recent window.

    Timezone contract: every cutoff is computed in UTC. created_at comes from
    SQLite CURRENT_TIMESTAMP (UTC, naive); reserved_until is written by app.py
    with an America/Edmonton UTC offset, so it is parsed and compared as a real
    instant in Python instead of lexically inside SQL.

    NOTE on the removed grace window: an earlier version also kept 'reserved'
    rows matchable for 60 minutes after reserved_until passed (Interac emails
    can lag 10-40 min behind the payment). That grace was dead code — the
    watcher's expire_reservations() sweep flips those rows to status='expired'
    within ~30 seconds of reserved_until passing, so they could never match
    here. It was removed rather than made real: making it real would hold every
    abandoned slot for an extra hour and hurt conversion. The actual
    protections are (a) clicking "I paid" moves the booking to pending_payment
    with a {pending_payment_hours}h window, and (b) a payment that arrives with
    no eligible pending booking alerts the admin instead of being dropped.

    private_days: admin-created private sessions have no short reservation
    window (reserved_until is NULL) and the payment link is emailed — the
    client may pay days later. Keep them matchable for this many days in
    either reserved or pending_payment state. Exact-amount matching plus the
    ambiguity guard keep this safe despite the long window.

    within_minutes is kept for API compatibility; live 'reserved' rows are
    gated by reserved_until > now, with a generous created_at bound so that
    admin manual blocks (1h reservation window) stay matchable too.
    """
    conn = get_db()
    c = conn.cursor()
    now = _utc_now()
    # 'reserved' rows are bounded by the live reserved_until instant below; the
    # created_at bound only guards against pathological rows with a far-future
    # reserved_until.
    reserved_created_cutoff = (now - timedelta(hours=pending_payment_hours)).strftime('%Y-%m-%d %H:%M:%S')
    pending_cutoff = (now - timedelta(hours=pending_payment_hours)).strftime('%Y-%m-%d %H:%M:%S')
    private_cutoff = (now - timedelta(days=private_days)).strftime('%Y-%m-%d %H:%M:%S')
    c.execute("""
        SELECT * FROM bookings
        WHERE confirmed = 0
        AND paid = 0
        AND (
            (status = 'reserved' AND reserved_until IS NOT NULL AND created_at > ?)
            OR
            (status = 'pending_payment' AND created_at > ?)
            OR
            (session_type = 'private'
             AND status IN ('reserved', 'pending_payment')
             AND created_at > ?)
        )
        ORDER BY created_at DESC
    """, (reserved_created_cutoff, pending_cutoff, private_cutoff))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    # reserved_until may carry a timezone offset — compare instants in Python.
    eligible = []
    for row in rows:
        if row.get("status") == "reserved" and (row.get("session_type") or "") != "private":
            reserved_until = _parse_db_datetime(row.get("reserved_until"))
            if reserved_until is None or reserved_until <= now:
                continue
        eligible.append(row)
    return eligible


def get_reconciliation_bookings(within_days=45):
    """Confirmed bookings that can still receive a corrected Interac amount.

    These are not candidates for auto-confirming; they are candidates for
    raising paid_amount when an Interac email arrives late or after a manual
    confirmation. Keep the window narrow enough to avoid matching old inbox
    history forever, but wide enough for future sessions already booked.
    """
    conn = get_db()
    c = conn.cursor()
    now = _utc_now()
    # Session dates are local-calendar dates; using the UTC date for a 7-day
    # heuristic window is off by at most one day, which the window absorbs.
    date_cutoff = (now.date() - timedelta(days=7)).strftime("%Y-%m-%d")
    created_cutoff = (now - timedelta(days=within_days)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        SELECT * FROM bookings
        WHERE status='confirmed'
          AND confirmed=1
          AND paid=1
          AND date >= ?
          AND created_at >= ?
        ORDER BY date ASC, time ASC
    """, (date_cutoff, created_cutoff))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _get_booking_full_price(booking):
    """Full session price for a booking (balance + overpayment cap).
    Reads events.yaml by event_id; falls back to a safe default."""
    try:
        stored = booking.get("full_price")
        if stored is not None:
            stored = float(stored)
            if stored > 0:
                return stored
    except Exception:
        pass
    try:
        import yaml
        event_id = booking.get("event_id")
        if event_id and os.path.exists(EVENTS_YAML_PATH):
            with open(EVENTS_YAML_PATH) as f:
                data = yaml.safe_load(f)
            for ev in (data.get("events", []) or []):
                if ev.get("id") == event_id:
                    fp = ev.get("full_price")
                    if fp is not None:
                        return float(fp)
    except Exception:
        pass
    return 190.0


def match_by_amount_only(amount, bookings):
    """Match an e-Transfer to a pending booking by amount (asymmetric, money-safe).

    Bands:
      1. Exact (within $0.01)                     -> 'exact'     -> auto-confirm
      2. Overpaid (> expected, up to full price)  -> 'overpaid'  -> auto-confirm (records actual)
      3. Underpaid (0.3x .. 1x expected)          -> 'underpaid' -> record only, DO NOT confirm
      4. Otherwise                                -> orphan

    An auto-action fires ONLY when exactly one booking is in the chosen band. If
    two or more sit in the same band the result is ambiguous and goes to manual
    review — we never silently confirm the wrong booking.

    Returns (booking, ambiguity_list, match_type) where
      match_type = 'exact' | 'overpaid' | 'underpaid' | None
    """
    exact, overpaid, underpaid = [], [], []
    for b in bookings:
        expected = get_expected_amount_for_booking(b["id"])
        if expected is None:
            # Unknown deposit (no stored amount, event not in events.yaml) —
            # never guess; the booking stays manual.
            continue
        diff = amount - expected
        if abs(diff) < 0.01:
            exact.append(b)
        elif diff > 0:
            full = _get_booking_full_price(b)
            cap = full if full > expected else expected * 2
            if amount <= cap + 1.0:  # generous, but never beyond the full session price
                overpaid.append((b, diff))
        elif diff < 0 and amount >= expected * 0.3:
            underpaid.append((b, abs(diff)))

    # 1) Exact normally wins — but when the same amount is ALSO a plausible
    #    overpayment for another booking, the winner is not certain: the payer
    #    may be the other client paying their full price. Cross-band collisions
    #    go to disambiguation (body match / manual) instead of auto-confirm.
    if exact:
        cross_band = exact + [b for b, _ in overpaid]
        if len(cross_band) == 1:
            return exact[0], [], 'exact'
        return None, cross_band, None  # ambiguous — multiple exact, or exact+overpaid collision

    # 2) Overpaid -> auto-confirm, but only if unambiguous
    if overpaid:
        if len(overpaid) > 1:
            return None, [b for b, _ in overpaid], None
        return overpaid[0][0], [], 'overpaid'

    # 3) Underpaid -> record only, but only if unambiguous
    if underpaid:
        if len(underpaid) > 1:
            return None, [b for b, _ in underpaid], None
        return underpaid[0][0], [], 'underpaid'

    return None, [], None


def confirm_booking(
    booking_id,
    paid_amount=None,
    message_id=None,
    *,
    ledger_id=None,
):
    """Confirm booking in DB.

    Only flips rows that are still unconfirmed: two same-amount Interac emails
    in one scan batch must not both confirm (and overwrite paid_amount on) the
    same booking. The second email then correctly falls through to the orphan
    path instead of being silently swallowed.
    """
    if message_id is not None:
        return _apply_booking_payment_transaction(
            message_id,
            booking_id,
            paid_amount,
            "confirm",
            ledger_id=ledger_id,
        )
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE bookings
        SET confirmed=1, paid=1, status='confirmed', paid_amount=?
        WHERE id=? AND confirmed=0
    """, (paid_amount, booking_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def reconcile_confirmed_payment(
    booking_id,
    paid_amount,
    message_id=None,
    *,
    allow_existing_orphan=False,
    ledger_id=None,
):
    """Raise paid_amount for an already-confirmed booking.

    Never lowers money, never changes date/time/client/status, and never sends
    client email. This is for delayed Interac messages correcting an amount
    that was previously entered as the standard deposit.
    """
    if message_id is not None:
        return _apply_booking_payment_transaction(
            message_id,
            booking_id,
            paid_amount,
            "reconcile",
            allow_existing_orphan=allow_existing_orphan,
            ledger_id=ledger_id,
        )
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE bookings
        SET paid=1, paid_amount=?
        WHERE id=?
          AND confirmed=1
          AND status='confirmed'
          AND (paid_amount IS NULL OR paid_amount < ?)
    """, (paid_amount, booking_id, paid_amount - 0.009))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


_MONTHS = {
    1: ("jan", "january"),
    2: ("feb", "february"),
    3: ("mar", "march"),
    4: ("apr", "april"),
    5: ("may",),
    6: ("jun", "june"),
    7: ("jul", "july"),
    8: ("aug", "august"),
    9: ("sep", "sept", "september"),
    10: ("oct", "october"),
    11: ("nov", "november"),
    12: ("dec", "december"),
}


def _body_has_booking_date(body, booking):
    raw = booking.get("date")
    if not raw:
        return False
    text = (body or "").lower()
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return raw.lower() in text
    day = dt.day
    suffix = "th"
    if day % 10 == 1 and day != 11:
        suffix = "st"
    elif day % 10 == 2 and day != 12:
        suffix = "nd"
    elif day % 10 == 3 and day != 13:
        suffix = "rd"
    variants = {
        raw.lower(),
        f"{dt.month}/{day}",
        f"{dt.month:02d}/{day:02d}",
        f"{day}/{dt.month}",
        f"{day:02d}/{dt.month:02d}",
    }
    for month in _MONTHS.get(dt.month, ()):
        variants.add(f"{month} {day}")
        variants.add(f"{month} {day}{suffix}")
        variants.add(f"{day} {month}")
    return any(v in text for v in variants)


def _body_has_booking_time(body, booking):
    raw = booking.get("time")
    if not raw:
        return False
    text = (body or "").lower().replace(".", "")
    try:
        hh, mm = [int(x) for x in raw.split(":", 1)]
    except Exception:
        return raw.lower() in text
    hour12 = hh % 12 or 12
    ampm = "am" if hh < 12 else "pm"
    variants = {
        f"{hh:02d}:{mm:02d}",
        f"{hh}:{mm:02d}",
        f"{hour12}:{mm:02d}",
        f"{hour12}:{mm:02d} {ampm}",
        f"{hour12}:{mm:02d}{ampm}",
    }
    if mm == 0:
        variants.update({f"{hour12} {ampm}", f"{hour12}{ampm}", f"{hh:02d}:00"})
    return any(v in text for v in variants)


def _body_has_booking_name(body, booking):
    text = re.sub(r"[^a-z0-9а-яіїєґё]+", " ", (body or "").lower())
    tokens = [
        t for t in re.split(r"\s+", (booking.get("name") or "").lower())
        if len(t) >= 3
    ]
    if not tokens:
        return False
    return sum(1 for token in tokens if token in text.split()) >= min(2, len(tokens))


def match_reconciliation_payment(amount, bookings, body):
    """Find one confirmed booking whose Interac email strongly identifies it."""
    matches = []
    for b in bookings:
        try:
            current_paid = float(b.get("paid_amount") or 0)
        except Exception:
            current_paid = 0.0
        if amount <= current_paid + 0.009:
            continue

        expected = get_expected_amount_for_booking(b["id"])
        if expected is None:
            continue
        full = _get_booking_full_price(b)
        cap = full if full > expected else expected * 2
        if amount < expected * 0.3 or amount > cap + 1.0:
            continue

        name_ok = _body_has_booking_name(body, b)
        date_ok = _body_has_booking_date(body, b)
        time_ok = _body_has_booking_time(body, b)
        score = sum([name_ok, date_ok, time_ok])
        if score >= 2:
            matches.append((score, b))

    if not matches:
        return None, []
    matches.sort(key=lambda item: item[0], reverse=True)
    best_score = matches[0][0]
    best = [b for score, b in matches if score == best_score]
    if len(best) == 1:
        return best[0], []
    return None, best


def match_ambiguous_pending_by_body(bookings, body):
    """Resolve an amount collision only when the Interac body is strongly unique.

    Mini-session clients commonly owe the same deposit. Name/date/time can
    safely distinguish them, but weak or tied matches must remain manual.
    """
    matches = []
    for booking in bookings:
        score = sum([
            _body_has_booking_name(body, booking),
            _body_has_booking_date(body, booking),
            _body_has_booking_time(body, booking),
        ])
        if score >= 2:
            matches.append((score, booking))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    best_score = matches[0][0]
    best = [booking for score, booking in matches if score == best_score]
    return best[0] if len(best) == 1 else None


def _classify_amount_for_booking(amount, booking):
    expected = get_expected_amount_for_booking(booking["id"])
    if expected is None:
        return None
    diff = amount - expected
    if abs(diff) < 0.01:
        return "exact"
    if diff > 0:
        full = _get_booking_full_price(booking)
        cap = full if full > expected else expected * 2
        return "overpaid" if amount <= cap + 1.0 else None
    if amount >= expected * 0.3:
        return "underpaid"
    return None


def _notify_admin_ambiguity(amount, candidates):
    """Send admin notification when amount matches multiple bookings.

    Deduped per (amount, candidate set): the watcher re-scans every ~60s and an
    unresolved collision must not re-fire the same Telegram alert each pass.
    """
    key = ("ambiguity", f"{amount:.2f}",
           tuple(sorted(str(b.get("id")) for b in candidates)))
    if not _should_send_alert(key):
        print(f"[admin] Ambiguity alert for ${amount:.2f} suppressed (sent recently)")
        return
    try:
        lines = [f"⚠️ **Ambiguous payment: ${amount:.2f}**",
                 f"Matches {len(candidates)} bookings:", ""]
        for b in candidates:
            lines.append(f"  • #{b['id']} {b['name']} @ {b['date']} {b['time']} ({b['email']})")
        lines.append("")
        lines.append("Please manually confirm the correct booking.")
        _send_admin_alert("\n".join(lines))
        print(f"[admin] Ambiguity alert sent for ${amount:.2f}")
    except Exception as e:
        print(f"[admin] Failed to send ambiguity alert: {e}")


def record_partial_payment(
    booking_id,
    paid_amount,
    message_id=None,
    *,
    ledger_id=None,
):
    """Record a partial/underpayment without confirming the booking.
    
    Updates paid_amount on the booking but keeps confirmed=0, paid=0.
    Sets status to 'partial_payment' so admin can see it needs attention.
    
    Returns True if updated, False if booking is already confirmed or not found.
    """
    if message_id is not None:
        return _apply_booking_payment_transaction(
            message_id,
            booking_id,
            paid_amount,
            "partial",
            ledger_id=ledger_id,
        )
    conn = get_db()
    c = conn.cursor()
    # Check if booking is already confirmed or paid
    c.execute("SELECT confirmed, paid FROM bookings WHERE id=?", (booking_id,))
    row = c.fetchone()
    if not row or row["confirmed"] or row["paid"]:
        conn.close()
        return False
    
    c.execute("""
        UPDATE bookings
        SET paid_amount=?, status='partial_payment'
        WHERE id=?
    """, (paid_amount, booking_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def _notify_admin_orphan(amount, body, msg_id, reason=None):
    """Send admin notification when e-Transfer has no matching pending booking.

    Deduped per message id — an unmatched payment that stays unmatched must not
    page the admin on every watcher pass.
    """
    if not _should_send_alert(("orphan", str(msg_id))):
        print(f"[admin] Orphan alert for message {msg_id} suppressed (sent recently)")
        return
    try:
        # Extract sender info from body for context
        snippet = (body or "")[:500].replace('\n', ' ').strip()
        lines = [
            f"💸 **Orphan payment: ${amount:.2f}**",
            reason or "No pending booking matches this amount.",
            f"",
            f"*Email snippet:*",
            f"```",
            f"{snippet[:300]}",
            f"```",
            f"",
            f"Message ID: `{msg_id}`",
            f"Action needed: check if client paid without booking, or booking expired."
        ]
        _send_admin_alert("\n".join(lines))
        print(f"[admin] Orphan alert sent for ${amount:.2f}")
    except Exception as e:
        print(f"[admin] Failed to send orphan alert: {e}")


def _notify_admin_overpaid(booking, expected, actual):
    """Notify admin: client paid more than expected — auto-confirmed."""
    try:
        from app import _notify_admin
        diff = actual - expected
        total = _get_booking_full_price(booking)
        balance = max(total - actual, 0)
        lines = [
            f"💰 **Payment received (overpaid)**",
            f"",
            f"Booking #{booking['id']} — {booking.get('name', '?')}",
            f"Expected: ${expected:.2f}",
            f"Received: ${actual:.2f}  (+${diff:.2f})",
            f"",
            f"✅ Auto-confirmed",
            f"Remaining balance: ${balance:.2f}",
        ]
        _notify_admin("\n".join(lines))
        print(f"[admin] Overpaid alert sent for #{booking['id']} (${actual:.2f})")
    except Exception as e:
        print(f"[admin] Failed to send overpaid alert: {e}")


def _notify_admin_underpaid(booking, expected, actual):
    """Notify admin: client paid less than expected — NOT confirmed."""
    try:
        from app import _notify_admin
        diff = expected - actual
        lines = [
            f"⚠️ **Payment received (UNDERPAID)**",
            f"",
            f"Booking #{booking['id']} — {booking.get('name', '?')}",
            f"Expected: ${expected:.2f}",
            f"Received: ${actual:.2f}  (-${diff:.2f})",
            f"",
            f"❌ NOT confirmed — recorded as partial_payment.",
            f"Action: confirm manually or contact the client.",
        ]
        _notify_admin("\n".join(lines))
        print(f"[admin] Underpaid alert sent for #{booking['id']} (${actual:.2f})")
    except Exception as e:
        print(f"[admin] Failed to send underpaid alert: {e}")


def _notify_admin_reconciled(booking, previous, actual):
    """Notify admin that a later Interac email corrected paid_amount."""
    try:
        from app import _notify_admin
        total = _get_booking_full_price(booking)
        balance = max(total - actual, 0)
        lines = [
            f"✅ **Payment amount corrected from Interac**",
            f"",
            f"Booking #{booking['id']} — {booking.get('name', '?')}",
            f"Previous paid amount: ${previous:.2f}",
            f"Interac amount: ${actual:.2f}",
            f"Remaining balance now: ${balance:.2f}",
        ]
        _notify_admin("\n".join(lines))
        print(f"[admin] Reconciled paid_amount for #{booking['id']}: ${previous:.2f} -> ${actual:.2f}")
    except Exception as e:
        print(f"[admin] Failed to send reconciliation alert: {e}")


def _parse_email_datetime(value):
    """Parse Himalaya envelope date to naive UTC datetime.

    Example from Gmail/Himalaya: "2026-05-10 15:19+00:00".
    Return None if parsing fails; callers should fail closed for safety.
    """
    if not value:
        return None
    text = str(value).strip()
    candidates = [text]
    # Python accepts ISO offsets better with a T separator.
    if " " in text:
        candidates.append(text.replace(" ", "T", 1))
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        pass
    return None


def _parse_db_datetime(value):
    """Parse SQLite datetime strings to naive UTC.

    created_at is naive UTC (SQLite CURRENT_TIMESTAMP); reserved_until is
    written with an America/Edmonton UTC offset. Timezone-aware values are
    converted to UTC and naive values are assumed to already be UTC, so every
    comparison in this module happens on one clock.
    """
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("T", " ")):
        dt = None
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            try:
                dt = datetime.strptime(candidate.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _filter_bookings_created_before_email(email, bookings, body=None):
    """Prevent stale e-Transfer emails from confirming future bookings."""
    email_dt = _parse_email_datetime(email.get("date"))
    if email_dt is None:
        # Fail closed: if we cannot trust the email timestamp, do not match it.
        print(f"   [skip] Could not parse email date for message {email.get('id')}")
        return []
    safe = []
    for booking in bookings:
        created_dt = _parse_db_datetime(booking.get("created_at"))
        if created_dt is None:
            continue
        # Allow a small clock-skew grace window, but reject old emails for new bookings.
        if created_dt <= email_dt + timedelta(minutes=2):
            safe.append(booking)
            continue
        # Some clients pay first, then finish the booking form. Permit that
        # only when the Interac message strongly identifies this exact booking.
        if body and created_dt <= email_dt + timedelta(hours=24):
            score = sum([
                _body_has_booking_name(body, booking),
                _body_has_booking_date(body, booking),
                _body_has_booking_time(body, booking),
            ])
            if score >= 2:
                safe.append(booking)
    return safe


def check_single_email(email, bookings, reconciliation_bookings=None):
    """Process one email against current pending bookings.
    Returns (confirmed_booking_id, ambiguity_list) or (None, None)."""
    msg_id = str(email.get("id", ""))
    if not msg_id:
        return None, None

    def archived_at_payment_boundary():
        if not is_etransfer_archived(msg_id):
            return False
        print(f"   [skip] Message {msg_id} is archived in the transfer ledger")
        return True

    if archived_at_payment_boundary():
        return None, None

    reconciliation_bookings = reconciliation_bookings or []
    processed = get_processed_email(msg_id)
    if processed and processed.get("booking_id") is not None:
        # Already-linked messages have completed their payment side effects.
        # Avoid re-reading every old Gmail body on every watcher pass.
        print(f"   [skip] Message {msg_id} already linked to booking #{processed.get('booking_id')}")
        return None, None
    if processed and not reconciliation_bookings:
        print(f"   [skip] Message {msg_id} already processed; no reconciliation candidates")
        return None, None

    body = read_message_body(msg_id)
    if not body:
        _record_body_read_failure()
        return None, None

    amount = extract_payment_info(body)
    if amount is None:
        print(f"   [skip] Could not extract amount from message {msg_id}")
        return None, None

    print(f"   💰 Extracted amount: ${amount:.2f}")

    # Ledger: record every incoming transfer (even if it never matches a booking).
    ledger_id = None
    try:
        _d = extract_etransfer_details(body)
        ledger_id = record_etransfer(
            reference_number=_d.get("reference_number"),
            message_id=msg_id,
            sender_name=_d.get("sender_name"),
            amount=amount,
            memo=_d.get("memo"),
            email_date=str(email.get("date") or ""),
            source="email",
        )
    except Exception as _e:
        print(f"   [ledger] record failed: {_e}")
    if ledger_id is None:
        print(f"   [skip] Message {msg_id} has no unique active ledger owner")
        return None, None

    # Body retrieval is slow external IO. Re-check after it, at the last shared
    # boundary before any gift, reconciliation, or booking payment mutation.
    if archived_at_payment_boundary():
        return None, None

    _email_received_at = _parse_email_datetime(email.get("date"))
    if not processed:
        gift_outcome = try_confirm_gift_etransfer(
            amount,
            body,
            msg_id,
            email_received_at=_email_received_at,
            ledger_id=ledger_id,
        )
        if gift_outcome == "committed" or gift_outcome == "conflict":
            return None, None
        if gift_outcome:
            mark_message_processed(msg_id, None, amount)
            return None, None

    if processed:
        # Do not re-confirm/re-run side effects. The one safe exception is
        # money reconciliation: an earlier run may have recorded the Interac
        # email as orphan/processed before a manual confirmation existed.
        recon_candidates = _filter_bookings_created_before_email(email, reconciliation_bookings, body)
        processed_booking_id = processed.get("booking_id")
        if processed_booking_id:
            recon_candidates = [
                b for b in recon_candidates
                if str(b.get("id")) == str(processed_booking_id)
            ]
        reconciled, recon_ambiguous = match_reconciliation_payment(amount, recon_candidates, body)
        if recon_ambiguous:
            print(f"   ⚠️ Reconciliation ambiguity: ${amount:.2f} matches {len(recon_ambiguous)} confirmed bookings")
            _notify_admin_ambiguity(amount, recon_ambiguous)
            return None, recon_ambiguous
        if reconciled is not None:
            previous = float(reconciled.get("paid_amount") or 0)
            if archived_at_payment_boundary():
                return None, None
            if reconcile_confirmed_payment(
                reconciled["id"],
                amount,
                msg_id,
                allow_existing_orphan=True,
                ledger_id=ledger_id,
            ):
                _notify_admin_reconciled(reconciled, previous, amount)
                print(f"   ✅ RECONCILED processed message {msg_id}: Booking #{reconciled['id']} ${previous:.2f} → ${amount:.2f}")
            return None, None
        print(f"   [skip] Message {msg_id} already processed")
        return None, None

    bookings = _filter_bookings_created_before_email(email, bookings or [], body)
    eligible_pending_count = len(bookings)

    matched, ambiguous, match_type = match_by_amount_only(amount, bookings) if bookings else (None, [], None)

    if ambiguous:
        strong_match = match_ambiguous_pending_by_body(ambiguous, body)
        if strong_match is not None:
            matched = strong_match
            match_type = _classify_amount_for_booking(amount, matched)
            ambiguous = []
            print(
                f"   ✅ Strong body match resolved amount collision: "
                f"Booking #{matched['id']} ({match_type})"
            )
        else:
            print(f"   ⚠️ Ambiguity: ${amount:.2f} matches {len(ambiguous)} bookings")
            _notify_admin_ambiguity(amount, ambiguous)
            # Do NOT mark ambiguous messages as processed. A later retry may
            # become unique after a temporary reservation is released.
            return None, ambiguous

    if matched is None:
        recon_candidates = _filter_bookings_created_before_email(email, reconciliation_bookings, body)
        reconciled, recon_ambiguous = match_reconciliation_payment(amount, recon_candidates, body)
        if recon_ambiguous:
            print(f"   ⚠️ Reconciliation ambiguity: ${amount:.2f} matches {len(recon_ambiguous)} confirmed bookings")
            _notify_admin_ambiguity(amount, recon_ambiguous)
            return None, recon_ambiguous
        if reconciled is not None:
            previous = float(reconciled.get("paid_amount") or 0)
            if archived_at_payment_boundary():
                return None, None
            if reconcile_confirmed_payment(
                reconciled["id"],
                amount,
                msg_id,
                ledger_id=ledger_id,
            ):
                _notify_admin_reconciled(reconciled, previous, amount)
                print(f"   ✅ RECONCILED Booking #{reconciled['id']} paid_amount ${previous:.2f} → ${amount:.2f}")
            return None, None

        if not eligible_pending_count:
            email_dt = _parse_email_datetime(email.get("date"))
            is_fresh = (
                email_dt is not None
                and _utc_now() - email_dt <= timedelta(hours=ETRANSFER_ORPHAN_FRESH_HOURS)
            )
            if is_fresh:
                # A payment just arrived and nothing can absorb it (expired
                # reservation, pay-without-booking, double payment). Silence
                # here used to hide lost money — alert the admin instead. The
                # message is NOT marked processed, so a booking completed
                # shortly after the payment (pay-first flow) can still match on
                # a later pass; the alert itself is deduped per message id.
                print(f"   ⚠️ Payment ${amount:.2f} has NO eligible pending bookings (message {msg_id}) — alerting admin")
                _notify_admin_orphan(
                    amount, body, msg_id,
                    reason=("Payment received but ZERO eligible pending bookings exist — "
                            "likely an expired reservation or a payment without a booking."),
                )
            else:
                print(f"   [skip] Stale message {msg_id}: no eligible pending bookings; no safe reconciliation match")
            return None, None

        print(f"   ❌ No booking matches ${amount:.2f}")
        # Orphan payment: e-Transfer received but no matching pending booking
        _notify_admin_orphan(amount, body, msg_id)
        mark_message_processed(msg_id, None, amount)
        return None, None

    expected = get_expected_amount_for_booking(matched["id"])

    if match_type in ('exact', 'overpaid'):
        # Auto-confirm with the ACTUAL amount received (balance = full_price - amount).
        if archived_at_payment_boundary():
            return None, None
        if confirm_booking(
            matched["id"],
            amount,
            msg_id,
            ledger_id=ledger_id,
        ):
            if match_type == 'overpaid':
                _notify_admin_overpaid(matched, expected, amount)
            print(f"   ✅ CONFIRMED Booking #{matched['id']} — ${amount:.2f} ({match_type})")
            return matched["id"], None
        print(f"   ❌ DB update failed for #{matched['id']}")
        return None, None

    if match_type == 'underpaid':
        # Record the payment but DO NOT confirm — admin handles it.
        if archived_at_payment_boundary():
            return None, None
        if record_partial_payment(
            matched["id"],
            amount,
            msg_id,
            ledger_id=ledger_id,
        ):
            _notify_admin_underpaid(matched, expected, amount)
            print(f"   ⚠️ UNDERPAID Booking #{matched['id']} — ${amount:.2f} (expected ${expected:.2f}); NOT confirmed")
            return None, None
        # Booking ownership changed or the row is no longer eligible. Leave the
        # message active so the coupled transaction can be retried or reviewed.
        print(f"   [skip] Could not record partial payment for #{matched['id']} (already confirmed?)")
        return None, None

    print(f"   ❌ Unhandled match_type for #{matched['id']}")
    return None, None


if __name__ == "__main__":
    # Standalone test mode
    emails = get_emails()
    if emails is None:
        print("Failed to fetch emails")
        sys.exit(1)
    etransfers = [e for e in emails if is_etransfer_email(e)]
    print(f"Found {len(etransfers)} e-Transfer emails")
    bookings = get_pending_bookings()
    print(f"Found {len(bookings)} pending bookings")
    for e in etransfers:
        check_single_email(e, bookings)
