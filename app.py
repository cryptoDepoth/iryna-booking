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
# Write log to persistent volume when available, else next to app
_log_dir = os.environ.get("BACKUP_DIR", "").replace("/backups", "") or os.path.dirname(__file__)
_log_path = os.path.join(_log_dir, 'booking.log')
try:
    os.makedirs(_log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(_log_path),
            logging.StreamHandler()
        ]
    )
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.StreamHandler()]
    )
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Stable secret key ──────────────────────────────────────────────────────────
# Priority: FLASK_SECRET_KEY env var → /data/.flask_secret (auto-generated once)
# NEVER fall back to os.urandom() here — that would invalidate sessions on every restart.
_secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
if not _secret_key:
    _secret_file = os.path.join(
        os.environ.get("BACKUP_DIR", "").replace("/backups", "") or os.path.dirname(__file__),
        ".flask_secret"
    )
    try:
        if os.path.exists(_secret_file):
            with open(_secret_file) as _sf:
                _secret_key = _sf.read().strip()
        if not _secret_key:
            import secrets as _secrets
            _secret_key = _secrets.token_hex(32)
            os.makedirs(os.path.dirname(_secret_file), exist_ok=True)
            with open(_secret_file, "w") as _sf:
                _sf.write(_secret_key)
            log.info(f"[secret] Generated new stable secret key → {_secret_file}")
        else:
            log.info(f"[secret] Loaded stable secret key from {_secret_file}")
    except Exception as _e:
        log.warning(f"[secret] Could not persist secret key: {_e} — using ephemeral key")
        import secrets as _secrets
        _secret_key = _secrets.token_hex(32)
app.secret_key = _secret_key

PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable)

# ===== GLOBAL E-TRANSFER WATCHER (replaces per-booking Popen) =====
import threading as _threading

_watcher_started = False

def _watcher_thread():
    """Daemon thread — continuously checks all pending bookings against new emails."""
    import time as _time
    CHECK_INTERVAL = 30  # seconds
    log_w = logging.getLogger("watcher")
    log_w.info("[watcher] Global e-Transfer watcher started")

    from check_etransfer_v2 import (
        get_pending_bookings, get_emails, is_etransfer_email, check_single_email
    )

    while True:
        try:
            pending = get_pending_bookings(within_minutes=30)
            if pending:
                emails = get_emails()
                if emails:
                    for email in emails:
                        if is_etransfer_email(email):
                            check_single_email(email, pending)
            else:
                log_w.debug("[watcher] No pending bookings")
        except Exception as e:
            log_w.error(f"[watcher] Error: {e}")

        _time.sleep(CHECK_INTERVAL)


def _start_global_watcher():
    global _watcher_started
    if _watcher_started:
        return
    t = _threading.Thread(target=_watcher_thread, daemon=True, name="etransfer-watcher")
    t.start()
    _watcher_started = True
    log.info("[main] Started global e-Transfer watcher thread")


# Start watcher once at module import (Gunicorn worker startup)
_start_global_watcher()

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
# Fall back to ADMIN_KEY for browser login if ADMIN_PASSWORD is unset, so a
# single secret is enough to operate both the form-login and the X-Admin-Key
# API access. Operators can still set ADMIN_PASSWORD separately if they want
# different values.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "") or ADMIN_KEY

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


def _send_email_raw(to_email, client_name, subject, plain, html):
    """Low-level: send multipart/alternative email via Himalaya CLI."""
    if not to_email:
        return False
    try:
        import subprocess
        boundary = f"====pashynska_{abs(hash(subject))}===="
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
            log.info(f"[email] Sent '{subject}' to {to_email}")
            return True
        else:
            log.error(f"[email] Himalaya failed ({subject}): {result.stderr[:200]}")
            return False
    except Exception as e:
        log.error(f"[email] Send failed ({subject}): {e}")
        return False


