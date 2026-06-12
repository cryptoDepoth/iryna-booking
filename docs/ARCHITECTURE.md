# Architecture — Pashynska Booking System v1

## System Overview

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   Client     │     │   Flask Server    │     │   External       │
│   Browser    │     │   (app.py)        │     │   Services       │
├─────────────┤     ├──────────────────┤     ├──────────────────┤
│             │     │                  │     │                  │
│  Landing    ├────►│  GET /           │     │  SQLite DB       │
│  Page       │     │  (index.html)    ├────►│  (bookings.db)   │
│             │     │                  │     │                  │
│  Slot       ├────►│  GET /slots/:date│     ├──────────────────┤
│  Selection  │     │  (JSON API)      │     │  Telegram Bot    │
│             │     │                  │     │  API             │
│  Reserve    ├────►│  POST /reserve   │     │                  │
│             │     │  → holds 15 min  ├────►│  Himalaya CLI    │
│  Payment    ├────►│  POST /confirm   │     │  (IMAP/SMTP)     │
│  Details    │     │  → starts checker│     │                  │
│             │     │  → sends TG msg  ├────►├──────────────────┤
│  Live       │◄───►│  GET /success    │     │  Google Calendar │
│  Status     │     │  polls /status   │     │  API             │
│  Page       │     │  every 3s        │     │                  │
│             │     │                  │     ├──────────────────┤
│             │     │                  │     │  Notion API      │
│             │     │                  │     │  (optional)      │
└─────────────┘     └──────────────────┘     └──────────────────┘
                            ▲
                            │
                    ┌───────┴────────┐
                    │  Admin          │
                    │  (Telegram/     │
                    │   Browser)      │
                    └────────────────┘
```

## Data Flow

### 1. Slot Reservation (`/reserve`)

- Validates slot against `event.json` config
- Acquires SQLite `BEGIN IMMEDIATE` lock
- Checks for conflicts (not cancelled/expired)
- Deletes old cancelled rows for same slot
- Inserts new row: `status='reserved'`, `reserved_until = now + 15min`
- Returns `booking_id`

### 2. Client Confirmation (`/confirm`)

- Receives `booking_id`, `name`, `email`, `phone`, `instagram`
- Validates booking exists and is in `reserved` status
- Checks reservation hasn't expired
- Updates row: fills client data, `status='pending_payment'`, extends hold 20min
- Sends Telegram admin notification with inline buttons
- Launches `timed_cron.py --booking-id <id>` as background process
- Syncs to Notion

### 3. Payment Verification (`check_etransfer_v2.py` + `timed_cron.py`)

- Primary path: the in-app watcher thread polls Gmail every ~60s via `check_etransfer_v2.py`
- `timed_cron.py` is an optional time-boxed re-check (up to 20 minutes) that delegates
  all parsing/matching to `check_etransfer_v2` (the legacy v1 `check_etransfer.py` was removed)
- Parses Interac e-Transfer notification emails via Himalaya CLI
- Extracts: amount, sender name, reference number
- Amount-only matching against the booking's stored `deposit_amount` (or events.yaml);
  exact/overpaid → auto-confirm, underpaid → recorded only, collisions → admin alert
- Uses file-based lock (`.timed_cron.lock`) to prevent duplicates

### 4. Booking Confirmation (`confirm_booking_paid()`)

- Updates SQLite: `confirmed=1, paid=1, status='confirmed', paid_amount=?`
- Calls `send_confirmation_email()` via Himalaya template send
- Calls `create_calendar_event_for_booking()` (Google Calendar)
- Calls `sync_to_notion()` (updates Notion page)
- Logs result

### 5. Live Status Polling (`/booking-status`)

- Lightweight JSON endpoint: `{id, status, confirmed, paid}`
- Client's success.html polls every 3 seconds
- On `confirmed=true`: JS triggers confetti animation, updates UI
- On `cancelled`: shows cancellation message

### 6. Telegram Webhook (`/telegram/webhook`)

- Receives inline button callbacks from admin
- `confirm:<booking_id>` → calls `confirm_booking_paid()`, edits TG message
- `cancel:<booking_id>` → calls `cancel_booking_release()`, edits TG message
- Validates chat ID against `TELEGRAM_CHAT_ID`

## Database Schema

```sql
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    name TEXT DEFAULT '',
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    instagram TEXT DEFAULT '',
    session_type TEXT DEFAULT '',
    status TEXT DEFAULT 'reserved',
        -- 'reserved' → 'pending_payment' → 'confirmed'
        -- 'reserved' → 'cancelled'
        -- 'reserved' → 'expired'
    paid INTEGER DEFAULT 0,
    confirmed INTEGER DEFAULT 0,
    paid_amount REAL,
    created_at TEXT DEFAULT (datetime('now')),
    reserved_until TEXT,
    notion_page_id TEXT,
    calendar_event_id TEXT,
    calendar_event_url TEXT,
    UNIQUE(date, time)
);
```

## Security

- **HTTP Basic Auth** on all `/admin/*` endpoints
- **Rate limiting** on `/reserve` (10 requests per IP per 10 minutes)
- **SQL injection protection** via parameterized queries
- **Transaction safety** with `BEGIN IMMEDIATE` for slot operations
- **Input validation** on slot times (checked against event.json)
- **Telegram webhook** validates chat_id before processing
- **Secrets** in `.env` (excluded from git)

## Error Handling

- All external calls (Telegram, Notion, Calendar, Himalaya) wrapped in try/catch
- Failures logged but don't crash the booking flow
- Notion 401 → logged, booking proceeds
- Calendar failure → logged, booking proceeds
- Email send failure → logged, booking confirmed anyway
- Checker timeout → booking stays `pending_payment` until admin acts
