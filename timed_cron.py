#!/usr/bin/env python3
"""
Timed Cron Job — Active payment check after client confirms payment.
Runs for a limited time (default: 20 minutes), checking every 1 minute.

All Gmail fetching, Interac parsing, and amount matching live in
check_etransfer_v2 — this file only schedules scans. (It used to carry its own
stale copies of the v1 parser/matcher with a hardcoded $95 fallback and a
keyword-only sender check; those were removed so there is exactly one matching
implementation.)

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
import argparse
from datetime import datetime, timedelta

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(__file__))

# v2 safe payment checker (amount-only, processed_emails ledger)
from check_etransfer_v2 import (
    check_single_email,
    get_pending_bookings,
    get_emails,
    get_reconciliation_bookings,
    is_etransfer_email,
)

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


def _pending_bookings_for(booking_id=None):
    """Pending bookings from the shared v2 window, optionally for one id."""
    bookings = get_pending_bookings()
    if booking_id is not None:
        bookings = [b for b in bookings if str(b.get("id")) == str(booking_id)]
    return bookings


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
    save_cron_state({
        "start_time": datetime.now().isoformat(),
        "deadline": deadline.isoformat(),
        "interval_seconds": interval_seconds,
        "status": "running",
        "checks_done": 0,
        "payments_found": 0,
        "booking_id": booking_id
    })

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
        bookings = _pending_bookings_for(booking_id)
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
                except Exception:
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
        reconciliation = get_reconciliation_bookings(within_days=120)
        if booking_id is not None:
            reconciliation = [b for b in reconciliation if str(b.get("id")) == str(booking_id)]

        # Process each email using v2 safe checker (amount-only, processed_emails ledger)
        for email in etransfers:
            print(f"\n   📧 Checking: {email.get('subject', 'No subject')}")

            result = check_single_email(email, bookings, reconciliation)
            if result is None:
                continue
            confirmed_id, ambiguous = result

            # Refresh booking list if one was confirmed
            if confirmed_id:
                payments_confirmed += 1
                bookings = [b for b in bookings if b["id"] != confirmed_id]

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