def _send_abandoned_email(booking):
    """Send 'You were so close!' recovery email to a client who didn't complete payment."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    slot_time = booking.get("time", "")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev or not email:
        return False

    date_nice = ""
    try:
        date_nice = datetime.strptime(ev["date"], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_nice = ev.get("date", "")

    booking_url = f"https://{os.environ.get('SITE_HOST', 'www.pashynska.agency')}"
    subject = f"Still thinking about your photo session? 📸"

    plain = (
        f"Hi {name},\n\n"
        f"We noticed you started booking a mini session for {date_nice} at {slot_time} "
        f"but didn't quite finish.\n\n"
        f"Totally okay — life gets busy! But if you're still thinking about it, "
        f"there might still be spots available.\n\n"
        f"Book here: {booking_url}\n\n"
        f"No pressure at all — just didn't want you to miss out! 🌸\n\n"
        f"Warmly,\nIryna\n@pashynska.photo"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.07);">
  <tr><td style="background:linear-gradient(135deg,#b8a4c8 0%,#9b5e8a 100%);padding:36px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:36px;">📸</p>
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:normal;letter-spacing:1px;">You were so close!</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.85);font-size:14px;">Pashynska Photography</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 20px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{name}</strong> 👋</p>
    <p style="margin:0 0 16px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      We noticed you started booking a mini session for
      <strong>{ev.get('title', 'your session')}</strong> on <strong>{date_nice} at {slot_time}</strong>
      but didn't quite finish.
    </p>
    <p style="margin:0 0 28px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      Totally okay — life gets busy! But if you're still thinking about it,
      spots fill up fast and I'd love to have you. 🌸
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      <tr><td align="center">
        <a href="{booking_url}" style="display:inline-block;background:linear-gradient(135deg,#c084a8,#9b5e8a);color:#fff;text-decoration:none;padding:14px 36px;border-radius:50px;font-size:15px;letter-spacing:.5px;">
          Check available spots →
        </a>
      </td></tr>
    </table>
    <p style="margin:0;font-size:13px;color:#b8a0b0;text-align:center;line-height:1.6;">
      No pressure at all — just didn't want you to miss out!
    </p>
  </td></tr>
  <tr><td style="background:#f9f1f5;padding:20px 40px;text-align:center;border-top:1px solid #f0e0e8;">
    <p style="margin:0;font-size:12px;color:#b8a0b0;">Pashynska Photography · Calgary, AB · Canada<br>
    <a href="https://instagram.com/pashynska.photo" style="color:#c084a8;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


def _send_reminder_email(booking):
    """Send 48-hour pre-session reminder email."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    slot_time = booking.get("time", "")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev or not email:
        return False

    date_nice = ""
    try:
        date_nice = datetime.strptime(ev["date"], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_nice = ev.get("date", "")

    location = ev.get("location", "Location details coming soon")
    subject = f"Your session is in 2 days! 🌸 — {date_nice} at {slot_time}"

    plain = (
        f"Hi {name},\n\n"
        f"Just a friendly reminder — your mini photo session is coming up in 2 days!\n\n"
        f"📅 {date_nice}\n"
        f"⏰ {slot_time}\n"
        f"📍 {location}\n\n"
        f"A few tips to make the most of your session:\n"
        f"• Wear colours that complement each other (avoid busy patterns)\n"
        f"• Arrive 5 minutes early so we can start relaxed\n"
        f"• Bring any props you love — a blanket, flowers, a favourite hat\n"
        f"• Most importantly — just have fun! I'll guide you the whole time 😊\n\n"
        f"Any questions? DM me on Instagram @pashynska.photo\n\n"
        f"See you soon!\n"
        f"Iryna 🌸"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.07);">
  <tr><td style="background:linear-gradient(135deg,#c084a8 0%,#9b5e8a 100%);padding:36px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:36px;">🌸</p>
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:normal;letter-spacing:1px;">See you in 2 days!</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.85);font-size:14px;">Pashynska Photography</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 20px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{name}</strong>! 👋</p>
    <p style="margin:0 0 24px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      Your mini photo session is just <strong>2 days away</strong>! Here are the details:
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;border-radius:12px;margin-bottom:28px;">
      <tr><td style="padding:20px 24px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;color:#7a5a6a;font-size:14px;">🗓 Date</td>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{date_nice}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;color:#7a5a6a;font-size:14px;">⏰ Time</td>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{slot_time}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#7a5a6a;font-size:14px;">📍 Location</td>
            <td style="padding:8px 0;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{location}</strong></td>
          </tr>
        </table>
      </td></tr>
    </table>
    <h3 style="margin:0 0 12px;color:#5a3d4a;font-size:15px;">Tips for a beautiful session:</h3>
    <table cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">👗 &nbsp;Wear colours that complement each other — avoid busy patterns</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">⏱ &nbsp;Arrive 5 minutes early so we can start relaxed</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">🎀 &nbsp;Bring props you love — a blanket, flowers, a favourite hat</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">😊 &nbsp;Just have fun — I'll guide you the whole time!</td></tr>
    </table>
    <p style="margin:0 0 8px;font-size:14px;color:#7a5a6a;">Questions? DM me on Instagram
      <a href="https://instagram.com/pashynska.photo" style="color:#c084a8;text-decoration:none;">@pashynska.photo</a>
    </p>
  </td></tr>
  <tr><td style="padding:24px 40px;text-align:left;border-top:1px solid #f0e0e8;">
    <p style="margin:0;font-size:15px;color:#5a3d4a;">See you soon! 🌸</p>
    <p style="margin:8px 0 0;font-size:14px;color:#9b5e8a;"><strong>Iryna Pashynska</strong><br>
    <a href="https://instagram.com/pashynska.photo" style="color:#c084a8;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
  <tr><td style="background:#f9f1f5;padding:16px 40px;text-align:center;border-top:1px solid #f0e0e8;">
    <p style="margin:0;font-size:12px;color:#b8a0b0;">Pashynska Photography · Calgary, AB · Canada</p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


def _send_24h_reminder_email(booking):
    """Send 24-hour pre-session reminder email — short and punchy."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    slot_time = booking.get("time", "")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev or not email:
        return False

    date_nice = ""
    try:
        date_nice = datetime.strptime(ev["date"], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_nice = ev.get("date", "")

    location = ev.get("location", "Location details coming soon")
    subject = f"Tomorrow: your session at {slot_time}! 🌸 — {date_nice}"

    plain = (
        f"Hi {name},\n\n"
        f"Your mini photo session is **tomorrow**!\n\n"
        f"📅 {date_nice}\n"
        f"⏰ {slot_time}\n"
        f"📍 {location}\n\n"
        f"Quick prep checklist:\n"
        f"• Soft, coordinating colours (lilac, cream, white, pastels)\n"
        f"• Avoid neon and busy patterns\n"
        f"• Arrive 5 min early\n"
        f"• Bring any favourite prop (blanket, flowers, hat)\n\n"
        f"Any last-minute questions? DM @pashynska.photo\n\n"
        f"See you tomorrow!\n"
        f"Iryna 🌸"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.07);">
  <tr><td style="background:linear-gradient(135deg,#c084a8 0%,#9b5e8a 100%);padding:36px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:36px;">🌸</p>
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:normal;letter-spacing:1px;">See you tomorrow!</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.85);font-size:14px;">Pashynska Photography</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 20px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{name}</strong>! 👋</p>
    <p style="margin:0 0 24px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      Your mini photo session is <strong>tomorrow</strong>! Here's everything you need:
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;border-radius:12px;margin-bottom:28px;">
      <tr><td style="padding:20px 24px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;color:#7a5a6a;font-size:14px;">🗓 Date</td>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{date_nice}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;color:#7a5a6a;font-size:14px;">⏰ Time</td>
            <td style="padding:8px 0;border-bottom:1px solid #f0e0e8;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{slot_time}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#7a5a6a;font-size:14px;">📍 Location</td>
            <td style="padding:8px 0;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{location}</strong></td>
          </tr>
        </table>
      </td></tr>
    </table>
    <h3 style="margin:0 0 12px;color:#5a3d4a;font-size:15px;">Quick prep checklist:</h3>
    <table cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">👗 &nbsp;Soft, coordinating colours — lilac, cream, white, pastels</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">⏱ &nbsp;Arrive 5 minutes early</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">🎀 &nbsp;Bring a favourite prop — blanket, flowers, hat</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">😊 &nbsp;Most importantly — just have fun!</td></tr>
    </table>
    <p style="margin:0 0 8px;font-size:14px;color:#7a5a6a;">Questions? DM me on Instagram
      <a href="https://instagram.com/pashynska.photo" style="color:#c084a8;text-decoration:none;">@pashynska.photo</a>
    </p>
  </td></tr>
  <tr><td style="background:#f9f1f5;padding:16px 40px;text-align:center;border-top:1px solid #f0e0e8;">
    <p style="margin:0;font-size:12px;color:#b8a0b0;">Pashynska Photography · Calgary, AB · Canada</p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


def _send_review_email(booking):
    """Send post-session review request email (5 days after session)."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not email:
        return False

    insta_url = "https://instagram.com/pashynska.photo"
    google_review_url = os.environ.get("GOOGLE_REVIEW_URL", "https://g.page/r/pashynska-photography/review")

    subject = "How were your photos? 🌸"

    plain = (
        f"Hi {name},\n\n"
        f"I hope you've had a chance to look through your photos and love them as much as I do!\n\n"
        f"If you enjoyed your session, a quick review means the absolute world to a small business like mine. "
        f"It takes just 30 seconds:\n\n"
        f"⭐ Google review: {google_review_url}\n"
        f"📸 Tag me on Instagram: {insta_url}\n\n"
        f"And of course — I'd LOVE to see you again for your next session! 🌸\n\n"
        f"Thank you so much for trusting me to capture your memories.\n\n"
        f"Warmly,\nIryna\n@pashynska.photo"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.07);">
  <tr><td style="background:linear-gradient(135deg,#e8c4d8 0%,#c084a8 100%);padding:36px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:36px;">⭐</p>
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:normal;letter-spacing:1px;">How were your photos?</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.85);font-size:14px;">Pashynska Photography</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 16px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{name}</strong>! 🌸</p>
    <p style="margin:0 0 20px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      I hope you've had a chance to look through your photos and love them as much as I do!
      It was such a pleasure photographing you.
    </p>
    <p style="margin:0 0 28px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      If you enjoyed your session, a quick review means the absolute world to a small business like mine — it helps other families and couples find me. It takes just 30 seconds! ✨
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      <tr><td align="center" style="padding-bottom:12px;">
        <a href="{google_review_url}" style="display:inline-block;background:linear-gradient(135deg,#f4c430,#f0a030);color:#fff;text-decoration:none;padding:14px 32px;border-radius:50px;font-size:15px;letter-spacing:.5px;">
          ⭐ Leave a Google Review
        </a>
      </td></tr>
      <tr><td align="center">
        <a href="{insta_url}" style="display:inline-block;background:linear-gradient(135deg,#c084a8,#9b5e8a);color:#fff;text-decoration:none;padding:12px 28px;border-radius:50px;font-size:14px;letter-spacing:.5px;">
          📸 Tag me on Instagram
        </a>
      </td></tr>
    </table>
    <p style="margin:20px 0 0;font-size:14px;color:#b8a0b0;text-align:center;line-height:1.6;">
      And I'd love to see you again for your next session! 💜
    </p>
  </td></tr>
  <tr><td style="padding:24px 40px;text-align:left;border-top:1px solid #f0e0e8;">
    <p style="margin:0;font-size:15px;color:#5a3d4a;">Thank you so much! 🌸</p>
    <p style="margin:8px 0 0;font-size:14px;color:#9b5e8a;"><strong>Iryna Pashynska</strong><br>
    <a href="{insta_url}" style="color:#c084a8;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
  <tr><td style="background:#f9f1f5;padding:16px 40px;text-align:center;border-top:1px solid #f0e0e8;">
    <p style="margin:0;font-size:12px;color:#b8a0b0;">Pashynska Photography · Calgary, AB · Canada</p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


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
# EVENTS_YAML_PATH env var lets Fly.io (or any deployment) store events on
# the persistent volume (/data/events.yaml) so they survive restarts/redeploys.
# start.sh copies the bundled events.yaml to /data on first run.
_EVENTS_PATH = (
    os.environ.get("EVENTS_YAML_PATH")
    or os.path.join(os.path.dirname(__file__), "events.yaml")
)

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
# DB_PATH: can be overridden via env var DB_PATH.
# Default: ~/.pashynska-data/bookings.db — OUTSIDE the app folder so the
# database survives code updates, git pulls, or server redeployments.
_default_data_dir = os.path.join(os.path.expanduser("~"), ".pashynska-data")
DB_PATH = os.environ.get("DB_PATH") or os.path.join(_default_data_dir, "bookings.db")
BACKUP_DIR = os.environ.get("BACKUP_DIR") or os.path.join(_default_data_dir, "backups")

# Ensure directories exist
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
log.info(f"[db] Database: {DB_PATH}")
log.info(f"[db] Backups:  {BACKUP_DIR}")


def init_db():
    """Create SQLite tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # ── bookings table ──
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

    # ── clients table — one row per unique email ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            instagram TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            notes TEXT DEFAULT '',
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            total_bookings INTEGER DEFAULT 0,
            total_confirmed INTEGER DEFAULT 0,
            total_paid REAL DEFAULT 0.0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── client_notes — timestamped notes per client ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS client_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        )
    ''')

    # ── processed_emails — prevent double-processing of same Interac email ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS processed_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            booking_id INTEGER,
            amount REAL,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── migrations: add columns that didn't exist in older installs ──
    _migrations = [
        ("bookings",  "paid_amount",       "ALTER TABLE bookings ADD COLUMN paid_amount REAL"),
        ("bookings",  "notion_page_id",    "ALTER TABLE bookings ADD COLUMN notion_page_id TEXT"),
        ("bookings",  "calendar_event_id", "ALTER TABLE bookings ADD COLUMN calendar_event_id TEXT"),
        ("bookings",  "calendar_event_url","ALTER TABLE bookings ADD COLUMN calendar_event_url TEXT"),
        ("bookings",  "event_id",          "ALTER TABLE bookings ADD COLUMN event_id TEXT"),
        ("clients",   "tags",              "ALTER TABLE clients ADD COLUMN tags TEXT DEFAULT '[]'"),
        ("clients",   "notes",             "ALTER TABLE clients ADD COLUMN notes TEXT DEFAULT ''"),
        # Automated email tracking
        ("bookings",  "abandoned_email_sent",  "ALTER TABLE bookings ADD COLUMN abandoned_email_sent TEXT"),
        ("bookings",  "reminder_email_sent",   "ALTER TABLE bookings ADD COLUMN reminder_email_sent TEXT"),
        ("bookings",  "reminder_24h_email_sent","ALTER TABLE bookings ADD COLUMN reminder_24h_email_sent TEXT"),
        ("bookings",  "review_email_sent",     "ALTER TABLE bookings ADD COLUMN review_email_sent TEXT"),
        # first_booking_at / last_booking_at for clients table
        ("clients",   "first_booking_at",  "ALTER TABLE clients ADD COLUMN first_booking_at TEXT"),
        ("clients",   "last_booking_at",   "ALTER TABLE clients ADD COLUMN last_booking_at TEXT"),
        ("bookings",  "confirmation_token", "ALTER TABLE bookings ADD COLUMN confirmation_token TEXT"),
        # processed_emails ledger for e-Transfer safety
        ("_meta",     "processed_emails",  "CREATE TABLE IF NOT EXISTS processed_emails (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL, booking_id INTEGER, amount REAL, processed_at TEXT DEFAULT CURRENT_TIMESTAMP)"),
    ]
    for _tbl, _col, _ddl in _migrations:
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    conn.close()


init_db()


# ─────────────────────────────────────────────────────────────────────────────
#  BACKGROUND EMAIL SCHEDULER
#  Runs every 5 minutes in a daemon thread. Handles:
#    1. Abandoned booking recovery  (2h after expiry)
#    2. Pre-session reminder         (48h before session)
#    3. Post-session review request  (5 days after session)
# ─────────────────────────────────────────────────────────────────────────────

def _run_email_scheduler():
    import threading as _threading
    # Small initial delay so the server has time to fully start
    _threading.Event().wait(60)
    while True:
        try:
            _process_abandoned_emails()
            _process_reminder_emails()      # 48h
            _process_24h_reminder_emails()  # 24h
            _process_review_emails()
        except Exception as _e:
            log.error(f"[scheduler] Unexpected error: {_e}")
        _threading.Event().wait(300)  # run every 5 minutes


def _process_abandoned_emails():
    """Send recovery emails to clients who reserved but never paid (2h cooldown)."""
    now = datetime.now()
    cutoff = (now - timedelta(hours=2)).isoformat()
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM bookings
        WHERE status = 'expired'
          AND abandoned_email_sent IS NULL
          AND created_at <= ?
          AND email IS NOT NULL AND email != ''
    """, (cutoff,)).fetchall()
    conn.close()
    for row in rows:
        b = dict(row)
        try:
            ok = _send_abandoned_email(b)
            conn2 = db_conn()
            conn2.execute(
                "UPDATE bookings SET abandoned_email_sent=? WHERE id=?",
                (now.isoformat(), b["id"])
            )
            conn2.commit()
            conn2.close()
            if ok:
                log.info(f"[scheduler] Abandoned email sent → booking #{b['id']} ({b.get('email')})")
        except Exception as e:
            log.error(f"[scheduler] Abandoned email failed for #{b['id']}: {e}")


