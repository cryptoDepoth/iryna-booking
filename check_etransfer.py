#!/usr/bin/env python3
"""
e-Transfer Email Parser for Pashynska Photography
Checks Gmail via Himalaya CLI for Interac e-Transfer notifications
and auto-confirms bookings when payment matches.

Usage: python3 check_etransfer.py
"""

import subprocess
import json
import re
import sqlite3
import os
import sys
import requests
from datetime import datetime, timedelta
# Add parent dir to path for importing app
sys.path.insert(0, os.path.dirname(__file__))
from app import sync_to_notion

DB_PATH = os.path.join(os.path.dirname(__file__), "bookings.db")

def get_emails():
    """Fetch recent emails via Himalaya CLI"""
    try:
        result = subprocess.run(
            ["himalaya", "envelope", "list", "--folder", "INBOX", "-o", "json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"Himalaya error: {result.stderr}")
            return []
        # Parse JSON (skip WARN lines)
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
        print(f"Error: {e}")
        return []

def is_etransfer_email(email):
    """Check if email is an Interac e-Transfer notification"""
    subject = email.get("subject", "").lower()
    sender = email.get("from", {}).get("addr", "").lower()
    
    keywords = ["interac", "e-transfer", "etransfer", "transfer received", "deposit received"]
    sender_domains = ["interac.ca", "payments.interac.ca", "notify.interac.ca"]
    
    has_keyword = any(kw in subject for kw in keywords)
    from_interac = any(domain in sender for domain in sender_domains)
    
    return has_keyword or from_interac

def read_email_body(email_id):
    """Read full email body using Himalaya.

    Himalaya may return either a JSON object or a JSON string when `-o json` is
    used. Normalize both into plain body text so Interac parsing is stable.
    """
    try:
        result = subprocess.run(
            ["himalaya", "message", "read", email_id, "-o", "json"],
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
        # Fallback for warn-prefixed or plain output.
        for line in raw.split('\n'):
            line = line.strip()
            if not line:
                continue
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
    except Exception as e:
        print(f"Error reading email {email_id}: {e}")
        return None

def extract_payment_info(body_text):
    """Extract payment amount and sender name from email body"""
    if not body_text:
        return None
    
    text_lower = body_text.lower()
    
    # Amount patterns
    amount = None
    patterns = [
        r'\$([\d,]+\.\d{2})',
        r'amount:\s*\$?([\d,]+\.?\d*)',
        r'for the amount of\s*\$?([\d,]+\.?\d*)',
        r'sent you\s*\$?([\d,]+\.?\d*)',
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

EXPECTED_AMOUNT = 95.0  # CAD deposit amount
PAYMENT_WINDOW_MINUTES = 15  # Time to pay before slot is released

def get_pending_bookings():
    """Get bookings awaiting payment"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""
        SELECT * FROM bookings 
        WHERE status = 'pending_payment'
        AND reserved_until > ?
        AND confirmed = 0
        AND paid = 0
        ORDER BY created_at DESC
    """, (now,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def expire_unpaid_bookings():
    """Release slots where payment was not received in time"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    
    # Find expired bookings
    c.execute("""
        SELECT id, name, time, date FROM bookings 
        WHERE status = 'pending_payment' 
        AND reserved_until <= ?
        AND confirmed = 0
        AND paid = 0
    """, (now,))
    expired = c.fetchall()
    
    expired_count = 0
    for row in expired:
        booking_id, name, time, date = row
        print(f"⏰ EXPIRED: Booking #{booking_id} — {name or '(no name)'} @ {date} {time}")
        
        # Delete from SQLite (free up the slot)
        c.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        
        # Update Notion status to "Expired" — v2 schema uses "Client Name" + "Booking ID (legacy)".
        try:
            from app import NOTION_HEADERS, NOTION_DATABASE_ID
            query_url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
            q = requests.post(query_url, headers=NOTION_HEADERS, json={
                "filter": {"property": "Booking ID (legacy)", "number": {"equals": booking_id}}
            }, timeout=15).json()
            if q.get("results"):
                page_id = q["results"][0]["id"]
                requests.patch(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=NOTION_HEADERS,
                    json={"properties": {
                        "Status": {"select": {"name": "Expired"}},
                        "Client Name": {"title": [{"text": {"content": f"{name or '(expired)'} — RELEASED"}}]},
                    }},
                    timeout=15,
                )
                print("   🔄 Notion updated: Status = Expired")
        except Exception as e:
            print(f"   ⚠️ Notion update failed: {e}")
        
        expired_count += 1
    
    conn.commit()
    conn.close()
    
    if expired_count:
        print(f"\n🗑️ {expired_count} expired booking(s) removed. Slots are now available again.")
    
    return expired_count

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
            # Check if any significant word matches
            p_words = [w for w in p_name.split() if len(w) > 2]
            b_words = [w for w in b_name.split() if len(w) > 2]
            if any(pw in b_words for pw in p_words):
                return booking
    
    return None

def confirm_booking(booking_id, paid_amount=None):
    """Confirm booking and set paid amount"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE bookings 
        SET confirmed=1, paid=1, status='confirmed', paid_amount=?
        WHERE id=?
    """, (paid_amount, booking_id))
    conn.commit()
    updated = c.rowcount
    conn.close()
    return updated > 0

def main():
    from datetime import datetime
    
    print("🔍 Checking e-Transfer system...")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # Step 1: Release expired bookings (unpaid, past deadline)
    print("[Step 1] Checking for expired bookings...")
    expired = expire_unpaid_bookings()
    print()
    
    # Step 2: Check for e-Transfer payments
    print("[Step 2] Checking Gmail for e-Transfer payments...")
    
    # Get pending bookings
    bookings = get_pending_bookings()
    if not bookings:
        print("   No pending bookings awaiting payment.")
        print(f"\n{'='*50}")
        print("ℹ️ Nothing to process. All caught up!")
        print(f"{'='*50}")
        return
    
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
    if not emails:
        print("\n   ⚠️ Could not fetch emails from Gmail.")
        return
    
    # Filter e-Transfer emails
    etransfers = [e for e in emails if is_etransfer_email(e)]
    if not etransfers:
        print("\n   📭 No new e-Transfer emails found in inbox.")
        print(f"\n{'='*50}")
        print(f"ℹ️ No payments to match. Will check again in 5 minutes.")
        print(f"{'='*50}")
        return
    
    print(f"\n   💰 {len(etransfers)} e-Transfer email(s) found.")
    print("=" * 50)
    
    confirmed = 0
    for email in etransfers:
        print(f"\n📧 Checking: {email.get('subject', 'No subject')}")
        
        # Read body
        body = read_email_body(email["id"])
        if not body:
            print("   Could not read email body.")
            continue
        
        # Extract info
        body_text = body if isinstance(body, str) else (body.get("text", {}).get("plain", "") or str(body))
        payment_info = extract_payment_info(body_text)
        
        if not payment_info or not payment_info.get("amount"):
            print("   Could not extract payment amount.")
            continue
        
        print(f"   Amount: ${payment_info['amount']:.2f}")
        print(f"   From: {payment_info.get('sender_name', 'Unknown')} ({payment_info.get('sender_email', 'N/A')})")
        
        # Match to booking
        booking = match_payment_to_booking(payment_info, bookings)
        if not booking:
            print("   ⚠️ No matching booking found.")
            continue
        
        # Check for underpayment
        expected = EXPECTED_AMOUNT
        paid = payment_info["amount"]
        if paid < expected:
            diff = expected - paid
            print(f"   ⚠️ UNDERPAID: ${paid:.2f} (expected ${expected:.2f}, short ${diff:.2f})")
            print(f"   ⏳ Will wait for additional payment.")
            continue  # Don't confirm yet, wait for more
        else:
            print(f"   ✅ Payment received: ${paid:.2f}")
        
        # Confirm
        if confirm_booking(booking["id"], paid):
            print(f"   ✅ CONFIRMED: Booking #{booking['id']} — {booking['name']} @ {booking['time']}")
            # Sync to Notion
            sync_to_notion(booking["id"])
            print(f"   🔄 Synced to Notion")
            confirmed += 1
        else:
            print(f"   ❌ Failed to confirm booking #{booking['id']}")
    
    print(f"\n{'='*50}")
    if confirmed:
        print(f"🎉 {confirmed} booking(s) auto-confirmed!")
    else:
        print("ℹ️ No bookings auto-confirmed. Manual review may be needed.")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
