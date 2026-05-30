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
"""

import os
import sys
import re
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "bookings.db"))
EVENTS_YAML_PATH = os.environ.get("EVENTS_YAML_PATH",
    os.path.join(os.path.dirname(__file__), "events.yaml"))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_message_processed(message_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM processed_emails WHERE message_id = ?", (message_id,))
    row = c.fetchone()
    conn.close()
    return row is not None


def mark_message_processed(message_id, booking_id, amount):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO processed_emails (message_id, booking_id, amount)
        VALUES (?, ?, ?)
    """, (message_id, booking_id, amount))
    conn.commit()
    conn.close()


def get_emails():
    """Fetch recent emails via Himalaya CLI."""
    try:
        result = subprocess.run(
            ["himalaya", "envelope", "list", "--folder", "INBOX", "-o", "json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"[himalaya] envelope list failed: rc={result.returncode}")
            return None
        lines = result.stdout.strip().split("\n")
        json_line = None
        for line in lines:
            line = line.strip()
            if line.startswith("[") or line.startswith("{"):
                json_line = line
                break
        if not json_line:
            return []
        return json.loads(json_line)
    except Exception as e:
        print(f"[himalaya] Error fetching emails: {e}")
        return None


def is_etransfer_email(email):
    """Check if email is an Interac e-Transfer notification."""
    subject = email.get("subject", "").lower()
    sender = email.get("from", {}).get("addr", "").lower()

    keywords = ["interac", "e-transfer", "etransfer", "transfer received", "deposit received"]
    sender_domains = ["interac.ca", "payments.interac.ca", "notify.interac.ca"]

    has_keyword = any(kw in subject for kw in keywords)
    from_interac = any(domain in sender for domain in sender_domains)

    return has_keyword or from_interac


def read_message_body(email_id):
    """Return normalized plain text for a Himalaya message id."""
    try:
        result = subprocess.run(
            ["himalaya", "message", "read", str(email_id), "-o", "json"],
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


def get_expected_amount_for_booking(booking_id):
    """Get expected deposit amount for a specific booking.

    Priority:
    1. bookings.deposit_amount column (stored at reserve time — most reliable)
    2. events.yaml lookup by event_id (fallback for older rows)
    3. Hard default $95.00
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

    return 95.0


def get_pending_bookings(within_minutes=30, grace_minutes=60):
    """Get bookings awaiting payment, created within recent window.

    grace_minutes: also include bookings whose reservation expired up to
    this many minutes ago.  Interac e-Transfer emails can arrive 10-40 min
    after the client sends the payment, so we must not cut off the search the
    moment reserved_until passes.
    """
    conn = get_db()
    c = conn.cursor()
    now = datetime.now()
    # Accept bookings that still have an active hold OR expired within the grace window
    grace_cutoff = (now - timedelta(minutes=grace_minutes)).strftime('%Y-%m-%d %H:%M:%S')
    created_cutoff = (now - timedelta(minutes=within_minutes + grace_minutes)).strftime('%Y-%m-%d %H:%M:%S')
    c.execute("""
        SELECT * FROM bookings
        WHERE status IN ('reserved', 'pending_payment')
        AND reserved_until > ?
        AND confirmed = 0
        AND paid = 0
        AND created_at > ?
        ORDER BY created_at DESC
    """, (grace_cutoff, created_cutoff))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _get_booking_full_price(booking):
    """Full session price for a booking (balance + overpayment cap).
    Reads events.yaml by event_id; falls back to a safe default."""
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

    # 1) Exact wins
    if exact:
        if len(exact) == 1:
            return exact[0], [], 'exact'
        return None, exact, None  # ambiguous — multiple exact matches

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


def confirm_booking(booking_id, paid_amount=None):
    """Confirm booking in DB."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE bookings
        SET confirmed=1, paid=1, status='confirmed', paid_amount=?
        WHERE id=?
    """, (paid_amount, booking_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def record_partial_payment(booking_id, paid_amount):
    """Record a partial/underpayment WITHOUT confirming the booking.

    Sets paid_amount + status='partial_payment', but ONLY while the booking is
    still unconfirmed (confirmed=0) — so a stray later e-Transfer can never flip
    an already-confirmed booking back to unpaid. Returns True if a row changed.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE bookings
        SET paid_amount=?, status='partial_payment'
        WHERE id=? AND confirmed=0
    """, (paid_amount, booking_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def _notify_admin_ambiguity(amount, candidates):
    """Send admin notification when amount matches multiple bookings."""
    # This will be called from timed_cron.py context where app functions are available
    try:
        from app import _tg_message
        lines = [f"⚠️ **Ambiguous payment: ${amount:.2f}**",
                 f"Matches {len(candidates)} bookings:", ""]
        for b in candidates:
            lines.append(f"  • #{b['id']} {b['name']} @ {b['date']} {b['time']} ({b['email']})")
        lines.append("")
        lines.append("Please manually confirm the correct booking.")
        _tg_message("\n".join(lines))
        print(f"[admin] Ambiguity alert sent for ${amount:.2f}")
    except Exception as e:
        print(f"[admin] Failed to send ambiguity alert: {e}")


def _notify_admin_orphan(amount, body, msg_id):
    """Send admin notification when e-Transfer has no matching pending booking."""
    try:
        from app import _tg_message
        # Extract sender info from body for context
        snippet = body[:500].replace('\n', ' ').strip()
        lines = [
            f"💸 **Orphan payment: ${amount:.2f}**",
            f"No pending booking matches this amount.",
            f"",
            f"*Email snippet:*",
            f"```",
            f"{snippet[:300]}",
            f"```",
            f"",
            f"Message ID: `{msg_id}`",
            f"Action needed: check if client paid without booking, or booking expired."
        ]
        _tg_message("\n".join(lines))
        print(f"[admin] Orphan alert sent for ${amount:.2f}")
    except Exception as e:
        print(f"[admin] Failed to send orphan alert: {e}")


def _notify_admin_overpaid(booking, expected, actual):
    """Notify admin: client paid more than expected — auto-confirmed."""
    try:
        from app import _tg_message
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
        _tg_message("\n".join(lines))
        print(f"[admin] Overpaid alert sent for #{booking['id']} (${actual:.2f})")
    except Exception as e:
        print(f"[admin] Failed to send overpaid alert: {e}")


def _notify_admin_underpaid(booking, expected, actual):
    """Notify admin: client paid less than expected — NOT confirmed."""
    try:
        from app import _tg_message
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
        _tg_message("\n".join(lines))
        print(f"[admin] Underpaid alert sent for #{booking['id']} (${actual:.2f})")
    except Exception as e:
        print(f"[admin] Failed to send underpaid alert: {e}")


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
    return None


def _parse_db_datetime(value):
    """Parse SQLite datetime strings stored either with space or T separator."""
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("T", " ")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            try:
                return datetime.strptime(candidate.split(".")[0], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
    return None


def _filter_bookings_created_before_email(email, bookings):
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
    return safe


def check_single_email(email, bookings):
    """Process one email against current pending bookings.
    Returns (confirmed_booking_id, ambiguity_list) or (None, None)."""
    msg_id = str(email.get("id", ""))
    if not msg_id:
        return None, None

    if is_message_processed(msg_id):
        print(f"   [skip] Message {msg_id} already processed")
        return None, None

    body = read_message_body(msg_id)
    if not body:
        return None, None

    amount = extract_payment_info(body)
    if amount is None:
        print(f"   [skip] Could not extract amount from message {msg_id}")
        return None, None

    print(f"   💰 Extracted amount: ${amount:.2f}")

    bookings = _filter_bookings_created_before_email(email, bookings)
    if not bookings:
        print(f"   [skip] No time-valid bookings for message {msg_id}")
        return None, None

    matched, ambiguous, match_type = match_by_amount_only(amount, bookings)

    if ambiguous:
        print(f"   ⚠️ Ambiguity: ${amount:.2f} matches {len(ambiguous)} bookings")
        _notify_admin_ambiguity(amount, ambiguous)
        # Do NOT mark ambiguous messages as processed.
        # A common real-world case is two temporary reservations with the same
        # deposit amount. One may expire moments later; keeping the message
        # unprocessed lets the watcher retry and confirm the remaining booking.
        return None, ambiguous

    if matched is None:
        print(f"   ❌ No booking matches ${amount:.2f}")
        # Orphan payment: e-Transfer received but no matching pending booking
        _notify_admin_orphan(amount, body, msg_id)
        mark_message_processed(msg_id, None, amount)
        return None, None

    expected = get_expected_amount_for_booking(matched["id"])

    if match_type in ('exact', 'overpaid'):
        # Auto-confirm with the ACTUAL amount received (balance = full_price - amount).
        if confirm_booking(matched["id"], amount):
            mark_message_processed(msg_id, matched["id"], amount)
            if match_type == 'overpaid':
                _notify_admin_overpaid(matched, expected, amount)
            print(f"   ✅ CONFIRMED Booking #{matched['id']} — ${amount:.2f} ({match_type})")
            return matched["id"], None
        print(f"   ❌ DB update failed for #{matched['id']}")
        return None, None

    if match_type == 'underpaid':
        # Record the payment but DO NOT confirm — admin handles it.
        if record_partial_payment(matched["id"], amount):
            mark_message_processed(msg_id, matched["id"], amount)
            _notify_admin_underpaid(matched, expected, amount)
            print(f"   ⚠️ UNDERPAID Booking #{matched['id']} — ${amount:.2f} (expected ${expected:.2f}); NOT confirmed")
            return matched["id"], None
        # Booking already confirmed or gone — mark processed so we don't retry-loop.
        print(f"   [skip] Could not record partial payment for #{matched['id']} (already confirmed?)")
        mark_message_processed(msg_id, matched["id"], amount)
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
    bookings = get_pending_booking()
    print(f"Found {len(bookings)} pending bookings")
    for e in etransfers:
        check_single_email(e, bookings)