def _process_reminder_emails():
    """Send 48h pre-session reminders to confirmed bookings."""
    now = datetime.now()
    # Window: sessions happening between 46h and 50h from now (4h window to avoid duplicates)
    date_from = (now + timedelta(hours=46)).strftime("%Y-%m-%d")
    date_to   = (now + timedelta(hours=50)).strftime("%Y-%m-%d")
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM bookings
        WHERE status = 'confirmed' AND confirmed = 1
          AND reminder_email_sent IS NULL
          AND date BETWEEN ? AND ?
          AND email IS NOT NULL AND email != ''
    """, (date_from, date_to)).fetchall()
    conn.close()
    for row in rows:
        b = dict(row)
        try:
            ok = _send_reminder_email(b)
            conn2 = db_conn()
            conn2.execute(
                "UPDATE bookings SET reminder_email_sent=? WHERE id=?",
                (now.isoformat(), b["id"])
            )
            conn2.commit()
            conn2.close()
            if ok:
                log.info(f"[scheduler] Reminder email sent → booking #{b['id']} ({b.get('email')}) for {b.get('date')}")
        except Exception as e:
            log.error(f"[scheduler] Reminder email failed for #{b['id']}: {e}")


def _process_24h_reminder_emails():
    """Send 24-hour pre-session reminders to confirmed bookings."""
    now = datetime.now()
    # Window: sessions happening between 22h and 26h from now (4h window to avoid duplicates)
    date_from = (now + timedelta(hours=22)).strftime("%Y-%m-%d")
    date_to   = (now + timedelta(hours=26)).strftime("%Y-%m-%d")
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM bookings
        WHERE status = 'confirmed' AND confirmed = 1
          AND reminder_24h_email_sent IS NULL
          AND date BETWEEN ? AND ?
          AND email IS NOT NULL AND email != ''
    """, (date_from, date_to)).fetchall()
    conn.close()
    for row in rows:
        b = dict(row)
        try:
            ok = _send_24h_reminder_email(b)
            conn2 = db_conn()
            conn2.execute(
                "UPDATE bookings SET reminder_24h_email_sent=? WHERE id=?",
                (now.isoformat(), b["id"])
            )
            conn2.commit()
            conn2.close()
            if ok:
                log.info(f"[scheduler] 24h reminder sent → booking #{b['id']} ({b.get('email')}) for {b.get('date')}")
        except Exception as e:
            log.error(f"[scheduler] 24h reminder failed for #{b['id']}: {e}")


