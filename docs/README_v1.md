# 📷 Pashynska Photography — Booking System v1

> Mini-session booking system with real-time e-Transfer payment verification,
> Telegram admin notifications, live client status updates, and automated email confirmations.

**Author:** Iryna Pashynska Photography (Calgary, AB)
**Stack:** Python 3.14 / Flask / SQLite / Himalaya CLI / Telegram Bot API
**License:** Private — All rights reserved

---

## Features

- **Client booking flow:** Select slot → fill details → send e-Transfer → real-time status page
- **Live status page:** Auto-updates from ⏳ Pending → ✅ Confirmed when payment verified (no reload)
- **Telegram admin alerts:** Inline buttons to confirm/cancel bookings directly from Telegram
- **e-Transfer verification:** Automatic email parsing via Himalaya CLI (IMAP)
- **Email confirmations:** Automatic confirmation email to client on payment verification
- **Google Calendar integration:** Creates calendar events for confirmed bookings
- **Notion sync:** Writes booking data to Notion database (optional)
- **Admin dashboard:** Full booking management at `/admin`
- **Anti-abuse:** Rate limiting, slot reservation expiry, SQL transaction safety

---

## Quick Start

### Prerequisites

- Python 3.12+
- [Himalaya CLI](https://github.com/pimalaya/himalaya) configured with Gmail IMAP/SMTP
- A Telegram bot token (via [@BotFather](https://t.me/BotFather))
- Cloudflared (`npm install -g cloudflared`) for public tunnel

### Install

```bash
git clone https://github.com/cryptoDepoth/iryna-booking.git
cd iryna-booking
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your secrets:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#   ADMIN_USER, ADMIN_PASSWORD
#   NOTION_API_KEY (optional)
```

```bash
cp event.json.example event.json
# Edit event.json with your session details:
#   title, date, start_time, end_time, deposit, etc.
```

### Run

```bash
# Start Flask server
python3 app.py

# In another terminal, start public tunnel:
cloudflared tunnel --url http://127.0.0.1:5001

# Set Telegram webhook to the tunnel URL:
python3 -c "
import os, requests
from dotenv import load_dotenv
load_dotenv()
t=os.environ['TELEGRAM_BOT_TOKEN']
url='https://YOUR-TUNNEL-URL.trycloudflare.com/telegram/webhook'
r=requests.post(f'https://api.telegram.org/bot{t}/setWebhook', json={'url':url})
print(r.json())
"
```

---

## Project Structure

```
iryna-booking/
├── app.py                  # Flask app — all routes, booking logic, Telegram, email
├── check_etransfer.py      # e-Transfer email parser (standalone checker)
├── timed_cron.py           # Active payment checker (launched per-booking)
├── sync_notion.py          # Notion database sync helper
├── gcal_helper.py          # Google Calendar API integration
├── event.json              # Active event/session configuration
├── event.json.example      # Template for event.json
├── requirements.txt        # Python dependencies
├── .env.example            # Template for environment secrets
├── setup_permanent.sh      # Production launchd + tunnel setup script
│
├── templates/
│   ├── index.html           # Landing page with slot selection
│   ├── payment.html         # Payment instructions + client details form
│   ├── success.html         # Live status page (auto-updates via polling)
│   └── admin.html           # Admin dashboard
│
├── static/images/           # Session/gallery photos
├── tests/
│   └── test_booking_flow.py # 5 regression tests (pytest)
│
└── docs/
    ├── RUNBOOK.md            # Operations manual
    ├── CHANGELOG.md          # Version history
    ├── ARCHITECTURE.md       # System design & data flow
    ├── API.md                # Full API reference
    └── agent-handoff/
        └── TECHNICAL_HANDOFF.md  # Detailed technical docs
```

---

## Booking Flow

```
Client                        System                          Admin (Telegram)
  │                              │                                  │
  ├── GET / ──────────────────►  │                                  │
  │  (landing page)              │                                  │
  │                              │                                  │
  ├── POST /reserve ─────────►  │                                  │
  │  (slot reserved 15 min)      │                                  │
  │                              │                                  │
  ├── GET /payment ──────────►   │                                  │
  │  (e-Transfer instructions)   │                                  │
  │                              │                                  │
  ├── POST /confirm ─────────►   │                                  │
  │  (name/email/phone)          ├── Telegram notification ──────► │
  │                              ├── Start e-Transfer checker       │
  │                              │                                  │
  ├── GET /success ──────────►   │                                  │
  │  (live status page)          │                                  │
  │                              │                                  │
  │  ┌─── polls /booking-status every 3s ───┐                     │
  │  │                                       │                     │
  │                              │  ◄──── Telegram ✅ Confirm ──────┤
  │                              ├── confirm_booking_paid()         │
  │                              ├── send_confirmation_email()      │
  │                              ├── create_calendar_event()        │
  │                              ├── sync_to_notion()               │
  │                              │                                  │
  │  ◄── page auto-updates: ✅ Confirmed! 🎉                      │
  │  ◄── email: "Booking Confirmed"                               │
```

---

## API Reference

### Public Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Landing page |
| `GET` | `/slots/<date>` | Available time slots |
| `POST` | `/reserve` | Reserve a slot (15 min hold) |
| `GET` | `/payment` | Payment instructions page |
| `POST` | `/confirm` | Submit client details + trigger verification |
| `GET` | `/success` | Live status page (polls `/booking-status`) |
| `GET` | `/booking-status` | JSON status for live polling |

### Admin Endpoints (HTTP Basic Auth)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin` | Admin dashboard |
| `POST` | `/admin/confirm` | Manually confirm a booking |
| `POST` | `/admin/cancel` | Cancel and release a booking |
| `GET/POST` | `/admin/event` | View/update event.json |

### Webhook

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/telegram/webhook` | Telegram inline button callbacks |

---

## Architecture

### Database (SQLite: `bookings.db`)

```sql
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, time TEXT,
    name TEXT, email TEXT, phone TEXT, instagram TEXT,
    session_type TEXT,
    status TEXT DEFAULT 'reserved',
    -- reserved → pending_payment → confirmed / cancelled / expired
    paid INTEGER DEFAULT 0,
    confirmed INTEGER DEFAULT 0,
    paid_amount REAL,
    created_at TEXT,
    reserved_until TEXT,
    notion_page_id TEXT,
    calendar_event_id TEXT,
    calendar_event_url TEXT,
    UNIQUE(date, time)
);
```

### Key Design Decisions

1. **Two-step reservation:** `/reserve` holds the slot → `/confirm` fills client data and starts payment check
2. **Active checker model:** Payment checker launched per-booking (not permanent cron), runs for 20 min
3. **Live polling:** Client page polls `/booking-status` every 3 seconds — no WebSocket complexity
4. **Telegram as admin panel:** Inline buttons for confirm/cancel — no need to open browser
5. **Himalaya CLI for email:** Sends confirmations via existing Gmail SMTP; reads e-Transfers via IMAP
6. **Slot release:** Cancelled/expired bookings free the slot for rebooking

---

## Testing

```bash
pytest tests/test_booking_flow.py -v
```

5 regression tests covering:
- Reserve → confirm → status update flow
- Duplicate reservation rejection
- Invalid slot rejection
- Cancelled slot can be re-reserved
- Confirmation email triggering

---

## Production Deployment

### Quick Tunnel (development/demo)
```bash
cloudflared tunnel --url http://127.0.0.1:5001
```

### Permanent Setup (macOS launchd)
```bash
bash setup_permanent.sh
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Admin Telegram chat ID |
| `ADMIN_USER` | Yes | Admin panel username |
| `ADMIN_PASSWORD` | Yes | Admin panel password |
| `BOOKING_BASE_URL` | Yes | Public URL for Telegram links |
| `NOTION_API_KEY` | No | Notion integration token |
| `GOOGLE_CREDENTIALS_FILE` | No | Google Calendar OAuth |
| `LOG_LEVEL` | No | Python log level (default: INFO) |

---

## External Dependencies

| Tool | Purpose |
|------|---------|
| [Himalaya CLI](https://github.com/pimalaya/himalaya) v1.2+ | Email send/receive (IMAP/SMTP) |
| [Cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) | Public tunnel to localhost |
| Telegram Bot API | Admin notifications + inline actions |

---

## Version

**v1.0.0** — 2026-05-03

Working production system with full booking flow, Telegram integration, live status, email confirmations, e-Transfer verification.
