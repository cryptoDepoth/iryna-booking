#!/usr/bin/env python3
"""
Iryna Pashynska Photography — Mini Session Booking System
Flask app with SQLite database, 20-min sessions + 10-min breaks, Instagram field.
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory
from datetime import datetime, timedelta
import json
import os
import sqlite3
import requests
import time

app = Flask(__name__)
app.secret_key = os.urandom(24)

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

# Serve static images
@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(os.path.join(app.root_path, 'static', 'images'), filename)

# ===== NOTION CONFIG =====
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "355510b9-cc5b-818c-aec6-d764f116e2b2")
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",  # stable version
    "Content-Type": "application/json"
}

def sync_to_notion(booking_id):
    """Sync a single booking to Notion"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return
        booking = dict(row)
        
        status_map = {"reserved": "Reserved", "pending": "Pending Payment", "pending_payment": "Awaiting Payment", "confirmed": "Confirmed", "expired": "Expired"}
        status = "Confirmed" if booking["confirmed"] else status_map.get(booking["status"], "Pending Payment")
        
        properties = {
            "Name": {"title": [{"text": {"content": booking["name"] or "(no name)"}}]},
            "Date": {"date": {"start": booking["date"]}},
            "Time": {"rich_text": [{"text": {"content": booking["time"]}}]},
            "Status": {"select": {"name": status}},
            "Instagram": {"rich_text": [{"text": {"content": booking.get("instagram","") or ""}}]},
            "Email": {"email": booking["email"] or None},
            "Phone": {"phone_number": booking["phone"] or None},
            "Session Type": {"rich_text": [{"text": {"content": booking.get("session_type","") or ""}}]},
            "Amount": {"number": booking.get("paid_amount") or SESSION_PRICE},
            "Paid": {"checkbox": bool(booking["paid"])},
            "Booking ID": {"number": booking["id"]},
        }
        
        # Check if already exists
        query_url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
        q = requests.post(query_url, headers=NOTION_HEADERS, json={
            "filter": {"property": "Booking ID", "number": {"equals": booking["id"]}}
        }).json()
        
        if q.get("results"):
            page_id = q["results"][0]["id"]
            requests.patch(f"https://api.notion.com/v1/pages/{page_id}",
                          headers=NOTION_HEADERS, json={"properties": properties})
        else:
            requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json={
                "parent": {"database_id": NOTION_DATABASE_ID},
                "properties": properties
            })
    except Exception as e:
        print(f"Notion sync error: {e}")