def _process_review_emails():
    """Send review request emails 5 days after a confirmed session."""
    now = datetime.now()
    # Sessions that happened between 4 and 6 days ago
    date_from = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    date_to   = (now - timedelta(days=4)).strftime("%Y-%m-%d")
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM bookings
        WHERE status = 'confirmed' AND confirmed = 1
          AND review_email_sent IS NULL
          AND date BETWEEN ? AND ?
          AND email IS NOT NULL AND email != ''
    """, (date_from, date_to)).fetchall()
    conn.close()
    for row in rows:
        b = dict(row)
        try:
            ok = _send_review_email(b)
            conn2 = db_conn()
            conn2.execute(
                "UPDATE bookings SET review_email_sent=? WHERE id=?",
                (now.isoformat(), b["id"])
            )
            conn2.commit()
            conn2.close()
            if ok:
                log.info(f"[scheduler] Review email sent → booking #{b['id']} ({b.get('email')})")
        except Exception as e:
            log.error(f"[scheduler] Review email failed for #{b['id']}: {e}")


# Start the scheduler in a background daemon thread
import threading as _bg_thread
_sched = _bg_thread.Thread(target=_run_email_scheduler, daemon=True, name="email-scheduler")
_sched.start()
log.info("[scheduler] Email scheduler started (abandoned / 48h reminder / 24h reminder / review)")


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── CLIENT SYNC ──────────────────────────────────────────────────────────────
def sync_client(email: str, name: str, phone: str = "", instagram: str = ""):
    """Upsert a client record and refresh aggregated stats from bookings table.
    Called automatically whenever a booking is created or confirmed."""
    if not email:
        return
    conn = db_conn()
    c = conn.cursor()
    now = datetime.now().isoformat()

    # Insert or update basic info (keep latest name/phone/ig)
    c.execute("""
        INSERT INTO clients (email, name, phone, instagram, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            name      = excluded.name,
            phone     = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE clients.phone END,
            instagram = CASE WHEN excluded.instagram != '' THEN excluded.instagram ELSE clients.instagram END,
            last_seen = excluded.last_seen
    """, (email.lower().strip(), name, phone, instagram, now, now))

    # Refresh aggregate stats
    c.execute("""
        UPDATE clients SET
            total_bookings  = (SELECT COUNT(*) FROM bookings WHERE LOWER(email)=LOWER(?) AND status NOT IN ('expired')),
            total_confirmed = (SELECT COUNT(*) FROM bookings WHERE LOWER(email)=LOWER(?) AND confirmed=1),
            total_paid      = (SELECT COALESCE(SUM(paid_amount),0) FROM bookings WHERE LOWER(email)=LOWER(?) AND confirmed=1)
        WHERE LOWER(email) = LOWER(?)
    """, (email, email, email, email))

    conn.commit()
    conn.close()


def rebuild_clients_from_bookings():
    """One-time rebuild: populate clients table from existing bookings."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT DISTINCT email, name, phone, instagram FROM bookings WHERE email != '' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    for r in rows:
        sync_client(r["email"], r["name"], r["phone"] or "", r["instagram"] or "")
    log.info(f"[db] Rebuilt clients table: {len(rows)} unique emails processed")


# Run once on startup to populate clients from existing bookings
try:
    _c = db_conn()
    _cnt = _c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    _c.close()
    if _cnt == 0:
        rebuild_clients_from_bookings()
except Exception as _e:
    log.warning(f"[db] Client rebuild skipped: {_e}")


# ── BACKUP ───────────────────────────────────────────────────────────────────
def create_backup(label: str = "auto") -> str:
    """Copy the SQLite file to BACKUP_DIR with a timestamp.
    Returns the backup file path."""
    import shutil
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = os.path.join(BACKUP_DIR, f"bookings_{ts}_{label}.db")
    shutil.copy2(DB_PATH, dest)
    # Keep only the 30 most recent backups (prevent disk bloat)
    all_bk = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")],
        reverse=True
    )
    for old in all_bk[30:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    log.info(f"[backup] Created: {dest}")
    return dest


# Daily auto-backup on startup
try:
    _today_prefix = datetime.now().strftime("%Y-%m-%d")
    _has_today = any(
        f.startswith(f"bookings_{_today_prefix}") and "startup" in f
        for f in os.listdir(BACKUP_DIR)
    )
    if not _has_today:
        create_backup("startup")
except Exception as _be:
    log.warning(f"[backup] Startup backup failed: {_be}")

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

import re as _re

def _validate_booking_fields(name, email, phone, instagram=""):
    """Validate client booking fields. Returns (is_valid, error_message)."""
    # Name: letters (incl. accented), spaces, hyphens, apostrophes — min 2 chars
    if not name or len(name.strip()) < 2:
        return False, "Please enter your full name (at least 2 characters)"
    if not _re.match(r"^[A-Za-zÀ-ÖØ-öø-ÿ'\- ]{2,80}$", name.strip()):
        return False, "Name should contain only letters, spaces, or hyphens"

    # Email: standard format check
    if not email or not _re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", email.strip()):
        return False, "Please enter a valid email address (e.g. jane@example.com)"
    if len(email) > 254:
        return False, "Email address is too long"

    # Phone: Canadian number — accepts various formats, normalises to 10 digits
    if phone:
        digits = _re.sub(r"\D", "", phone)
        if digits.startswith("1") and len(digits) == 11:
            digits = digits[1:]  # strip leading country code 1
        if len(digits) != 10:
            return False, "Please enter a valid Canadian phone number (10 digits, e.g. 403-555-1234)"
        # First digit of area code must be 2-9, first digit of subscriber must be 2-9
        if digits[0] in "01" or digits[3] in "01":
            return False, "Please enter a valid Canadian phone number"

    # Instagram: optional — if provided must be @handle or handle (1-30 alphanumeric/._)
    if instagram:
        handle = instagram.lstrip("@")
        if not _re.match(r"^[A-Za-z0-9_.]{1,30}$", handle):
            return False, "Instagram handle should be 1–30 characters (letters, numbers, . or _)"

    return True, ""


@app.route("/reserve", methods=["POST"])
def reserve_slot():
    data = request.json or {}
    slot_time = data.get("time")
    event_id = data.get("event_id") or data.get("date")  # accept either
    client_name = (data.get("name") or "").strip()
    client_email = (data.get("email") or "").strip().lower()
    client_phone = (data.get("phone") or "").strip()
    client_ig = (data.get("instagram") or "").strip()
    session_type = data.get("session_type", "")

    if not slot_time:
        return jsonify({"success": False, "error": "No time slot specified"}), 400

    # ── Field validation ──
    valid, err = _validate_booking_fields(client_name, client_email, client_phone, client_ig)
    if not valid:
        return jsonify({"success": False, "error": err}), 400

    # Normalise phone: strip non-digits, remove leading 1 if 11 digits
    if client_phone:
        _digits = _re.sub(r"\D", "", client_phone)
        if _digits.startswith("1") and len(_digits) == 11:
            _digits = _digits[1:]
        # Format as (XXX) XXX-XXXX
        client_phone = f"({_digits[:3]}) {_digits[3:6]}-{_digits[6:]}"

    # Normalise Instagram — always store without @
    if client_ig and client_ig.startswith("@"):
        client_ig = client_ig[1:]

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

        token = secrets.token_urlsafe(16)

        c.execute("""
            INSERT INTO bookings
                (date, time, name, email, phone, instagram, session_type, status, reserved_until, event_id, confirmation_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
        """, (event_date, slot_time, client_name, client_email, client_phone, client_ig, session_type, expires.isoformat(), ev["id"], token))

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

    # Sync client record (upsert) — creates or updates client profile
    try:
        sync_client(client_email, client_name, client_phone, client_ig)
    except Exception as _e:
        log.warning(f"[reserve] sync_client failed: {_e}")

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
        "confirmation_token": token,
        "expires_at": expires.isoformat(),
        "message": f"Reserved for {RESERVATION_MINUTES} minutes. Complete payment before {expires.strftime('%H:%M')}."
    })

