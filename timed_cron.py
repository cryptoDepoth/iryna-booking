#!/usr/bin/env python3
"""
Timed Cron Job — Active payment check after client confirms payment.
Runs for a limited time (default: 20 minutes), checking every 1 minute.

Usage: python3 timed_cron.py [--minutes N] [--interval N] [--booking-id ID]

Options:
  --minutes N     Max runtime in minutes (default: 20)
  --interval N    Check interval in seconds (default: 60)
  --booking-id ID Optional booking id to watch; otherwise watches all pending bookings
"""

import os
import sys
import time
import json
import sqlite3
import argparse
import subprocess
import re
from datetime import datetime, timedelta

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(__file__))

DB_PATH = os.path.join(os.path.dirname(__file__), "bookings.db")
LOCK_PATH = os.path.join(os.path.dirname(__file__), ".timed_cron.lock")


def active_lock_exists():
    """Return True if another timed checker is still active."""
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH) as f:
            state = json.load(f)
        pid = int(state.get("pid") or 0)
        deadline = state.get("deadline")
        if deadline and datetime.fromisoformat(deadline) < datetime.now():
            os.remove(LOCK_PATH)
            return False
        if pid:
            os.kill(pid, 0)
            return True
    except ProcessLookupError:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
        return False
    except Exception:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
        return False
    return False


def acquire_lock(deadline, booking_id=None):
    if active_lock_exists():
        print("Another active payment checker is already running; exiting.")
        return False
    with open(LOCK_PATH, "w") as f:
        json.dump({"pid": os.getpid(), "deadline": deadline.isoformat(), "booking_id": booking_id}, f, indent=2)
    return True


def release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except OSError:
        pass