# ===== CONFIG =====
EVENT_TITLE = "🪻 Blossom Mini Sessions"
SESSION_LENGTH = 20      # minutes of actual shooting
BREAK_LENGTH = 10        # minutes break between sessions
SLOT_INTERVAL = 30       # total interval (20 + 10)
SESSION_PRICE = 95       # CAD — half deposit ($190/2)
SESSION_TOTAL = 190      # CAD — full price before GST
EMAIL = "iryna.pashynska@gmail.com"
DATE = "2026-05-03"      # TODAY — May 3 Blossom Mini Session
START_TIME = "10:00"
END_TIME = "16:00"

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
    # Migrate: add paid_amount if missing (for existing databases)
    try:
        c.execute("ALTER TABLE bookings ADD COLUMN paid_amount REAL DEFAULT NULL")
        conn.commit()
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
    """Delete expired reserved/pending_payment rows so slots open back up. Returns count."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""
        DELETE FROM bookings
        WHERE confirmed = 0
          AND paid = 0
          AND reserved_until IS NOT NULL
          AND reserved_until <= ?
          AND status IN ('reserved', 'pending_payment')
    """, (now,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

# Generate time slots with breaks
def generate_slots():
    slots = []
    start = datetime.strptime(START_TIME, "%H:%M")
    end = datetime.strptime(END_TIME, "%H:%M")
    current = start
    while current < end:
        slot_str = current.strftime("%H:%M")
        session_end = current + timedelta(minutes=SESSION_LENGTH)
        slots.append({
            "time": slot_str,
            "label": f"{slot_str} – {session_end.strftime('%H:%M')}",
            "reserved_until": None
        })
        current += timedelta(minutes=SLOT_INTERVAL)
    return slots

SLOTS = generate_slots()

# ===== ROUTES =====

@app.route("/")
def index():
    return render_template("index.html",
        title=EVENT_TITLE,
        date=DATE,
        price=SESSION_PRICE,
        total=SESSION_TOTAL,
        email=EMAIL,
        session_length=SESSION_LENGTH,
        break_length=BREAK_LENGTH,
        slots=SLOTS
    )

@app.route("/slots/<date_str>")
def get_slots(date_str):
    if date_str != DATE:
        return jsonify({"slots": [], "message": "Only May 3 available"})
    
    now = datetime.now()
    conn = db_conn()
    c = conn.cursor()
    
    available_slots = []
    for slot in SLOTS:
        # Check if confirmed booking exists
        c.execute("SELECT * FROM bookings WHERE date=? AND time=? AND (confirmed=1 OR reserved_until > ?)", 
                  (DATE, slot["time"], now.isoformat()))
        existing = c.fetchone()
        
        if not existing:
            available_slots.append({
                "time": slot["time"],
                "label": slot["label"]
            })
    
    conn.close()
    
    return jsonify({
        "date": date_str,
        "slots": available_slots,
        "total": len(SLOTS),
        "available": len(available_slots)
    })

@app.route("/reserve", methods=["POST"])
def reserve_slot():
    data = request.json
    time = data.get("time")

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
    """, (DATE, time, now.isoformat()))
    if c.fetchone():
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "error": "Slot is no longer available"})

    # INSERT OR IGNORE so a duplicate constraint never silently overwrites another
    # reservation that slipped in between the check and here (belt-and-suspenders).
    c.execute("""
        INSERT OR IGNORE INTO bookings
            (date, time, name, email, phone, instagram, session_type, status, reserved_until)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
    """, (DATE, time, "", "", "", "", "", expires.isoformat()))

    if c.rowcount == 0:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "error": "Slot just taken"})

    booking_id = c.lastrowid
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "expires_at": expires.isoformat(),
        "message": f"Reserved for 15 minutes. Complete payment before {expires.strftime('%H:%M')}."
    })

@app.route("/payment")
def payment():
    time = request.args.get("time")
    if not time:
        return redirect(url_for("index"))
    return render_template("payment.html",
        time=time,
        name=request.args.get("name", ""),
        client_email=request.args.get("email", ""),
        phone=request.args.get("phone", ""),
        instagram=request.args.get("instagram", ""),
        session_type=request.args.get("session_type", ""),
        date=DATE,
        price=SESSION_PRICE,
        email=EMAIL,
        session_length=SESSION_LENGTH
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
    client_name = data.get("name", "")
    client_email = data.get("email", "")
    client_phone = data.get("phone", "")
    client_ig = data.get("instagram", "")
    session_type = data.get("session_type", "")
    
    conn = db_conn()
    c = conn.cursor()
    
    # Update or insert booking as pending
    c.execute('''
        INSERT INTO bookings (date, time, name, email, phone, instagram, session_type, status, reserved_until)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_payment', ?)
        ON CONFLICT(date, time) DO UPDATE SET
            name=excluded.name,
            email=excluded.email,
            phone=excluded.phone,
            instagram=excluded.instagram,
            session_type=excluded.session_type,
            status='pending_payment',
            reserved_until=excluded.reserved_until
    ''', (DATE, time, client_name, client_email, client_phone, client_ig, session_type,
          (datetime.now() + timedelta(minutes=15)).isoformat()))
    conn.commit()
    
    # Get booking ID
    c.execute("SELECT id FROM bookings WHERE date=? AND time=?", (DATE, time))
    row = c.fetchone()
    booking_id = row["id"] if row else None
    conn.close()
    
    # Sync to Notion
    if booking_id:
        sync_to_notion(booking_id)
    
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
    return render_template("success.html",
        email=EMAIL,
        date=DATE,
        price=SESSION_PRICE,
        booking=booking
    )

@app.route("/admin")
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
        "total_expected": sum(SESSION_PRICE for b in rows if b["date"] == DATE),
        "total_paid": sum(b.get("paid_amount", 0) or 0 for b in rows)
    })

@app.route("/admin/confirm", methods=["POST"])
def admin_confirm():
    data = request.json
    booking_id = data.get("booking_id")
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE bookings SET confirmed=1, paid=1, status='confirmed' WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    
    # Sync to Notion
    sync_to_notion(booking_id)
    
    return jsonify({"success": True})

# ===== RUN =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