@app.route("/payment")
def payment():
    """Standalone payment page — identity-safe via booking_id+token."""
    booking_id = request.args.get("booking_id")
    token      = request.args.get("token")
    if not booking_id or not token:
        return redirect(url_for("index"))

    conn = db_conn()
    conn.row_factory = sqlite3.Row
    c  = conn.cursor()
    c.execute("SELECT * FROM bookings WHERE id=? AND confirmation_token=?",
              (booking_id, token))
    row = c.fetchone()
    conn.close()

    if not row:
        return redirect(url_for("index"))

    booking = dict(row)
    ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    if not ev:
        ev = {}

    return render_template("payment.html",
        booking=booking,
        date=ev.get("date", DATE),
        time=booking.get("time", ""),
        name=booking.get("name", ""),
        price=ev.get("deposit", SESSION_PRICE) if ev.get("deposit") is not None else SESSION_PRICE,
        session_length=ev.get("session_length", SESSION_LENGTH),
        email=EMAIL,
        stripe_payment_link=ev.get("stripe_payment_link", "")
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
    booking_id = data.get("booking_id")
    if not booking_id:
        return jsonify({"success": False, "error": "booking_id required"}), 400
    
    conn = db_conn()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Lookup by booking_id — identity-safe, no ON CONFLICT by date/time
    c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
    booking = c.fetchone()
    
    if not booking:
        conn.close()
        return jsonify({"success": False, "error": "Booking not found"}), 404
    
    row = dict(booking)
    
    # Can only move reserved bookings to pending_payment
    if row["status"] not in ("reserved", "pending_payment"):
        conn.close()
        return jsonify({"success": False, "error": "Booking already confirmed/cancelled/expired"}), 400
    
    event_id = row.get("event_id")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    event_date = row["date"]
    time = row["time"]
    client_name = row["name"]
    client_email = row["email"]
    client_phone = row["phone"]
    client_ig = row["instagram"]
    session_type = row["session_type"]
    
    # Extend reservation window
    new_expires = (datetime.now() + timedelta(minutes=RESERVATION_MINUTES)).isoformat()
    c.execute("""
        UPDATE bookings
        SET status='pending_payment', reserved_until=?, confirmed=0, paid=0
        WHERE id=?
    """, (new_expires, booking_id))
    conn.commit()
    conn.close()
    
    # Sync to Notion
    sync_to_notion(booking_id)
    
    # Notify with inline confirm/cancel buttons
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
    
    # Payment submitted — global watcher auto-detects e-Transfer
    log.info(f"[confirm] Booking #{booking_id} — {client_name} @ {time} — payment submitted, global watcher active")
    
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
        time=booking.get("time", "15:00") if booking else "15:00",
        price=ev.get("deposit", SESSION_PRICE) if ev else SESSION_PRICE,
        event_title=ev.get("title", "Photo Session") if ev else "Photo Session",
        session_length=ev.get("session_length", 20) if ev else 20,
        location=ev.get("location", "Calgary, AB") if ev else "Calgary, AB",
        booking=booking,
        confirmation_token=booking.get("confirmation_token") if booking else ""
    )

