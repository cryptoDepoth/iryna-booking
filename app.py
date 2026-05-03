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
import sqlite3
import requests
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
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())

# ===== RATE LIMITING =====
# Simple IP-based rate limit: 10 requests per 10 minutes per IP
_rate_limits = {}

def check_rate_limit(ip):
    now = time.time()
    if ip not in _rate_limits:
        _rate_limits[ip] = []
    # Clean old entries
    _rate_limits[ip] = [t for t in _rate_limits[ip] if now - t < 600]
    return len(_rate_limits[ip]) < 10  # 10 requests per 10 min

def record_request(ip):
    if ip not in _rate_limits:
        _rate_limits[ip] = []
    _rate_limits[ip].append(time.time())

# ===== ADMIN AUTH =====
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

def admin_required(f):
    """Require X-Admin-Key header for admin endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if ADMIN_KEY and request.headers.get("X-Admin-Key") != ADMIN_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ===== NOTIFICATIONS =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def _notify_admin(message):
    """Send notification to photographer via Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[notify] Telegram not configured. Message: {message[:80]}...")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        log.info("[notify] Telegram message sent")
    except Exception as e:
        log.error(f"[notify] Telegram failed: {e}")


def _send_client_email(to_email, client_name, event_date, slot_time, event_title, booking_id):
    """Send confirmation email to client via Himalaya CLI."""
    if not to_email:
        return
    try:
        import subprocess
        date_nice = datetime.strptime(event_date, "%Y-%m-%d").strftime("%B %d, %Y")
        subject = f"Booking Confirmed — {event_title} on {date_nice}"
        body = f"""Hi {client_name},

Your mini photo session is confirmed! Here are the details:

Event: {event_title}
Date: {date_nice}
Time: {slot_time}
Booking ID: #{booking_id}

What's included:
• 20-minute photo session
• 15 professionally edited photos
• All original photos
• Quick turnaround (within 48 hours)

Location details will be sent closer to the session date.

If you need to reschedule, please DM me on Instagram @pashynska.photo.

Looking forward to our session!

Warmly,
Iryna Pashynska
@pashynska.photo
"""
        template = f"""From: Iryna Pashynska <iryna.pashynska@gmail.com>
To: {client_name} <{to_email}>
Subject: {subject}
Content-Type: text/plain; charset=utf-8

{body}"""
        result = subprocess.run(
            ["himalaya", "template", "send", "-a", "iryna"],
            input=template, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info(f"[email] Confirmation sent to {to_email}")
        else:
            log.error(f"[email] Himalaya failed: {result.stderr[:200]}")
    except Exception as e:
        log.error(f"[email] Send failed: {e}")

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
            f"Booking #{b['id']}"
        )
        import subprocess as _sp
        result = _sp.run(
            [helper, "create",
             "--calendar", CALENDAR_ID,
             "--summary", summary,
             "--start", start_dt.isoformat(),
             "--end", end_dt.isoformat(),
             "--tz", CALENDAR_TZ,
             "--description", description],
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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

@app.route("/")
def index():
    """Landing page — shows the active event or event list."""
    event_id = request.args.get("event")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev:
        return "No events available", 404
    slots = generate_slots(ev)
    return render_template("index.html",
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
        slots=slots
    )

@app.route("/events")
def list_events():
    """API: list all events with status."""
    result = []
    for ev in EVENTS:
        if ev.get("status") in ("active", "upcoming"):
            result.append({
                "id": ev["id"],
                "title": ev.get("title", ""),
                "subtitle": ev.get("subtitle", ""),
                "date": ev["date"],
                "start_time": ev.get("start_time", ""),
                "end_time": ev.get("end_time", ""),
                "deposit": ev.get("deposit", SESSION_PRICE),
                "full_price": ev.get("full_price", SESSION_TOTAL),
                "status": ev.get("status", ""),
            })
    return jsonify({"events": result})

@app.route("/slots/<date_str>")
def get_slots(date_str):
    ev = get_event_by_date(date_str)
    if not ev:
        return jsonify({"slots": [], "message": f"No event on {date_str}"})
    
    slots = generate_slots(ev)
    now = datetime.now()
    conn = db_conn()
    c = conn.cursor()
    
    available_slots = []
    for slot in slots:
        c.execute("SELECT * FROM bookings WHERE date=? AND time=? AND (confirmed=1 OR reserved_until > ?)",
                  (ev["date"], slot["time"], now.isoformat()))
        existing = c.fetchone()
        
        if not existing:
            available_slots.append({
                "time": slot["time"],
                "label": slot["label"]
            })
    
    conn.close()
    
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
    expires = now + timedelta(minutes=15)

    conn = db_conn()
    c = conn.cursor()

    # BEGIN IMMEDIATE acquires a write lock up-front, serialising concurrent
    # reserve attempts so the check-then-insert is atomic.
    c.execute("BEGIN IMMEDIATE")

    # Slot is taken if there is a confirmed booking OR an active reservation
    c.execute("""
        SELECT id FROM bookings
        WHERE date=? AND time=?
          AND (confirmed=1 OR reserved_until > ?)
    """, (event_date, slot_time, now.isoformat()))
    if c.fetchone():
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "error": "Slot is no longer available"})

    c.execute("""
        INSERT OR IGNORE INTO bookings
            (date, time, name, email, phone, instagram, session_type, status, reserved_until, event_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
    """, (event_date, slot_time, client_name, client_email, client_phone, client_ig, session_type, expires.isoformat(), ev["id"]))

    if c.rowcount == 0:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "error": "Slot just taken"})

    booking_id = c.lastrowid
    conn.commit()
    conn.close()

    # Notify photographer via Telegram
    _notify_admin(f"🆕 New reservation #{booking_id}\n"
                  f"👤 {client_name or '(no name)'}\n"
                  f"📅 {event_date} @ {slot_time}\n"
                  f"🎉 {ev.get('title', '')}\n"
                  f"📧 {client_email}\n"
                  f"📸 @{client_ig or 'N/A'}\n"
                  f"🏷️ {session_type or 'N/A'}\n"
                  f"⏱️ Expires in 15 min")

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "event_id": ev["id"],
        "expires_at": expires.isoformat(),
        "message": f"Reserved for 15 minutes. Complete payment before {expires.strftime('%H:%M')}."
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
          (datetime.now() + timedelta(minutes=15)).isoformat(), ev["id"] if ev else None))
    conn.commit()
    
    # Get booking ID
    c.execute("SELECT id FROM bookings WHERE date=? AND time=?", (event_date, time))
    row = c.fetchone()
    booking_id = row["id"] if row else None
    conn.close()
    
    # Sync to Notion
    if booking_id:
        sync_to_notion(booking_id)

    # Notify photographer
    _notify_admin(f"💰 Payment submitted for #{booking_id}\n"
                  f"👤 {client_name}\n"
                  f"📅 {event_date} @ {time}\n"
                  f"🎉 {ev.get('title', '') if ev else ''}\n"
                  f"📧 {client_email}\n"
                  f"📸 @{client_ig or 'N/A'}\n"
                  f"🏷️ {session_type or 'N/A'}\n"
                  f"⏳ Awaiting e-Transfer confirmation")

    log.info(f"[confirm] Booking #{booking_id} — {client_name} @ {time} — payment submitted")
    
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

@app.route("/admin")
@admin_required
def admin():
    """Simple admin view of all bookings"""
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM bookings ORDER BY created_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    
    return jsonify({
        "bookings": rows,
        "total": len(rows),
        "confirmed": sum(1 for b in rows if b["confirmed"]),
        "pending": sum(1 for b in rows if not b["confirmed"] and b["status"] in ("pending", "pending_payment")),
        "reserved": sum(1 for b in rows if b["status"] == "reserved"),
        "expired": sum(1 for b in rows if b["status"] == "expired"),
        "total_expected": sum(SESSION_PRICE for b in rows if b["date"] == DATE and b["status"] not in ("expired", "cancelled")),
        "total_paid": sum(b.get("paid_amount", 0) or 0 for b in rows)
    })

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
            booking_id=booking_id
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

# ===== RUN =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
