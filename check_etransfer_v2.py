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
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "bookings.db"))
EVENTS_YAML_PATH = os.environ.get("EVENTS_YAML_PATH",
    os.path.join(os.path.dirname(__file__), "events.yaml"))
ETRANSFER_EMAIL_PAGE_SIZE = int(os.environ.get("ETRANSFER_EMAIL_PAGE_SIZE", "75"))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_message_processed(message_id):
    return get_processed_email(message_id) is not None


def get_processed_email(message_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM processed_emails WHERE message_id = ?", (message_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_message_processed(message_id, booking_id, amount):
    conn = get_db()
    c = conn.cursor()
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
    if booking_id is not None:
        try:
            c.execute(
                "UPDATE etransfers SET matched_booking_id=?, status='matched' "
                "WHERE message_id=? AND (matched_booking_id IS NULL OR matched_booking_id=?)",
                (booking_id, message_id, booking_id),
            )
        except Exception:
            pass
    conn.commit()
    conn.close()


def get_emails(page_size=None):
    """Fetch recent emails via Himalaya CLI."""
    page_size = int(page_size or ETRANSFER_EMAIL_PAGE_SIZE)
    commands = [
        [
            "himalaya", "envelope", "list",
            "--folder", "INBOX",
            "--page-size", str(page_size),
            "-o", "json",
            "order", "by", "date", "desc",
        ],
        ["himalaya", "envelope", "list", "--folder", "INBOX", "-o", "json"],
    ]
    try:
        result = None
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                break
        if result is None or result.returncode != 0:
            rc = result.returncode if result else "?"
            print(f"[himalaya] envelope list failed: rc={rc}")
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
    scans of the same email/CSV row never create duplicates.
    """
    ref = reference_number or (f"msg:{message_id}" if message_id else None)
    if not ref:
        return False
    if status is None:
        status = "matched" if matched_booking_id else "unmatched"
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO etransfers (reference_number, message_id, sender_name, amount, memo,
                                    direction, email_date, matched_booking_id, status, source)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (ref, message_id, sender_name, amount, memo, direction, email_date,
              matched_booking_id, status, source))
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
                 WHERE reference_number=? AND (matched_booking_id IS NULL OR matched_booking_id=?)
            """, (matched_booking_id, ref, matched_booking_id))
    conn.commit()
    conn.close()
    return True


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


def get_reconciliation_bookings(within_days=45):
    """Confirmed bookings that can still receive a corrected Interac amount.

    These are not candidates for auto-confirming; they are candidates for
    raising paid_amount when an Interac email arrives late or after a manual
    confirmation. Keep the window narrow enough to avoid matching old inbox
    history forever, but wide enough for future sessions already booked.
    """
    conn = get_db()
    c = conn.cursor()
    today = datetime.now().date()
    date_cutoff = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    created_cutoff = (datetime.now() - timedelta(days=within_days)).strftime("%Y-%m-%d %H:%M:%S")
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


def reconcile_confirmed_payment(booking_id, paid_amount):
    """Raise paid_amount for an already-confirmed booking.

    Never lowers money, never changes date/time/client/status, and never sends
    client email. This is for delayed Interac messages correcting an amount
    that was previously entered as the standard deposit.
    """
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


def _notify_admin_reconciled(booking, previous, actual):
    """Notify admin that a later Interac email corrected paid_amount."""
    try:
        from app import _tg_message
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
        _tg_message("\n".join(lines))
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


def check_single_email(email, bookings, reconciliation_bookings=None):
    """Process one email against current pending bookings.
    Returns (confirmed_booking_id, ambiguity_list) or (None, None)."""
    msg_id = str(email.get("id", ""))
    if not msg_id:
        return None, None

    body = read_message_body(msg_id)
    if not body:
        return None, None

    amount = extract_payment_info(body)
    if amount is None:
        print(f"   [skip] Could not extract amount from message {msg_id}")
        return None, None

    print(f"   💰 Extracted amount: ${amount:.2f}")

    # Ledger: record every incoming transfer (even if it never matches a booking).
    try:
        _d = extract_etransfer_details(body)
        record_etransfer(
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

    reconciliation_bookings = reconciliation_bookings or []
    processed = get_processed_email(msg_id)
    if processed:
        # Do not re-confirm/re-run side effects. The one safe exception is
        # money reconciliation: an earlier run may have recorded the Interac
        # email as orphan/processed before a manual confirmation existed.
        recon_candidates = _filter_bookings_created_before_email(email, reconciliation_bookings)
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
            if reconcile_confirmed_payment(reconciled["id"], amount):
                mark_message_processed(msg_id, reconciled["id"], amount)
                _notify_admin_reconciled(reconciled, previous, amount)
                print(f"   ✅ RECONCILED processed message {msg_id}: Booking #{reconciled['id']} ${previous:.2f} → ${amount:.2f}")
            return None, None
        print(f"   [skip] Message {msg_id} already processed")
        return None, None

    original_booking_count = len(bookings or [])
    bookings = _filter_bookings_created_before_email(email, bookings or [])

    matched, ambiguous, match_type = match_by_amount_only(amount, bookings) if bookings else (None, [], None)

    if ambiguous:
        print(f"   ⚠️ Ambiguity: ${amount:.2f} matches {len(ambiguous)} bookings")
        _notify_admin_ambiguity(amount, ambiguous)
        # Do NOT mark ambiguous messages as processed.
        # A common real-world case is two temporary reservations with the same
        # deposit amount. One may expire moments later; keeping the message
        # unprocessed lets the watcher retry and confirm the remaining booking.
        return None, ambiguous

    if matched is None:
        recon_candidates = _filter_bookings_created_before_email(email, reconciliation_bookings)
        reconciled, recon_ambiguous = match_reconciliation_payment(amount, recon_candidates, body)
        if recon_ambiguous:
            print(f"   ⚠️ Reconciliation ambiguity: ${amount:.2f} matches {len(recon_ambiguous)} confirmed bookings")
            _notify_admin_ambiguity(amount, recon_ambiguous)
            return None, recon_ambiguous
        if reconciled is not None:
            previous = float(reconciled.get("paid_amount") or 0)
            if reconcile_confirmed_payment(reconciled["id"], amount):
                mark_message_processed(msg_id, reconciled["id"], amount)
                _notify_admin_reconciled(reconciled, previous, amount)
                print(f"   ✅ RECONCILED Booking #{reconciled['id']} paid_amount ${previous:.2f} → ${amount:.2f}")
            return None, None

        if not original_booking_count:
            print(f"   [skip] No pending bookings for message {msg_id}; no safe reconciliation match")
            return None, None

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