@app.route("/calendar-ics/<booking_id>")
def calendar_ics(booking_id):
    """Generate .ics calendar file for a confirmed booking."""
    token = request.args.get("token", "")
    conn = db_conn()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bookings WHERE id=? AND confirmation_token=?", (booking_id, token))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "not found"}), 404

    booking = dict(row)
    if not booking.get("confirmed"):
        return jsonify({"error": "booking not confirmed"}), 403

    ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    event_date = ev["date"] if ev else booking.get("date", "")
    event_time = booking.get("time", "15:00")
    session_length = ev.get("session_length", 20) if ev else 20

    # Parse datetime
    from datetime import datetime, timedelta
    dt_start = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M")
    dt_end = dt_start + timedelta(minutes=session_length)

    dt_start_utc = dt_start.strftime("%Y%m%dT%H%M%SZ")
    dt_end_utc = dt_end.strftime("%Y%m%dT%H%M%SZ")
    dt_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    summary = ev.get("title", "Photo Session") if ev else "Photo Session"
    location = ev.get("location", "Calgary, AB")

    ics_body = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Pashynska Photography//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:{booking_id}@pashynska.agency
DTSTAMP:{dt_stamp}
DTSTART:{dt_start_utc}
DTEND:{dt_end_utc}
SUMMARY:{summary}
LOCATION:{location}
DESCRIPTION:Booking #{booking_id} with Pashynska Photography\\nClient: {booking.get('name', '')}
END:VEVENT
END:VCALENDAR"""

    from flask import Response
    return Response(ics_body, mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename=booking-{booking_id}.ics"})

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

    # Sync client stats after confirmation
    if booking:
        try:
            sync_client(booking.get("email", ""), booking.get("name", ""),
                        booking.get("phone", ""), booking.get("instagram", ""))
        except Exception as _e:
            log.warning(f"[confirm] sync_client failed: {_e}")

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


EVENTS_YAML_PATH = _EVENTS_PATH  # same path used for reads
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


# ===== EVENT CRUD =====

def _reload_events_globals():
    """Reload all in-memory event globals after YAML changes."""
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


@app.route("/admin/events/create", methods=["POST"])
@admin_required
def admin_create_event():
    """Create a new event and append to events.yaml."""
    data = request.json or {}

    # Required fields
    title = str(data.get("title", "")).strip()
    date_str = str(data.get("date", "")).strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    if not date_str:
        return jsonify({"error": "date is required"}), 400
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    # Build slug-style id from date + sanitised title
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30]
    event_id = f"{slug}-{date_str}"

    with open(EVENTS_YAML_PATH) as fh:
        yaml_data = yaml.safe_load(fh) or {}

    events_list = yaml_data.get("events", [])
    # Ensure unique id
    existing_ids = {e["id"] for e in events_list}
    base_id = event_id
    suffix = 2
    while event_id in existing_ids:
        event_id = f"{base_id}-{suffix}"
        suffix += 1

    session_len = int(data.get("session_length", 20))
    break_len = int(data.get("break_length", 10))

    new_event = {
        "id": event_id,
        "title": title,
        "subtitle": str(data.get("subtitle", "")).strip(),
        "date": date_str,
        "start_time": data.get("start_time", "10:00"),
        "end_time": data.get("end_time", "17:00"),
        "session_length": session_len,
        "break_length": break_len,
        "slot_interval": session_len + break_len,
        "deposit": float(data.get("deposit", 100)),
        "full_price": float(data.get("full_price", 300)),
        "location": str(data.get("location", "")).strip(),
        "session_type": "mini",
        "featured": bool(data.get("featured", False)),
        "status": data.get("status", "upcoming"),
        "included": [i.strip() for i in data.get("included", []) if str(i).strip()],
        "photos": [],
    }

    events_list.append(new_event)
    yaml_data["events"] = events_list

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

    _reload_events_globals()
    log.info(f"[admin] Event created: {event_id}")
    return jsonify({"success": True, "event_id": event_id})


@app.route("/admin/events/<event_id>/delete", methods=["POST"])
@admin_required
def admin_delete_event(event_id):
    """Delete an event from events.yaml. Refuses if there are active bookings."""
    with open(EVENTS_YAML_PATH) as fh:
        yaml_data = yaml.safe_load(fh) or {}

    events_list = yaml_data.get("events", [])
    event = next((e for e in events_list if e["id"] == event_id), None)
    if not event:
        return jsonify({"error": "Event not found"}), 404

    # Check for active/pending bookings
    conn = db_conn()
    active_count = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE event_id=? AND status NOT IN ('cancelled','expired')",
        (event_id,)
    ).fetchone()[0]
    conn.close()

    if active_count > 0:
        force = (request.json or {}).get("force", False)
        if not force:
            return jsonify({
                "error": f"Event has {active_count} active booking(s). Pass force=true to delete anyway.",
                "active_bookings": active_count
            }), 409

    yaml_data["events"] = [e for e in events_list if e["id"] != event_id]

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

    _reload_events_globals()
    log.info(f"[admin] Event deleted: {event_id}")
    return jsonify({"success": True})


@app.route("/admin/events/<event_id>/duplicate", methods=["POST"])
@admin_required
def admin_duplicate_event(event_id):
    """Duplicate an event with a new id, cleared photos and upcoming status."""
    with open(EVENTS_YAML_PATH) as fh:
        yaml_data = yaml.safe_load(fh) or {}

    events_list = yaml_data.get("events", [])
    source = next((e for e in events_list if e["id"] == event_id), None)
    if not source:
        return jsonify({"error": "Event not found"}), 404

    import copy, re
    new_event = copy.deepcopy(source)
    new_event["status"] = "upcoming"
    new_event["photos"] = []
    new_event["featured"] = False

    # Generate new id
    base_slug = re.sub(r"-\d{4}-\d{2}-\d{2}.*$", "", source["id"])
    existing_ids = {e["id"] for e in events_list}
    suffix = 2
    new_id = f"{base_slug}-copy"
    while new_id in existing_ids:
        new_id = f"{base_slug}-copy-{suffix}"
        suffix += 1
    new_event["id"] = new_id
    new_event["title"] = source["title"] + " (copy)"

    events_list.append(new_event)
    yaml_data["events"] = events_list

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

    _reload_events_globals()
    log.info(f"[admin] Event duplicated: {event_id} → {new_id}")
    return jsonify({"success": True, "new_event_id": new_id})


@app.route("/admin/events/<event_id>/update-meta", methods=["POST"])
@admin_required
def admin_update_event_meta(event_id):
    """Update event metadata: title, subtitle, date, featured, included items."""
    data = request.json or {}

    with open(EVENTS_YAML_PATH) as fh:
        yaml_data = yaml.safe_load(fh) or {}

    events_list = yaml_data.get("events", [])
    event = next((e for e in events_list if e["id"] == event_id), None)
    if not event:
        return jsonify({"error": "Event not found"}), 404

    if "title" in data:
        event["title"] = str(data["title"])[:120]
    if "subtitle" in data:
        event["subtitle"] = str(data["subtitle"])[:200]
    if "date" in data:
        try:
            datetime.strptime(str(data["date"]), "%Y-%m-%d")
            event["date"] = str(data["date"])
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    if "featured" in data:
        event["featured"] = bool(data["featured"])
    if "included" in data:
        event["included"] = [str(i).strip() for i in data["included"] if str(i).strip()]

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

    _reload_events_globals()
    log.info(f"[admin] Event meta updated: {event_id}")
    return jsonify({"success": True})


# ─────────────────────────────────────────────
#  CLIENT DATABASE ROUTES
# ─────────────────────────────────────────────

@app.route("/admin/clients")
@admin_required
def admin_clients():
    """Client database page."""
    return render_template("admin_clients.html")


@app.route("/admin/api/clients")
@admin_required
def api_clients_list():
    """JSON list of all clients with optional search/filter."""
    q = (request.args.get("q") or "").strip().lower()
    tag = (request.args.get("tag") or "").strip()
    sort = request.args.get("sort", "last_booking_at")
    allowed_sorts = {"last_booking_at", "total_bookings", "total_paid", "name", "created_at"}
    if sort not in allowed_sorts:
        sort = "last_booking_at"

    conn = db_conn()
    sql = """
        SELECT id, name, email, phone, instagram, tags,
               total_bookings, total_confirmed, total_paid,
               first_booking_at, last_booking_at, created_at, notes
        FROM clients
        WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (LOWER(name) LIKE ? OR LOWER(email) LIKE ? OR LOWER(phone) LIKE ? OR LOWER(instagram) LIKE ?)"
        params += [f"%{q}%"] * 4
    if tag:
        sql += " AND (',' || tags || ',') LIKE ?"
        params.append(f"%,{tag},%")
    sql += f" ORDER BY {sort} DESC NULLS LAST"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/api/clients/<int:client_id>")