def get_timed_cron_config():
    """Get config from JSON file if exists, default: 20 minutes, every 60 seconds"""
    config_path = os.path.join(os.path.dirname(__file__), ".timed_cron.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"max_minutes": 20, "interval_seconds": 60}


def save_cron_state(state):
    """Save current cron state for monitoring"""
    state_path = os.path.join(os.path.dirname(__file__), ".timed_cron_state.json")
    with open(state_path, 'w') as f:
        json.dump(state, f, indent=2, default=str)


def load_cron_state():
    """Load current cron state"""
    state_path = os.path.join(os.path.dirname(__file__), ".timed_cron_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def expire_unpaid_bookings():
    """Release slots where payment was not received in time"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    
    c.execute("""
        SELECT id, name, time, date FROM bookings 
        WHERE status = 'pending_payment' 
        AND reserved_until <= ?
        AND confirmed = 0
        AND paid = 0
    """, (now,))
    expired = c.fetchall()
    
    for row in expired:
        booking_id, name, time, date = row
        print(f"⏰ EXPIRED: Booking #{booking_id} — {name or '(no name)'} @ {date} {time}")
        
        c.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        print(f"   ✅ Removed expired booking #{booking_id}")
    
    conn.commit()
    conn.close()
    return len(expired)


def get_pending_bookings(booking_id=None):
    """Get bookings awaiting payment, optionally restricted to one booking id."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now = datetime.now().isoformat()
    params = [now]
    extra = ""
    if booking_id is not None:
        extra = " AND id = ?"
        params.append(int(booking_id))
    c.execute(f"""
        SELECT * FROM bookings 
        WHERE status = 'pending_payment'
        AND reserved_until > ?
        AND confirmed = 0
        AND paid = 0
        {extra}
        ORDER BY created_at DESC
    """, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_emails():
    """Fetch recent emails via Himalaya CLI"""
    try:
        result = subprocess.run(
            ["himalaya", "envelope", "list", "--folder", "INBOX", "-o", "json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.strip().split('\n')
        json_line = None
        for line in lines:
            line = line.strip()
            if line.startswith('[') or line.startswith('{'):
                json_line = line
                break
        if not json_line:
            return []
        return json.loads(json_line)
    except Exception as e:
        print(f"Error fetching emails: {e}")
        return None


def is_etransfer_email(email):
    """Check if email is an Interac e-Transfer notification"""
    subject = email.get("subject", "").lower()
    sender = email.get("from", {}).get("addr", "").lower()
    
    keywords = ["interac", "e-transfer", "etransfer", "transfer received", "deposit received"]
    sender_domains = ["interac.ca", "payments.interac.ca", "notify.interac.ca"]
    
    has_keyword = any(kw in subject for kw in keywords)
    from_interac = any(domain in sender for domain in sender_domains)
    
    return has_keyword or from_interac


def read_message_body(email_id):
    """Return normalized plain text for a Himalaya message id."""
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
    for line in raw.split('\n'):
        line = line.strip()
        if line.startswith('"') or line.startswith('{'):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, str):
                    return parsed
                if isinstance(parsed, dict):
                    return parsed.get("text", {}).get("plain", "") or parsed.get("body", "") or str(parsed)
            except json.JSONDecodeError:
                continue
    return raw


def extract_payment_info(body_text):
    """Extract payment amount and sender name from email body"""
    if not body_text:
        return None
    
    text_lower = body_text.lower()
    
    # Amount patterns
    amount = None
    patterns = [
        r'\$([0-9,]+\.\d{2})',
        r'amount:\s*\$?([0-9,]+\.?\d*)',
        r'for the amount of\s*\$?([0-9,]+\.?\d*)',
        r'sent you\s*\$?([0-9,]+\.?\d*)',
    ]
    for pattern in patterns:
        match = re.search(pattern, body_text, re.I)
        if match:
            try:
                amount = float(match.group(1).replace(',', ''))
                break
            except:
                pass
    
    # Sender name
    sender = None
    sender_patterns = [
        r'Sent From:\s*([^\n\r]+)',
        r'Interac e-Transfer: You[^\n]+ from\s+(.+?)\s+and it has been automatically deposited',
        r'([A-Z][A-Z\' -]+)\s+sent you',
        r'name\s*[:=]\s*([A-Za-z\s\'-]+)',
    ]
    for pattern in sender_patterns:
        match = re.search(pattern, body_text, re.I)
        if match:
            sender = match.group(1).strip()
            break
    
    # Sender email
    sender_email = None
    email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', body_text)
    if email_match:
        sender_email = email_match.group(0)
    
    return {
        "amount": amount,
        "sender_name": sender,
        "sender_email": sender_email
    }


def match_payment_to_booking(payment_info, bookings):
    """Match payment to a booking by email or name"""
    for booking in bookings:
        # Exact email match
        if payment_info.get("sender_email") and booking.get("email"):
            if payment_info["sender_email"].lower() == booking["email"].lower():
                return booking
        
        # Name fuzzy match
        if payment_info.get("sender_name") and booking.get("name"):
            p_name = payment_info["sender_name"].lower()
            b_name = booking["name"].lower()
            p_words = [w for w in p_name.split() if len(w) > 2]
            b_words = [w for w in b_name.split() if len(w) > 2]
            if any(pw in b_words for pw in p_words):
                return booking
    
    return None


def confirm_booking(booking_id, paid_amount=None):
    """Confirm booking, send email + Telegram notification."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        UPDATE bookings 
        SET confirmed=1, paid=1, status='confirmed', paid_amount=?
        WHERE id=?
    """, (paid_amount, booking_id))
    conn.commit()
    updated = c.rowcount
    conn.close()
    
    if updated > 0:
        print(f"      📧 Sending confirmation email to client...")
        try:
            from app import send_confirmation_email, notify_payment_confirmed
            send_confirmation_email(booking_id)
            notify_payment_confirmed(booking_id, paid_amount)
        except Exception as e:
            print(f"      ⚠️ Email/Telegram notification error: {e}")
        # Sync to Notion
        sync_to_notion(booking_id)
        return True
    return False


def sync_to_notion(booking_id):
    """Import sync function from app.py"""
    try:
        from app import NOTION_API_KEY, NOTION_DATABASE_ID, sync_to_notion as sync_impl
        if NOTION_API_KEY and NOTION_DATABASE_ID:
            return sync_impl(booking_id)
        else:
            print(f"   ⚠️ Notion sync skipped (missing config)")
    except Exception as e:
        print(f"   ⚠️ Notion sync failed: {e}")


def expected_deposit_amount():
    try:
        event_path = os.path.join(os.path.dirname(__file__), "event.json")
        with open(event_path) as f:
            cfg = json.load(f)
        return float(cfg.get("deposit") or 95.0)
    except Exception:
        return 95.0


def main(max_minutes=20, interval_seconds=60, booking_id=None):
    """Main cron loop with time limit"""
    print("=" * 60)
    print("⏳ TIMED CRON JOB — Active Payment Check")
    print("=" * 60)
    print(f"Config: max_runtime={max_minutes}min, check_interval={interval_seconds}s, booking_id={booking_id or 'all'}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Calculate deadline
    deadline = datetime.now() + timedelta(minutes=max_minutes)
    print(f"Deadline: {deadline.strftime('%Y-%m-%d %H:%M:%S')}")
    if not acquire_lock(deadline, booking_id):
        return 0
    
    # Save start state
    cron_config = get_timed_cron_config()
    save_cron_state({
        "start_time": datetime.now().isoformat(),
        "deadline": deadline.isoformat(),
        "interval_seconds": interval_seconds,
        "status": "running",
        "checks_done": 0,
        "payments_found": 0,
        "booking_id": booking_id
    })
    
    expected_amount = expected_deposit_amount()
    
    checked_count = 0
    payments_confirmed = 0
    
    while datetime.now() < deadline:
        # Check interval
        if checked_count > 0:
            print(f"\n⏳ Interval: waiting {interval_seconds}s...")
            time.sleep(interval_seconds)
            if datetime.now() >= deadline:
                print("⏰ Deadline reached, exiting...")
                break
        
        checked_count += 1
        print(f"\n[Check #{checked_count}] {datetime.now().strftime('%H:%M:%S')}")
        
        # Get pending bookings
        bookings = get_pending_bookings(booking_id)
        if not bookings:
            print("   ✅ All caught up! No pending bookings.")
            break
        
        print(f"   📋 {len(bookings)} booking(s) awaiting payment:")
        for b in bookings:
            time_left = ""
            if b.get('reserved_until'):
                try:
                    expiry = datetime.fromisoformat(b['reserved_until'].replace('Z', '+00:00'))
                    mins_left = max(0, int((expiry - datetime.now(expiry.tzinfo)).total_seconds() / 60))
                    time_left = f" | {mins_left}m left"
                except:
                    pass
            print(f"      #{b['id']}: {b['name'] or '(no name)'} @ {b['time']} ({b['email']}){time_left}")
        
        # Fetch emails
        emails = get_emails()
        if emails is None:
            print("   ⚠️ Could not fetch emails from Gmail.")
            continue
        
        # Filter e-Transfer emails
        etransfers = [e for e in emails if is_etransfer_email(e)]
        if not etransfers:
            print("   📭 No e-Transfer emails found.")
            continue
        
        print(f"   💰 {len(etransfers)} e-Transfer email(s) found.")
        
        # Process each email
        for email in etransfers:
            print(f"\n   📧 Checking: {email.get('subject', 'No subject')}")
            
            # Read body
            try:
                body_text = read_message_body(email["id"])
                if not body_text:
                    print("      Could not read email body.")
                    continue
            except Exception as e:
                print(f"      Error reading email: {e}")
                continue
            
            # Extract payment info
            payment_info = extract_payment_info(body_text)
            if not payment_info or not payment_info.get("amount"):
                print("      Could not extract payment amount.")
                continue
            
            amount = payment_info["amount"]
            sender_name = payment_info.get("sender_name", "Unknown")
            sender_email = payment_info.get("sender_email", "N/A")
            
            print(f"      Amount: ${amount:.2f}")
            print(f"      From: {sender_name} ({sender_email})")
            
            # Match to booking
            booking = match_payment_to_booking(payment_info, bookings)
            if not booking:
                print("      ⚠️ No matching booking found.")
                continue
            
            # Check amount with ±$3 tolerance
            expected = expected_amount
            tolerance = 3.0
            if amount < expected - tolerance:
                diff = expected - amount
                print(f"      ⚠️ UNDERPAID: ${amount:.2f} (expected ${expected:.2f}, short ${diff:.2f})")
                print("      ⏳ Will wait for additional payment.")
                continue
            if amount > expected + tolerance:
                print(f"      ⚠️ OVERPAID: ${amount:.2f} (expected ${expected:.2f}, over ${amount - expected:.2f})")
                print("      ⏳ Will still confirm — overpayment is OK.")
            
            # Confirm booking
            print(f"      ✅ Payment confirmed: ${amount:.2f}")
            if confirm_booking(booking["id"], amount):
                print(f"      ✅ CONFIRMED: Booking #{booking['id']} — {booking['name']}")
                payments_confirmed += 1
                # Remove this booking from list
                bookings = [b for b in bookings if b["id"] != booking["id"]]
            else:
                print(f"      ❌ Failed to confirm booking #{booking['id']}")
        
        # Update state
        save_cron_state({
            "start_time": datetime.now().isoformat(),
            "deadline": (datetime.now() + timedelta(minutes=max_minutes)).isoformat(),
            "interval_seconds": interval_seconds,
            "status": "running",
            "checks_done": checked_count,
            "payments_found": payments_confirmed,
            "booking_id": booking_id
        })
    
    print("\n" + "=" * 60)
    print(f"✅ Timed cron completed!")
    print(f"   Checks: {checked_count}")
    print(f"   Payments confirmed: {payments_confirmed}")
    print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # Clear state when done
    state_path = os.path.join(os.path.dirname(__file__), ".timed_cron_state.json")
    if os.path.exists(state_path):
        os.remove(state_path)
    release_lock()
    
    return payments_confirmed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Active payment check cron")
    parser.add_argument("--minutes", type=int, default=None, help="Max runtime in minutes")
    parser.add_argument("--interval", type=int, default=None, help="Check interval in seconds")
    parser.add_argument("--booking-id", type=int, default=None, help="Only watch this booking id")
    args = parser.parse_args()
    
    config = get_timed_cron_config()
    max_minutes = args.minutes if args.minutes else config.get("max_minutes", 20)
    interval_seconds = args.interval if args.interval else config.get("interval_seconds", 60)
    
    print(f"Starting timed cron: max={max_minutes}min, interval={interval_seconds}s, booking_id={args.booking_id or 'all'}")
    try:
        main(max_minutes, interval_seconds, args.booking_id)
    finally:
        release_lock()
