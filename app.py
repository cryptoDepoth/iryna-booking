#!/usr/bin/env python3
"""
Iryna Pashynska Photography — Mini Session Booking System
Flask app with SQLite database, 20-min sessions + 10-min breaks, Instagram field.
"""

# Load .env file before any imports that use env vars
import os as _os
_env_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env")
if _os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip().strip('"').strip("'")
                if _key not in _os.environ:
                    _os.environ[_key] = _val
    print(f"[env] Loaded {_env_path}")

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory
from datetime import datetime, timedelta
from functools import wraps
import json
import logging
import os
import hmac
import sqlite3
import requests
import sys
import time
import yaml

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'booking.log')),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY") or os.urandom(24).hex()

PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable)


def _start_etransfer_checker(booking_id):
    """Launch timed_cron.py in background to auto-detect e-Transfer for this booking."""
    try:
        import subprocess as _sp
        cron_script = os.path.join(os.path.dirname(__file__) or ".", "timed_cron.py")
        _sp.Popen(
            [PYTHON_BIN, cron_script, "--booking-id", str(booking_id), "--interval", "30", "--minutes", "20"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            cwd=os.path.dirname(__file__) or "."
        )
        log.info(f"[etransfer-checker] Started for booking #{booking_id} (30s interval, 20min max)")
    except Exception as e:
        log.error(f"[etransfer-checker] Failed to start: {e}")

# ===== RATE LIMITING =====
# Simple IP-based rate limit: 5 booking requests per 10 minutes per IP
_rate_limits = {}
# Separate counter for admin login attempts (brute-force protection)
_login_attempts = {}

def check_login_rate_limit(ip):
    now = time.time()
    window = [t for t in _login_attempts.get(ip, []) if now - t < 900]  # 15-min window
    _login_attempts[ip] = window
    return len(window) < 10  # max 10 login attempts per 15 min

def record_login_attempt(ip):
    now = time.time()
    _login_attempts.setdefault(ip, []).append(now)

def check_rate_limit(ip):
    now = time.time()
    window = [t for t in _rate_limits.get(ip, []) if now - t < 600]
    _rate_limits[ip] = window
    return len(window) < 5  # 5 requests per 10 min (matches README)

def record_request(ip):
    now = time.time()
    _rate_limits.setdefault(ip, []).append(now)
    # Evict stale IPs when the dict grows large to prevent unbounded memory use
    if len(_rate_limits) > 10_000:
        cutoff = now - 600
        stale = [k for k, v in _rate_limits.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _rate_limits[k]

# ===== ADMIN AUTH =====
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

def _admin_key_from_request():
    return (
        request.headers.get("X-Admin-Key")
        or request.args.get("key")
        or (request.json.get("key") if request.is_json else None)
        or ""
    )

def _admin_authorized():
    # Browser session (logged in via /admin/login)
    if session.get("admin_authenticated"):
        return True
    # Programmatic API access via X-Admin-Key header or ?key= param
    if ADMIN_KEY and _admin_key_from_request() == ADMIN_KEY:
        return True
    # SECURITY: never allow open access — admin MUST have credentials set
    return False

def admin_required(f):
    """Require a browser login, X-Admin-Key header, or ?key= query param."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _admin_authorized():
            if request.method == "GET" and request.path == "/admin":
                return redirect(url_for("admin_login", next=request.path))
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ===== NOTIFICATIONS =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "792920251")  # Andrzej — always gets copies
BASE_URL = os.environ.get("BOOKING_BASE_URL", "")


def _tg_send(chat_id, text, reply_markup=None):
    """Low-level: send a Telegram message to a specific chat."""
    if not TELEGRAM_BOT_TOKEN:
        log.info(f"[tg] No token. Would send to {chat_id}: {text[:80]}...")
        return None
    if not chat_id:
        return None
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        if r.status_code == 200:
            log.info(f"[tg] Message sent to {chat_id}")
            return r.json().get("result", {})
        else:
            log.error(f"[tg] Send failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        log.error(f"[tg] Send error: {e}")
    return None


def _notify_admin(message, reply_markup=None):
    """Send notification to both Iryna and Andrzej via Telegram."""
    _tg_send(TELEGRAM_CHAT_ID, message, reply_markup=reply_markup)
    _tg_send(TELEGRAM_ADMIN_CHAT_ID, message, reply_markup=reply_markup)


def _notify_new_reservation(booking_id, client_name, client_email, event_date,
                            slot_time, event_title, session_type, client_ig,
                            client_phone=None):
    """Send NEW reservation notification with inline confirm/cancel buttons."""
    ig_clean = (client_ig or "").lstrip("@")
    phone_display = client_phone or "N/A"
    admin_url = f"{BASE_URL}/admin" if BASE_URL else "/admin"
    
    text = (
        f"🆕 <b>New reservation #{booking_id}</b>\n\n"
        f"👤 {client_name or '(no name)'}\n"
        f"📧 {client_email}\n"
        f"📞 {phone_display}\n"
        f"📱 Instagram: @{ig_clean or 'N/A'}\n\n"
        f"📅 {event_date} @ {slot_time}\n"
        f"🎉 {event_title}\n"
        f"🏷 Session: {session_type or 'N/A'}\n"
        f"⏱️ Expires in {RESERVATION_MINUTES} min\n\n"
        f"<b>Press below when client pays:</b>"
    )
    
    action_row = [
        {"text": "✅ Payment Received", "callback_data": f"confirm:{booking_id}"},
        {"text": "❌ Cancel", "callback_data": f"cancel:{booking_id}"},
    ]
    link_row = [
        {"text": "🔗 Admin Panel", "url": admin_url},
    ]
    if ig_clean:
        link_row.append({"text": "📸 Instagram", "url": f"https://instagram.com/{ig_clean}"})
    
    keyboard = {"inline_keyboard": [action_row, link_row]}
    _notify_admin(text, reply_markup=keyboard)


def _notify_payment_pending(booking_id, client_name, client_email, event_date,
                            slot_time, event_title, session_type, client_ig,
                            expected_deposit=None, client_phone=None):
    """Send payment notification with inline confirm/cancel buttons to Iryna + Andrzej."""
    deposit = expected_deposit or SESSION_PRICE
    slot_end = ""
    try:
        from datetime import datetime, timedelta
        t = datetime.strptime(slot_time, "%H:%M")
        slot_end = (t + timedelta(minutes=SESSION_LENGTH)).strftime("%H:%M")
    except Exception:
        pass

    admin_url = f"{BASE_URL}/admin" if BASE_URL else "/admin"
    success_url = f"{BASE_URL}/success?booking_id={booking_id}" if BASE_URL else f"/success?booking_id={booking_id}"

    ig_clean = (client_ig or "").lstrip("@")
    phone_display = client_phone or "N/A"

    text = (
        f"💰 <b>Payment marked as sent</b>\n\n"
        f"📋 Booking #{booking_id}\n"
        f"👤 {client_name or '(no name)'}\n"
        f"📧 {client_email}\n"
        f"📞 {phone_display}\n"
        f"📱 Instagram: @{ig_clean or 'N/A'}\n\n"
        f"📅 {event_date} · {slot_time}{'–' + slot_end if slot_end else ''}\n"
        f"🏷 Session: {event_title}\n"
        f"💵 Expected deposit: ${deposit:.2f} CAD\n\n"
        f"⏳ <b>Check e-Transfer, then press a button below:</b>\n\n"
        f"🔗 <a href=\"{admin_url}\">Admin panel</a>  |  "
        f"<a href=\"{success_url}\">Client page</a>"
    )

    action_row = [
        {"text": "✅ Payment Received", "callback_data": f"confirm:{booking_id}"},
        {"text": "❌ Cancel", "callback_data": f"cancel:{booking_id}"},
    ]
    link_row = [
        {"text": "🔗 Admin Panel", "url": admin_url},
        {"text": "📄 Client Page", "url": success_url},
    ]
    if ig_clean:
        link_row.append({"text": "📸 Instagram", "url": f"https://instagram.com/{ig_clean}"})

    keyboard = {"inline_keyboard": [action_row, link_row]}
    _tg_send(TELEGRAM_CHAT_ID, text, reply_markup=keyboard)
    _tg_send(TELEGRAM_ADMIN_CHAT_ID, text, reply_markup=keyboard)


def _send_client_email(to_email, client_name, event_date, slot_time, event_title, booking_id, location=None):
    """Send HTML confirmation email to client via Himalaya CLI (multipart/alternative)."""
    if not to_email:
        return
    try:
        import subprocess
        import email.utils
        date_nice = datetime.strptime(event_date, "%Y-%m-%d").strftime("%B %d, %Y")
        subject = f"Booking Confirmed — {event_title} on {date_nice}"

        location_line = f"Location: {location}\n" if location else "Location details will be sent closer to the session date.\n"
        plain = (
            f"Hi {client_name},\n\n"
            f"Your mini photo session is confirmed!\n\n"
            f"Event: {event_title}\n"
            f"Date: {date_nice}\n"
            f"Time: {slot_time}\n"
            f"{location_line}"
            f"Booking ID: #{booking_id}\n\n"
            f"What's included:\n"
            f"• 20-minute photo session\n"
            f"• 15 professionally edited photos\n"
            f"• All original photos included\n"
            f"• Quick turnaround (within 48 hours)\n\n"
            f"Need to reschedule? DM me on Instagram @pashynska.photo.\n\n"
            f"Looking forward to our session!\n\n"
            f"Warmly,\nIryna Pashynska\n@pashynska.photo"
        )

        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.07);">
  <tr><td style="background:linear-gradient(135deg,#c084a8 0%,#9b5e8a 100%);padding:40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:32px;">🪻</p>
    <h1 style="margin:0;color:#fff;font-size:26px;font-weight:normal;letter-spacing:1px;">Booking Confirmed</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.85);font-size:14px;">Pashynska Photography</p>
  </td></tr>
  <tr><td style="padding:36px 40px 24px;">
    <p style="margin:0 0 20px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{client_name}</strong>,</p>
    <p style="margin:0 0 28px;font-size:15px;color:#7a5a6a;line-height:1.7;">Your mini photo session is confirmed! ✨ I'm so excited to capture beautiful moments with you.</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;border-radius:12px;margin-bottom:28px;">
      <tr><td style="padding:24px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;color:#7a5a6a;font-size:14px;">🎉 &nbsp;Event</td>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{event_title}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;color:#7a5a6a;font-size:14px;">🗓 &nbsp;Date</td>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{date_nice}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;color:#7a5a6a;font-size:14px;">⏰ &nbsp;Time</td>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{slot_time}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#7a5a6a;font-size:14px;">🆔 &nbsp;Booking ID</td>
            <td style="padding:8px 0;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">#{booking_id}</strong></td>
          </tr>
        </table>
      </td></tr>
    </table>
    <h3 style="margin:0 0 12px;color:#5a3d4a;font-size:15px;">What's included:</h3>
    <table cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      <tr><td style="padding:3px 0;color:#7a5a6a;font-size:14px;">🌸 &nbsp;20-minute photo session</td></tr>
      <tr><td style="padding:3px 0;color:#7a5a6a;font-size:14px;">📸 &nbsp;15 professionally edited photos</td></tr>
      <tr><td style="padding:3px 0;color:#7a5a6a;font-size:14px;">🖼 &nbsp;All original photos included</td></tr>
      <tr><td style="padding:3px 0;color:#7a5a6a;font-size:14px;">⚡ &nbsp;Quick turnaround (within 48 hours)</td></tr>
    </table>
    {f'<p style="margin:0 0 12px;font-size:14px;color:#7a5a6a;line-height:1.7;">📍 <strong>Location:</strong> {location}</p>' if location else '<p style="margin:0 0 12px;font-size:14px;color:#7a5a6a;line-height:1.7;">📍 Exact location will be sent closer to the session date.</p>'}
    <p style="margin:0 0 28px;font-size:14px;color:#7a5a6a;line-height:1.7;">Need to reschedule? DM me on Instagram <a href="https://instagram.com/pashynska.photo" style="color:#c084a8;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
  <tr><td style="padding:0 40px 36px;">
    <p style="margin:0;font-size:15px;color:#5a3d4a;">Looking forward to our session! 🌸</p>
    <p style="margin:12px 0 0;font-size:14px;color:#9b5e8a;"><strong>Iryna Pashynska</strong><br>
    <a href="https://instagram.com/pashynska.photo" style="color:#c084a8;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
  <tr><td style="background:#f9f1f5;padding:20px 40px;text-align:center;border-top:1px solid #f0e0e8;">
    <p style="margin:0;font-size:12px;color:#b8a0b0;">Pashynska Photography · Calgary, AB · Canada<br>
    <a href="https://instagram.com/pashynska.photo" style="color:#c084a8;text-decoration:none;">instagram.com/pashynska.photo</a></p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""

        boundary = f"====boundary_{booking_id}===="
        template = (
            f"From: Iryna Pashynska <iryna.pashynska@gmail.com>\r\n"
            f"To: {client_name} <{to_email}>\r\n"
            f"Subject: {subject}\r\n"
            f"MIME-Version: 1.0\r\n"
            f"Content-Type: multipart/alternative; boundary=\"{boundary}\"\r\n"
            f"\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{plain}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n\r\n"
            f"{html}\r\n"
            f"--{boundary}--\r\n"
        )

        result = subprocess.run(
            ["himalaya", "message", "send", "-a", "iryna"],
            input=template, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info(f"[email] HTML confirmation sent to {to_email}")
        else:
            log.error(f"[email] Himalaya failed: {result.stderr[:200]}")
    except Exception as e:
        log.error(f"[email] Send failed: {e}")


def notify_payment_confirmed(booking_id, paid_amount=None):
    """Send Telegram notification to admin: booking was auto-confirmed by e-Transfer checker."""
    try:
        conn = db_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        if not row:
            return
        b = dict(row)
        amount_str = f"${paid_amount:.2f}" if paid_amount else "deposit"
        msg = (
            f"✅ <b>Auto-Confirmed: Payment Received!</b>\n\n"
            f"👤 {b.get('name','?')}\n"
            f"📧 {b.get('email','?')}\n"
            f"📱 {b.get('phone','N/A')}\n"
            f"📸 @{b.get('instagram','N/A')}\n"
            f"📅 {b.get('date','?')} @ {b.get('time','?')}\n"
            f"💰 <b>Received: {amount_str}</b>\n"
            f"🆔 Booking #{booking_id}\n\n"
            f"Email confirmation sent to client."
        )
        _notify_admin(msg)
    except Exception as e:
        log.error(f"[notify_confirmed] Failed: {e}")

# Serve static images
@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(os.path.join(app.root_path, 'static', 'images'), filename)

# ===== NOTION CONFIG =====
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
# New Bookings v2 database (improved schema, Status field, Calendar Event link, etc.)
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "d722613f-a8b5-438f-bcf0-0ef9f84c3d78")
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

# Map internal SQLite status → Notion select option
NOTION_STATUS_MAP = {
    "reserved": "Reserved",
    "pending": "Pending Payment",
    "pending_payment": "Pending Payment",
    "underpaid": "Underpaid",
    "confirmed": "Confirmed",
    "completed": "Completed",
    "expired": "Expired",
    "cancelled": "Cancelled",
}


def _slot_label(t, event=None):
    """Turn '10:00' into '10:00–10:20' using session_length from event."""
    sl = (event or _active).get("session_length", SESSION_LENGTH)
    try:
        start = datetime.strptime(t, "%H:%M")
        end = start + timedelta(minutes=sl)
        return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
    except Exception:
        return t


def sync_to_notion(booking_id):
    """Sync a single SQLite booking to the Bookings v2 Notion DB.
    Stores the Notion page_id back in SQLite so subsequent calls patch in place."""
    if not NOTION_API_KEY:
        return  # silently skip when no key configured
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return
        booking = dict(row)

        if booking["confirmed"]:
            status_name = "Confirmed"
        else:
            status_name = NOTION_STATUS_MAP.get(booking["status"], "Pending Payment")

        properties = {
            "Client Name": {"title": [{"text": {"content": booking["name"] or "(awaiting details)"}}]},
            "Status": {"select": {"name": status_name}},
            "Date": {"date": {"start": booking["date"]}},
            "Time Slot": {"rich_text": [{"text": {"content": _slot_label(booking["time"])}}]},
            "Instagram": {"rich_text": [{"text": {"content": booking.get("instagram") or ""}}]},
            "Deposit (CAD)": {"number": booking.get("paid_amount") or SESSION_PRICE},
            "Total (CAD)": {"number": SESSION_TOTAL},
            "Paid": {"checkbox": bool(booking["paid"])},
            "Booking ID (legacy)": {"number": booking["id"]},
        }
        # Notion rejects empty strings for email/phone — only set when present
        if booking.get("email"):
            properties["Email"] = {"email": booking["email"]}
        if booking.get("phone"):
            properties["Phone"] = {"phone_number": booking["phone"]}
        if booking.get("session_type"):
            properties["Session Type"] = {"select": {"name": booking["session_type"]}}
        if booking.get("paid"):
            properties["Payment Method"] = {"select": {"name": "e-Transfer"}}
        if booking.get("calendar_event_url"):
            properties["Calendar Event"] = {"url": booking["calendar_event_url"]}
        if booking.get("reserved_until"):
            properties["Reserved Until"] = {"date": {"start": booking["reserved_until"]}}

        page_id = booking.get("notion_page_id")
        if page_id:
            r = requests.patch(f"https://api.notion.com/v1/pages/{page_id}",
                               headers=NOTION_HEADERS,
                               json={"properties": properties}, timeout=15)
            if r.status_code >= 400:
                print(f"Notion patch failed ({r.status_code}): {r.text[:200]}")
        else:
            r = requests.post("https://api.notion.com/v1/pages",
                              headers=NOTION_HEADERS,
                              json={"parent": {"database_id": NOTION_DATABASE_ID},
                                    "properties": properties}, timeout=15)
            if r.status_code < 300:
                page_id = r.json().get("id")
                c.execute("UPDATE bookings SET notion_page_id=? WHERE id=?", (page_id, booking_id))
                conn.commit()
            else:
                print(f"Notion create failed ({r.status_code}): {r.text[:200]}")
        conn.close()
    except Exception as e:
        print(f"Notion sync error: {e}")


# ===== GOOGLE CALENDAR CONFIG =====
CALENDAR_ID = os.environ.get("BOOKING_CALENDAR_ID", "iryna.pashynska@gmail.com")
CALENDAR_TZ = os.environ.get("BOOKING_CALENDAR_TZ", "America/Edmonton")
# Filled in by run-calendar-helper.py via env at runtime; left empty here so import never fails
_calendar_helper = None  # type: ignore


def create_calendar_event_for_booking(booking_id):
    """Create a Google Calendar event by shelling out to the GCal MCP helper script.
    Stored event URL goes back into SQLite so Notion can render the link.
    Falls back gracefully if no helper is configured."""
    helper = os.environ.get("GCAL_HELPER")  # path to a CLI that wraps create_event
    if not helper:
        return None
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return None
        b = dict(row)

        start_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=SESSION_LENGTH)
        summary = f"📸 {b['name'] or 'Mini Session'} — {EVENT_TITLE}"
        description = (
            f"Mini Photo Session ({SESSION_LENGTH} min)\n"
            f"Client: {b['name']}\n"
            f"Phone: {b['phone']}\n"
            f"Email: {b['email']}\n"
            f"Instagram: {b.get('instagram') or ''}\n"
            f"Session type: {b.get('session_type') or ''}\n"
            f"Booking #{b['id']}\n"
            f"Location: {ev.get('location') if ev else 'TBD'}"
        )
        # Get event details for location
        event = get_event_by_id(b.get('event_id')) if b.get('event_id') else None
        location = event.get('location', '') if event else ''
        
        import subprocess as _sp
        result = _sp.run(
            [helper, "create",
             "--calendar", CALENDAR_ID,
             "--summary", summary,
             "--start", start_dt.isoformat(),
             "--end", end_dt.isoformat(),
             "--tz", CALENDAR_TZ,
             "--description", description,
             "--location", location],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"GCal helper failed: {result.stderr[:200]}")
            conn.close()
            return None
        out = json.loads(result.stdout.strip())
        event_id = out.get("id")
        event_url = out.get("htmlLink") or out.get("url")
        c.execute("UPDATE bookings SET calendar_event_id=?, calendar_event_url=? WHERE id=?",
                  (event_id, event_url, booking_id))
        conn.commit()
        conn.close()
        return event_url
    except Exception as e:
        print(f"Calendar error: {e}")
        return None

# ===== EVENTS CONFIG (YAML) =====
_EVENTS_PATH = os.path.join(os.path.dirname(__file__), "events.yaml")

def _load_events():
    """Load events from YAML, return list of event dicts."""
    with open(_EVENTS_PATH, "r") as f:
        data = yaml.safe_load(f)
    return data.get("events", []), data.get("settings", {})

EVENTS, SETTINGS = _load_events()

def get_active_event():
    """Return the first active event, or None."""
    for ev in EVENTS:
        if ev.get("status") == "active":
            return ev
    return EVENTS[0] if EVENTS else None

def get_event_by_date(date_str):
    """Find an event by its date."""
    for ev in EVENTS:
        if ev["date"] == date_str:
            return ev
    return None

def get_event_by_id(event_id):
    """Find an event by its id."""
    for ev in EVENTS:
        if ev["id"] == event_id:
            return ev
    return None

# Convenience accessors (backward-compatible)
_active = get_active_event() or {}
EVENT_TITLE = _active.get("title", "Mini Sessions")
SESSION_LENGTH = _active.get("session_length", 20)
BREAK_LENGTH = _active.get("break_length", 10)
SLOT_INTERVAL = _active.get("slot_interval", 30)
SESSION_PRICE = _active.get("deposit", 95)
SESSION_TOTAL = _active.get("full_price", 190)
EMAIL = SETTINGS.get("photographer_email", "")
RESERVATION_MINUTES = SETTINGS.get("reservation_minutes", 15)
DATE = _active.get("date", "")
START_TIME = _active.get("start_time", "10:00")
END_TIME = _active.get("end_time", "16:00")

# ===== DATABASE =====
DB_PATH = os.path.join(os.path.dirname(__file__), "bookings.db")

def init_db():
    """Create SQLite tables if they don't exist"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            instagram TEXT,
            session_type TEXT,
            status TEXT DEFAULT 'pending',
            paid BOOLEAN DEFAULT 0,
            confirmed BOOLEAN DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reserved_until TEXT,
            paid_amount REAL DEFAULT NULL,
            UNIQUE(date, time)
        )
    ''')
    # Lightweight migrations for columns added over time.
    for col, ddl in [
        ("paid_amount",         "ALTER TABLE bookings ADD COLUMN paid_amount REAL"),
        ("notion_page_id",      "ALTER TABLE bookings ADD COLUMN notion_page_id TEXT"),
        ("calendar_event_id",   "ALTER TABLE bookings ADD COLUMN calendar_event_id TEXT"),
        ("calendar_event_url",  "ALTER TABLE bookings ADD COLUMN calendar_event_url TEXT"),
        ("event_id",            "ALTER TABLE bookings ADD COLUMN event_id TEXT"),
    ]:
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()

init_db()

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def expire_reservations():
    """Mark expired reserved/pending_payment rows as 'expired' so slots open back up.
    Returns count of expired bookings."""
    conn = db_conn()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""
        SELECT id, notion_page_id FROM bookings
        WHERE confirmed = 0
          AND paid = 0
          AND reserved_until IS NOT NULL
          AND reserved_until <= ?
          AND status IN ('reserved', 'pending_payment')
    """, (now,))
    rows = c.fetchall()

    for row in rows:
        booking_id = row["id"]
        notion_page_id = row["notion_page_id"]

        # Update status to 'expired' instead of deleting
        c.execute("UPDATE bookings SET status='expired', reserved_until=NULL WHERE id=?",
                  (booking_id,))
        log.info(f"[expire] Booking #{booking_id} marked as expired")

        # Sync status to Notion
        if notion_page_id and NOTION_API_KEY:
            try:
                requests.patch(
                    f"https://api.notion.com/v1/pages/{notion_page_id}",
                    headers=NOTION_HEADERS,
                    json={"properties": {"Status": {"select": {"name": "Expired"}}}},
                    timeout=10
                )
            except Exception as e:
                log.error(f"[expire] Notion update failed for #{booking_id}: {e}")

    expired_count = len(rows)
    conn.commit()
    conn.close()
    return expired_count

# Generate time slots for a specific event
def generate_slots(event=None):
    """Generate time slots based on event config."""
    ev = event or _active
    if not ev:
        return []
    start = datetime.strptime(ev.get("start_time", START_TIME), "%H:%M")
    end = datetime.strptime(ev.get("end_time", END_TIME), "%H:%M")
    interval = ev.get("slot_interval", SLOT_INTERVAL)
    sl = ev.get("session_length", SESSION_LENGTH)
    slots = []
    current = start
    while current < end:
        slot_str = current.strftime("%H:%M")
        session_end = current + timedelta(minutes=sl)
        slots.append({
            "time": slot_str,
            "label": f"{slot_str} – {session_end.strftime('%H:%M')}",
            "reserved_until": None
        })
        current += timedelta(minutes=interval)
    return slots

SLOTS = generate_slots()

# ===== ROUTES =====

def _public_visible_events():
    """Events shown on the public landing — active or upcoming, not hidden."""
    out = []
    for ev in EVENTS:
        if ev.get("hidden"):
            continue
        if ev.get("status") not in ("active", "upcoming"):
            continue
        out.append(ev)
    return out

def _enrich_event_for_landing(ev):
    """Add computed fields used by the landing template (date_pretty, days_until, spots)."""
    from datetime import date as _date
    e = dict(ev)
    e.setdefault("type", "mini")
    e.setdefault("featured", False)
    e.setdefault("subtitle", "")
    try:
        d = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        e["date_pretty"] = d.strftime("%a, %B %-d, %Y") if hasattr(d, "strftime") else str(d)
        e["days_until"] = (d - _date.today()).days
    except Exception:
        e["date_pretty"] = ev.get("date", "")
        e["days_until"] = 0
    # spots accounting from the actual DB
    total = int(ev.get("total_spots") or len(generate_slots(ev)))
    booked = 0
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM bookings
            WHERE date=? AND status NOT IN ('cancelled','expired')
        """, (ev["date"],))
        booked = c.fetchone()[0] or 0
        conn.close()
    except Exception:
        pass
    e["spots"] = {"total": total, "booked": booked, "left": max(0, total - booked)}
    return e

@app.route("/")
def index():
    """Landing — new v2 design with event grid, featured banner, and booking drawer."""
    event_id = request.args.get("event")

    # ── Direct event link: still render v2 landing, but could auto-open drawer in future ──
    if event_id:
        ev = get_event_by_id(event_id)
        if not ev:
            return "Event not found", 404
        # Always use v2 landing; JavaScript will handle direct event linking if needed
        return render_template("index_v2.html", direct_event_id=event_id)

    # ── Render the new landing grid (v2 design) for all cases ──
    return render_template("index_v2.html")


def _render_single_event(ev):
    slots = generate_slots(ev)
    return render_template(
        "index.html",
        title=ev.get("title", EVENT_TITLE),
        date=ev["date"],
        price=ev.get("deposit", SESSION_PRICE),
        total=ev.get("full_price", SESSION_TOTAL),
        email=EMAIL,
        session_length=ev.get("session_length", SESSION_LENGTH),
        break_length=ev.get("break_length", BREAK_LENGTH),
        event_id=ev["id"],
        subtitle=ev.get("subtitle", ""),
        location=ev.get("location", ""),
        included=ev.get("included", []),
        photos=ev.get("photos", []),
        slots=slots,
    )

@app.route("/events")
def list_events():
    """API: list all events with full details including available spots."""
    result = []
    now = datetime.now()
    conn = db_conn()
    c = conn.cursor()

    for ev in EVENTS:
        if ev.get("status") in ("active", "upcoming", "completed"):
            # Calculate total and available spots
            slots = generate_slots(ev)
            total_spots = len(slots)

            c.execute("""
                SELECT time FROM bookings
                WHERE date=?
                  AND status NOT IN ('cancelled', 'expired')
                  AND (confirmed=1 OR reserved_until > ?)
            """, (ev["date"], now.isoformat()))
            booked_times = {row["time"] for row in c.fetchall()}
            available_spots = len([s for s in slots if s["time"] not in booked_times])

            # Get first photo URL
            photos = ev.get("photos", [])
            photo_url = photos[0] if photos else "/static/images/placeholder.jpg"

            result.append({
                "id": ev["id"],
                "title": ev.get("title", ""),
                "subtitle": ev.get("subtitle", ""),
                "description": ev.get("subtitle", ""),
                "date": ev["date"],
                "start_time": ev.get("start_time", ""),
                "end_time": ev.get("end_time", ""),
                "session_length": ev.get("session_length", 20),
                "break_length": ev.get("break_length", 10),
                "slot_interval": ev.get("slot_interval", 30),
                "deposit": ev.get("deposit", SESSION_PRICE),
                "full_price": ev.get("full_price", SESSION_TOTAL),
                "price": ev.get("deposit", SESSION_PRICE),
                "location": ev.get("location", "Calgary"),
                "status": ev.get("status", ""),
                "session_type": ev.get("session_type", "mini"),
                "type": ev.get("session_type", "mini"),
                "featured": ev.get("featured", False),
                "hidden": ev.get("hidden", False),
                "total_spots": total_spots,
                "spots_left": available_spots,
                "photo_url": photo_url,
                "photo": photo_url,
                "included": ev.get("included", []),
            })
    conn.close()
    return jsonify({"events": result})

@app.route("/slots/<date_str>")
def get_slots(date_str):
    # Support looking up by event_id via query param too
    event_id = request.args.get("event_id")
    if event_id:
        ev = get_event_by_id(event_id)
    else:
        ev = get_event_by_date(date_str)
    if not ev:
        return jsonify({"slots": [], "message": f"No event on {date_str}"})

    slots = generate_slots(ev)
    now = datetime.now()
    conn = db_conn()
    c = conn.cursor()

    # Single query instead of one per slot (N+1 → 1)
    c.execute("""
        SELECT time FROM bookings
        WHERE date=?
          AND status NOT IN ('cancelled', 'expired')
          AND (confirmed=1 OR reserved_until > ?)
    """, (ev["date"], now.isoformat()))
    booked_times = {row["time"] for row in c.fetchall()}
    conn.close()

    available_slots = [
        {"time": s["time"], "label": s["label"]}
        for s in slots
        if s["time"] not in booked_times
    ]

    return jsonify({
        "date": date_str,
        "event_id": ev["id"],
        "event_title": ev.get("title", ""),
        "slots": available_slots,
        "total": len(slots),
        "available": len(available_slots)
    })

@app.route("/reserve", methods=["POST"])
def reserve_slot():
    data = request.json or {}
    slot_time = data.get("time")
    event_id = data.get("event_id") or data.get("date")  # accept either
    client_name = data.get("name", "")
    client_email = data.get("email", "")
    client_phone = data.get("phone", "")
    client_ig = data.get("instagram", "")
    session_type = data.get("session_type", "")

    if not slot_time:
        return jsonify({"success": False, "error": "No time slot specified"}), 400

    # Resolve event
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev:
        return jsonify({"success": False, "error": "Event not found"}), 404
    event_date = ev["date"]

    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if isinstance(ip, str) and ',' in ip:
        ip = ip.split(',')[0].strip()

    if not check_rate_limit(ip):
        return jsonify({"success": False, "error": "Too many requests. Please wait 10 minutes."}), 429
    record_request(ip)

    now = datetime.now()
    expires = now + timedelta(minutes=RESERVATION_MINUTES)

    conn = db_conn()
    c = conn.cursor()
    booking_id = None

    try:
        # BEGIN IMMEDIATE acquires a write lock up-front, serialising concurrent
        # reserve attempts so the check-then-insert is atomic.
        c.execute("BEGIN IMMEDIATE")

        # Slot is taken if there is a confirmed booking OR an active (non-expired) reservation
        c.execute("""
            SELECT id FROM bookings
            WHERE date=? AND time=?
              AND status NOT IN ('cancelled', 'expired')
              AND (confirmed=1 OR reserved_until > ?)
        """, (event_date, slot_time, now.isoformat()))
        if c.fetchone():
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Slot is no longer available"})

        # Remove stale rows: cancelled, expired, and past-deadline reserved/pending
        # so the INSERT doesn't hit the UNIQUE(date, time) constraint.
        c.execute("""
            DELETE FROM bookings
            WHERE date=? AND time=?
              AND (
                status IN ('cancelled', 'expired')
                OR (status IN ('reserved', 'pending_payment') AND reserved_until <= ?)
              )
        """, (event_date, slot_time, now.isoformat()))

        c.execute("""
            INSERT INTO bookings
                (date, time, name, email, phone, instagram, session_type, status, reserved_until, event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
        """, (event_date, slot_time, client_name, client_email, client_phone, client_ig, session_type, expires.isoformat(), ev["id"]))

        if c.rowcount == 0:
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Slot just taken"})

        booking_id = c.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Notify photographer via Telegram
    _notify_new_reservation(
        booking_id=booking_id,
        client_name=client_name,
        client_email=client_email,
        event_date=event_date,
        slot_time=slot_time,
        event_title=ev.get("title", ""),
        session_type=session_type,
        client_ig=client_ig,
        client_phone=client_phone,
    )

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "event_id": ev["id"],
        "expires_at": expires.isoformat(),
        "message": f"Reserved for {RESERVATION_MINUTES} minutes. Complete payment before {expires.strftime('%H:%M')}."
    })

@app.route("/payment")
def payment():
    time = request.args.get("time")
    event_id = request.args.get("event_id")
    if not time:
        return redirect(url_for("index"))
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    return render_template("payment.html",
        time=time,
        name=request.args.get("name", ""),
        client_email=request.args.get("email", ""),
        phone=request.args.get("phone", ""),
        instagram=request.args.get("instagram", ""),
        session_type=request.args.get("session_type", ""),
        date=ev["date"] if ev else DATE,
        price=ev.get("deposit", SESSION_PRICE) if ev else SESSION_PRICE,
        email=EMAIL,
        session_length=ev.get("session_length", SESSION_LENGTH) if ev else SESSION_LENGTH,
        event_id=ev["id"] if ev else ""
    )


@app.route("/expired", methods=["GET", "POST"])
def expired_endpoint():
    """Manually trigger expired-reservation cleanup. Safe to call repeatedly."""
    deleted = expire_reservations()
    return jsonify({"success": True, "released": deleted,
                    "message": f"{deleted} expired slot(s) released"})

@app.route("/confirm", methods=["POST"])
def confirm_payment():
    data = request.json
    time = data.get("time")
    event_id = data.get("event_id")
    client_name = data.get("name", "")
    client_email = data.get("email", "")
    client_phone = data.get("phone", "")
    client_ig = data.get("instagram", "")
    session_type = data.get("session_type", "")

    ev = get_event_by_id(event_id) if event_id else get_active_event()
    event_date = ev["date"] if ev else DATE
    
    conn = db_conn()
    c = conn.cursor()
    
    # Update or insert booking as pending
    c.execute('''
        INSERT INTO bookings (date, time, name, email, phone, instagram, session_type, status, reserved_until, event_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_payment', ?, ?)
        ON CONFLICT(date, time) DO UPDATE SET
            name=excluded.name,
            email=excluded.email,
            phone=excluded.phone,
            instagram=excluded.instagram,
            session_type=excluded.session_type,
            status='pending_payment',
            reserved_until=excluded.reserved_until,
            event_id=excluded.event_id
    ''', (event_date, time, client_name, client_email, client_phone, client_ig, session_type,
          (datetime.now() + timedelta(minutes=RESERVATION_MINUTES)).isoformat(), ev["id"] if ev else None))
    conn.commit()
    
    # Get booking ID
    c.execute("SELECT id FROM bookings WHERE date=? AND time=?", (event_date, time))
    row = c.fetchone()
    booking_id = row["id"] if row else None
    conn.close()
    
    # Sync to Notion
    if booking_id:
        sync_to_notion(booking_id)

    # Notify with inline confirm/cancel buttons
    if booking_id:
        _notify_payment_pending(
            booking_id=booking_id,
            client_name=client_name,
            client_email=client_email,
            event_date=event_date,
            slot_time=time,
            event_title=ev.get("title", "Mini Session") if ev else "Mini Session",
            session_type=session_type,
            client_ig=client_ig,
            expected_deposit=ev.get("deposit", SESSION_PRICE) if ev else SESSION_PRICE,
            client_phone=client_phone,
        )

    # Start automatic e-Transfer checker
    if booking_id:
        _start_etransfer_checker(booking_id)

    log.info(f"[confirm] Booking #{booking_id} — {client_name} @ {time} — payment submitted, checker started")
    
    return jsonify({
        "success": True,
        "message": "Booking received! I'll confirm once payment is received.",
        "booking_id": booking_id
    })

@app.route("/success")
def success():
    booking_id = request.args.get("booking_id")
    booking = None
    if booking_id:
        conn = db_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        if row:
            booking = dict(row)
        conn.close()
    ev = get_event_by_id(booking["event_id"]) if booking and booking.get("event_id") else get_active_event()
    return render_template("success.html",
        email=EMAIL,
        date=ev["date"] if ev else DATE,
        price=ev.get("deposit", SESSION_PRICE) if ev else SESSION_PRICE,
        booking=booking
    )

@app.route("/backstage")
def backstage():
    """Hidden admin access — redirects to admin login or dashboard if already authenticated."""
    if _admin_authorized():
        return redirect(url_for("admin"))
    return redirect(url_for("admin_login", next=url_for("admin")))

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Browser login for the admin dashboard."""
    next_url = request.values.get("next") or url_for("admin")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("admin")

    # Already logged in — skip straight to admin
    if session.get("admin_authenticated"):
        return redirect(next_url)

    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        if isinstance(ip, str) and "," in ip:
            ip = ip.split(",")[0].strip()

        # Brute-force protection: max 10 login attempts per IP per 15 min
        if not check_login_rate_limit(ip):
            return render_template(
                "admin_login.html",
                error="Too many login attempts. Please wait 15 minutes.",
                next_url=next_url,
            ), 429

        record_login_attempt(ip)

        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_user = hmac.compare_digest(username, ADMIN_USER)
        valid_password = bool(ADMIN_PASSWORD) and hmac.compare_digest(password, ADMIN_PASSWORD)

        if valid_user and valid_password:
            session["admin_authenticated"] = True
            session.permanent = True  # respect PERMANENT_SESSION_LIFETIME
            log.info(f"[admin] Successful login from {ip}")
            return redirect(next_url)

        log.warning(f"[admin] Failed login attempt from {ip} (user={username!r})")
        return render_template(
            "admin_login.html",
            error="Incorrect username or password.",
            next_url=next_url,
        ), 401

    return render_template("admin_login.html", error=None, next_url=next_url)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin():
    """Admin dashboard — HTML view with Confirm/Cancel buttons."""
    # Get filter parameters
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    session_type = request.args.get("session_type", "")
    status = request.args.get("status", "")
    search = request.args.get("search", "").strip()
    page = request.args.get("page", "1")
    limit = request.args.get("limit", "50")

    try:
        page_num = int(page)
        if page_num < 1:
            page_num = 1
    except ValueError:
        page_num = 1

    try:
        limit_num = int(limit)
        if limit_num < 1 or limit_num > 1000:
            limit_num = 50
    except ValueError:
        limit_num = 50

    offset = (page_num - 1) * limit_num

    conn = db_conn()
    c = conn.cursor()

    # Build WHERE clause
    conditions = []
    params = []

    if date_from:
        conditions.append("date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date <= ?")
        params.append(date_to)
    if session_type:
        conditions.append("session_type = ?")
        params.append(session_type)
    if status and status != "all":
        if status == "pending":
            conditions.append("status IN ('pending_payment', 'reserved')")
        else:
            conditions.append("status = ?")
            params.append(status)
    if search:
        conditions.append("(name LIKE ? OR email LIKE ? OR phone LIKE ? OR instagram LIKE ?)")
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term, search_term])

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    # Get total count for pagination
    c.execute(f"SELECT COUNT(*) as total FROM bookings WHERE {where_clause}", params)
    total_count = c.fetchone()["total"]

    # Get filtered bookings
    order_clause = "ORDER BY date DESC, time ASC"
    sql = f"SELECT * FROM bookings WHERE {where_clause} {order_clause} LIMIT ? OFFSET ?"
    params_with_limit = params + [limit_num, offset]
    c.execute(sql, params_with_limit)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    # Calculate stats based on filtered results
    filtered_stats = {
        "total": len(rows),
        "confirmed": sum(1 for b in rows if b["status"] == "confirmed"),
        "pending": sum(1 for b in rows if b["status"] in ("pending_payment", "reserved")),
        "cancelled": sum(1 for b in rows if b["status"] == "cancelled"),
        "expired": sum(1 for b in rows if b["status"] == "expired"),
        "total_expected": sum(b.get("paid_amount", 0) or 0 for b in rows if b["status"] == "confirmed"),
    }

    # Get overall stats (unfiltered) for summary
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total, "
              "SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) as confirmed, "
              "SUM(CASE WHEN status IN ('pending_payment', 'reserved') THEN 1 ELSE 0 END) as pending, "
              "SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled, "
              "SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) as expired, "
              "SUM(CASE WHEN status = 'confirmed' THEN paid_amount ELSE 0 END) as total_expected FROM bookings")
    overall_row = c.fetchone()
    overall_stats = {
        "total": overall_row["total"] or 0,
        "confirmed": overall_row["confirmed"] or 0,
        "pending": overall_row["pending"] or 0,
        "cancelled": overall_row["cancelled"] or 0,
        "expired": overall_row["expired"] or 0,
        "total_expected": overall_row["total_expected"] or 0,
    }
    conn.close()

    # Get unique session types for dropdown
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT session_type FROM bookings WHERE session_type IS NOT NULL AND session_type != '' ORDER BY session_type")
    session_types = [row["session_type"] for row in c.fetchall()]
    conn.close()

    return render_template("admin.html",
                           bookings=rows,
                           filtered_stats=filtered_stats,
                           overall_stats=overall_stats,
                           events=EVENTS,
                           session_types=session_types,
                           filters={
                               "date_from": date_from,
                               "date_to": date_to,
                               "session_type": session_type,
                               "status": status,
                               "search": search,
                               "page": page_num,
                               "limit": limit_num,
                               "total_count": total_count,
                               "total_pages": (total_count + limit_num - 1) // limit_num if limit_num > 0 else 1
                           })

@app.route("/admin/export")
@admin_required
def admin_export():
    """Export bookings as CSV."""
    # Get filter parameters (same as admin dashboard)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    session_type = request.args.get("session_type", "")
    status = request.args.get("status", "")
    search = request.args.get("search", "").strip()

    conn = db_conn()
    c = conn.cursor()

    conditions = []
    params = []

    if date_from:
        conditions.append("date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date <= ?")
        params.append(date_to)
    if session_type:
        conditions.append("session_type = ?")
        params.append(session_type)
    if status and status != "all":
        if status == "pending":
            conditions.append("status IN ('pending_payment', 'reserved')")
        else:
            conditions.append("status = ?")
            params.append(status)
    if search:
        conditions.append("(name LIKE ? OR email LIKE ? OR phone LIKE ? OR instagram LIKE ?)")
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term, search_term])

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"SELECT * FROM bookings WHERE {where_clause} ORDER BY date DESC, time ASC"
    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    # Generate CSV
    import io
    import csv
    output = io.StringIO()
    if rows:
        fieldnames = rows[0].keys()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    else:
        # Write header only
        writer = csv.writer(output)
        writer.writerow(["No data matching filters"])

    # Create response
    from flask import Response
    filename = f"bookings-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.route("/admin/confirm", methods=["POST"])
@admin_required
def admin_confirm():
    data = request.json
    booking_id = data.get("booking_id")
    paid_amount = data.get("paid_amount") or SESSION_PRICE
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE bookings SET confirmed=1, paid=1, status='confirmed', paid_amount=? WHERE id=?",
              (paid_amount, booking_id))
    conn.commit()
    # Fetch booking details for email notification
    c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
    row = c.fetchone()
    booking = dict(row) if row else {}
    conn.close()

    # Side-effects: GCal event first (so Notion can include the link), then Notion
    event_url = create_calendar_event_for_booking(booking_id)
    sync_to_notion(booking_id)

    # Send confirmation email to client
    ev = get_event_by_id(booking.get("event_id"))
    if ev and booking:
        _send_client_email(
            to_email=booking.get("email", ""),
            client_name=booking.get("name", "Client"),
            event_date=ev["date"],
            slot_time=booking.get("time", ""),
            event_title=ev.get("title", "Mini Session"),
            booking_id=booking_id,
            location=ev.get("location")
        )

    log.info(f"[admin] Booking #{booking_id} confirmed, paid ${paid_amount}")

    return jsonify({"success": True, "calendar_event": event_url})

@app.route("/admin/cancel", methods=["POST"])
@admin_required
def admin_cancel():
    """Cancel a booking — frees the slot."""
    data = request.json
    booking_id = data.get("booking_id")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE bookings SET status='cancelled', reserved_until=NULL WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    sync_to_notion(booking_id)
    log.info(f"[admin] Booking #{booking_id} cancelled")
    return jsonify({"success": True})


@app.route("/admin/delete", methods=["POST"])
@admin_required
def admin_delete():
    """Delete a booking — permanent removal from database."""
    data = request.json
    booking_id = data.get("booking_id")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400
    conn = db_conn()
    c = conn.cursor()
    c.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    sync_to_notion(booking_id)
    log.info(f"[admin] Booking #{booking_id} permanently deleted")
    return jsonify({"success": True})


EVENTS_YAML_PATH = os.path.join(os.path.dirname(__file__), "events.yaml")
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/admin/events/<event_id>/update", methods=["POST"])
@admin_required
def admin_update_event(event_id):
    """Update event time/schedule settings and save to events.yaml."""
    data = request.json or {}

    with open(EVENTS_YAML_PATH) as fh:
        yaml_data = yaml.safe_load(fh)

    event = next((e for e in yaml_data.get("events", []) if e["id"] == event_id), None)
    if not event:
        return jsonify({"error": "Event not found"}), 404

    # Validate and apply time fields
    try:
        if "start_time" in data:
            datetime.strptime(data["start_time"], "%H:%M")
            event["start_time"] = data["start_time"]
        if "end_time" in data:
            datetime.strptime(data["end_time"], "%H:%M")
            event["end_time"] = data["end_time"]
        if "session_length" in data:
            sl = int(data["session_length"])
            if not (5 <= sl <= 120):
                return jsonify({"error": "session_length must be 5–120 minutes"}), 400
            event["session_length"] = sl
        if "break_length" in data:
            bl = int(data["break_length"])
            if not (0 <= bl <= 60):
                return jsonify({"error": "break_length must be 0–60 minutes"}), 400
            event["break_length"] = bl
        # slot_interval is always session_length + break_length
        event["slot_interval"] = event.get("session_length", 20) + event.get("break_length", 10)
        if "title" in data:
            event["title"] = str(data["title"])[:120]
        if "subtitle" in data:
            event["subtitle"] = str(data["subtitle"])[:200]
        if "deposit" in data:
            event["deposit"] = float(data["deposit"])
        if "full_price" in data:
            event["full_price"] = float(data["full_price"])
        if "status" in data and data["status"] in ("active", "upcoming", "completed"):
            event["status"] = data["status"]
        if "location" in data:
            event["location"] = str(data["location"])[:300]
    except ValueError as e:
        return jsonify({"error": f"Invalid value: {e}"}), 400

    # Validate that start < end
    try:
        s = datetime.strptime(event["start_time"], "%H:%M")
        e_ = datetime.strptime(event["end_time"], "%H:%M")
        if s >= e_:
            return jsonify({"error": "start_time must be before end_time"}), 400
    except Exception:
        pass

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

    # Reload events in memory
    global EVENTS, SETTINGS, _active, EVENT_TITLE, SESSION_LENGTH, BREAK_LENGTH, SLOT_INTERVAL, SESSION_PRICE, SESSION_TOTAL, DATE, START_TIME, END_TIME, SLOTS
    EVENTS, SETTINGS = _load_events()
    _active = get_active_event() or {}
    EVENT_TITLE = _active.get("title", "Mini Sessions")
    SESSION_LENGTH = _active.get("session_length", 20)
    BREAK_LENGTH = _active.get("break_length", 10)
    SLOT_INTERVAL = _active.get("slot_interval", 30)
    SESSION_PRICE = _active.get("deposit", 95)
    SESSION_TOTAL = _active.get("full_price", 190)
    DATE = _active.get("date", "")
    START_TIME = _active.get("start_time", "10:00")
    END_TIME = _active.get("end_time", "16:00")
    SLOTS = generate_slots()

    # Compute slot preview for the response
    updated_event = next((e for e in EVENTS if e["id"] == event_id), event)
    slots_preview = generate_slots(updated_event)

    log.info(f"[admin] Event {event_id} settings updated: {data}")
    return jsonify({"success": True, "slots_count": len(slots_preview), "event": {
        "id": updated_event["id"],
        "start_time": updated_event.get("start_time"),
        "end_time": updated_event.get("end_time"),
        "session_length": updated_event.get("session_length"),
        "break_length": updated_event.get("break_length"),
        "slot_interval": updated_event.get("slot_interval"),
    }})


@app.route("/admin/photos/<event_id>", methods=["GET"])
@admin_required
def admin_get_photos(event_id):
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"error": "Event not found"}), 404
    return jsonify({"photos": ev.get("photos", [])})

@app.route("/admin/photos/<event_id>/upload", methods=["POST"])
@admin_required
def admin_upload_photo(event_id):
    """Upload a new photo for an event (replaces slot index if provided)."""
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"error": "Event not found"}), 404

    f = request.files.get("photo")
    if not f or not _allowed_file(f.filename):
        return jsonify({"error": "Invalid file. Allowed: jpg, jpeg, png, webp"}), 400

    slot_index = request.form.get("slot_index")  # optional — which photo slot to replace

    import uuid
    ext = f.filename.rsplit(".", 1)[1].lower()
    filename = f"{event_id}_{uuid.uuid4().hex[:8]}.{ext}"
    images_dir = os.path.join(app.root_path, "static", "images")
    os.makedirs(images_dir, exist_ok=True)
    save_path = os.path.join(images_dir, filename)
    f.save(save_path)
    url = f"/images/{filename}"

    # Update events.yaml
    with open(EVENTS_YAML_PATH) as fh:
        data = yaml.safe_load(fh)

    for ev_data in data.get("events", []):
        if ev_data["id"] == event_id:
            photos = ev_data.get("photos") or []
            if slot_index is not None:
                idx = int(slot_index)
                if 0 <= idx < len(photos):
                    # delete old file if it lives in our images dir
                    old = photos[idx].lstrip("/")
                    old_path = os.path.join(app.root_path, "static", old)
                    if os.path.exists(old_path) and old_path != save_path:
                        try: os.remove(old_path)
                        except Exception: pass
                    photos[idx] = url
                else:
                    photos.append(url)
            else:
                photos.append(url)
            ev_data["photos"] = photos
            break

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False)

    # Reload events in memory
    global EVENTS, SETTINGS
    _cfg = yaml.safe_load(open(EVENTS_YAML_PATH))
    EVENTS = _cfg.get("events", [])
    SETTINGS = _cfg.get("settings", {})

    log.info(f"[admin] Photo uploaded for {event_id}: {url}")
    return jsonify({"success": True, "url": url})

@app.route("/admin/photos/<event_id>/delete", methods=["POST"])
@admin_required
def admin_delete_photo(event_id):
    """Remove a photo from an event's list."""
    slot_index = (request.json or {}).get("slot_index")
    if slot_index is None:
        return jsonify({"error": "slot_index required"}), 400

    with open(EVENTS_YAML_PATH) as fh:
        data = yaml.safe_load(fh)

    deleted_url = None
    for ev_data in data.get("events", []):
        if ev_data["id"] == event_id:
            photos = ev_data.get("photos") or []
            idx = int(slot_index)
            if 0 <= idx < len(photos):
                deleted_url = photos.pop(idx)
            ev_data["photos"] = photos
            break

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False)

    # delete file from disk if it's ours
    if deleted_url:
        old_path = os.path.join(app.root_path, "static", deleted_url.lstrip("/"))
        if os.path.exists(old_path):
            try: os.remove(old_path)
            except Exception: pass

    global EVENTS, SETTINGS
    _cfg = yaml.safe_load(open(EVENTS_YAML_PATH))
    EVENTS = _cfg.get("events", [])
    SETTINGS = _cfg.get("settings", {})

    return jsonify({"success": True})


@app.route("/booking-status")
def booking_status():
    """API for live client page polling — returns booking status + paid_amount."""
    booking_id = request.args.get("booking_id")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400
    conn = db_conn()
    row = conn.execute("SELECT status, confirmed, paid, paid_amount, name FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    b = dict(row)
    return jsonify({
        "status": b["status"],
        "confirmed": bool(b["confirmed"]),
        "paid": bool(b["paid"]),
        "paid_amount": b.get("paid_amount"),
        "name": b.get("name", "")
    })

# ===== TELEGRAM WEBHOOK =====
@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Handle inline button presses from Telegram bot (confirm/cancel bookings)."""
    data = request.json
    if not data or "callback_query" not in data:
        return jsonify({"ok": True})

    cb = data["callback_query"]
    cb_data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    from_user = cb.get("from", {}).get("first_name", "Admin")

    # Acknowledge immediately (removes spinner on button)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
        json={"callback_query_id": cb["id"]},
        timeout=5
    )

    if not cb_data:
        return jsonify({"ok": True})

    action, _, booking_id_str = cb_data.partition(":")
    try:
        booking_id = int(booking_id_str)
    except (ValueError, TypeError):
        return jsonify({"ok": True})

    if action == "confirm":
        conn = db_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            _tg_send(chat_id, f"❌ Booking #{booking_id} not found.")
            return jsonify({"ok": True})

        booking = dict(row)
        if booking["confirmed"]:
            conn.close()
            _tg_send(chat_id, f"ℹ️ Booking #{booking_id} already confirmed.")
            return jsonify({"ok": True})

        c.execute(
            "UPDATE bookings SET confirmed=1, paid=1, status='confirmed', paid_amount=? WHERE id=?",
            (SESSION_PRICE, booking_id)
        )
        conn.commit()
        conn.close()

        ev = get_event_by_id(booking.get("event_id"))
        event_title = ev.get("title", "Mini Session") if ev else "Mini Session"
        event_date = ev["date"] if ev else booking["date"]

        create_calendar_event_for_booking(booking_id)
        sync_to_notion(booking_id)
        if ev and booking:
            _send_client_email(
                to_email=booking.get("email", ""),
                client_name=booking.get("name", "Client"),
                event_date=event_date,
                slot_time=booking.get("time", ""),
                event_title=event_title,
                booking_id=booking_id,
                location=ev.get("location")
            )

        log.info(f"[tg-webhook] Booking #{booking_id} confirmed by {from_user}")
        updated_text = (
            f"✅ <b>CONFIRMED</b> by {from_user}\n\n"
            f"👤 {booking.get('name', 'Client')}\n"
            f"📅 {booking.get('date')} @ {booking.get('time')}\n"
            f"🎉 {event_title}\n"
            f"📋 Booking #{booking_id}\n"
            f"📧 Email sent to client"
        )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id,
                  "text": updated_text, "parse_mode": "HTML"},
            timeout=5
        )
        other_chat = TELEGRAM_ADMIN_CHAT_ID if str(chat_id) != TELEGRAM_ADMIN_CHAT_ID else TELEGRAM_CHAT_ID
        if other_chat:
            _tg_send(other_chat, updated_text)

    elif action == "cancel":
        conn = db_conn()
        c = conn.cursor()
        c.execute("UPDATE bookings SET status='cancelled', reserved_until=NULL WHERE id=?", (booking_id,))
        conn.commit()
        conn.close()
        sync_to_notion(booking_id)

        log.info(f"[tg-webhook] Booking #{booking_id} cancelled by {from_user}")
        updated_text = f"❌ <b>CANCELLED</b> by {from_user}\n📋 Booking #{booking_id}"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id,
                  "text": updated_text, "parse_mode": "HTML"},
            timeout=5
        )
        other_chat = TELEGRAM_ADMIN_CHAT_ID if str(chat_id) != TELEGRAM_ADMIN_CHAT_ID else TELEGRAM_CHAT_ID
        if other_chat:
            _tg_send(other_chat, updated_text)

    return jsonify({"ok": True})


# ===== RUN =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