@admin_required
def api_client_detail(client_id):
    """Single client profile + full booking history."""
    conn = db_conn()
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        conn.close()
        return jsonify({"error": "not found"}), 404
    client = dict(client)
    bookings = conn.execute(
        "SELECT * FROM bookings WHERE LOWER(email)=LOWER(?) ORDER BY date DESC, time DESC",
        (client["email"],)
    ).fetchall()
    notes = conn.execute(
        "SELECT * FROM client_notes WHERE client_id=? ORDER BY created_at DESC",
        (client_id,)
    ).fetchall()
    conn.close()
    return jsonify({
        "client": client,
        "bookings": [dict(b) for b in bookings],
        "notes": [dict(n) for n in notes],
    })


@app.route("/admin/api/clients/<int:client_id>/note", methods=["POST"])
@admin_required
def api_client_add_note(client_id):
    """Add a note to a client."""
    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    conn = db_conn()
    # Verify client exists
    if not conn.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone():
        conn.close()
        return jsonify({"error": "not found"}), 404
    conn.execute(
        "INSERT INTO client_notes (client_id, text) VALUES (?, ?)",
        (client_id, text)
    )
    conn.commit()
    note = conn.execute(
        "SELECT * FROM client_notes WHERE client_id=? ORDER BY created_at DESC LIMIT 1",
        (client_id,)
    ).fetchone()
    conn.close()
    return jsonify({"success": True, "note": dict(note)})


@app.route("/admin/api/clients/<int:client_id>/note/<int:note_id>", methods=["DELETE"])
@admin_required
def api_client_delete_note(client_id, note_id):
    """Delete a note."""
    conn = db_conn()
    conn.execute("DELETE FROM client_notes WHERE id=? AND client_id=?", (note_id, client_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/admin/api/clients/<int:client_id>/tag", methods=["POST"])
@admin_required
def api_client_tag(client_id):
    """Add or remove a tag on a client. Body: {tag, action:'add'|'remove'}"""
    data = request.json or {}
    tag = (data.get("tag") or "").strip().upper()
    action = data.get("action", "add")
    if not tag:
        return jsonify({"error": "tag required"}), 400
    conn = db_conn()
    row = conn.execute("SELECT tags FROM clients WHERE id=?", (client_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    existing = [t.strip().upper() for t in (row["tags"] or "").split(",") if t.strip()]
    if action == "add":
        if tag not in existing:
            existing.append(tag)
    else:
        existing = [t for t in existing if t != tag]
    new_tags = ",".join(existing)
    conn.execute("UPDATE clients SET tags=? WHERE id=?", (new_tags, client_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "tags": new_tags})


@app.route("/admin/api/clients/<int:client_id>/edit", methods=["POST"])
@admin_required
def api_client_edit(client_id):
    """Update client name / phone / instagram (manual override)."""
    data = request.json or {}
    conn = db_conn()
    if not conn.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone():
        conn.close()
        return jsonify({"error": "not found"}), 404
    fields = {}
    if "name" in data:
        fields["name"] = str(data["name"])[:120]
    if "phone" in data:
        fields["phone"] = str(data["phone"])[:30]
    if "instagram" in data:
        ig = str(data["instagram"]).lstrip("@")[:80]
        fields["instagram"] = ig
    if not fields:
        conn.close()
        return jsonify({"error": "nothing to update"}), 400
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE clients SET {set_clause} WHERE id=?",
                 list(fields.values()) + [client_id])
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/admin/backup", methods=["POST"])
@admin_required
def admin_manual_backup():
    """Trigger a manual database backup."""
    try:
        path = create_backup()
        return jsonify({"success": True, "path": str(path)})
    except Exception as e:
        log.error(f"[backup] Manual backup failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/admin/backups")
@admin_required
def admin_list_backups():
    """List available database backups."""
    import glob as _glob
    pattern = os.path.join(BACKUP_DIR, "bookings_*.db")
    files = sorted(_glob.glob(pattern), reverse=True)
    result = []
    for f in files[:50]:
        try:
            stat = os.stat(f)
            result.append({
                "filename": os.path.basename(f),
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        except OSError:
            pass
    return jsonify(result)


@app.route("/admin/api/clients/export")
@admin_required
def api_clients_export():
    """Export all clients as CSV."""
    import csv, io
    conn = db_conn()
    rows = conn.execute(
        "SELECT name, email, phone, instagram, tags, total_bookings, total_confirmed, total_paid, "
        "first_booking_at, last_booking_at, created_at FROM clients ORDER BY last_booking_at DESC NULLS LAST"
    ).fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Email", "Phone", "Instagram", "Tags",
                     "Total Bookings", "Confirmed", "Total Paid ($)",
                     "First Booking", "Last Booking", "Client Since"])
    for r in rows:
        writer.writerow(list(r))
    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=clients.csv"}
    )


# ─────────────────────────────────────────────

@app.route("/booking-status")
def booking_status():
    """API for live client page polling — requires confirmation_token for identity safety."""
    booking_id = request.args.get("booking_id")
    token = request.args.get("token")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400
    conn = db_conn()
    row = conn.execute("SELECT status, confirmed, paid, paid_amount, name, confirmation_token FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    b = dict(row)
    # Token check: must match confirmation_token
    stored_token = b.get("confirmation_token", "")
    if not token or not stored_token or token != stored_token:
        log.warning(f"[booking-status] Invalid token for booking #{booking_id}")
        return jsonify({"error": "unauthorized"}), 403
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
