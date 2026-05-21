# Changelog — Pashynska Booking System

All notable changes to the Pashynska Photography Booking System.

## [1.0.0] — 2026-05-03

### Core Features
- **Booking flow:** Client selects slot → fills details → sends e-Transfer → live status page
- **Live status page:** Auto-updates from Pending → Confirmed (no page reload, 3s polling)
- **Confetti animation** on confirmation
- **Telegram admin notifications** with inline confirm/cancel buttons
- **e-Transfer verification:** Automatic email parsing via Himalaya CLI (IMAP)
- **Email confirmations:** Auto-sent to client when booking confirmed
- **Google Calendar integration:** Creates events for confirmed bookings
- **Notion sync:** Writes booking data to Notion database
- **Admin dashboard** at `/admin` with full booking management
- **Rate limiting** on slot reservations (10 req / 10 min per IP)

### Bug Fixes (session 2026-05-03)
- Fixed: `/confirm` no longer accepts bookings without prior `/reserve`
- Fixed: `ON CONFLICT DO UPDATE` removed — prevents overwriting other clients' bookings
- Fixed: Fake slot `99:99` rejected via strict `event.json` validation
- Fixed: Empty JSON body on `/confirm` returns 400 instead of 500
- Fixed: Slot availability query excludes `cancelled`/`expired` bookings
- Fixed: Cancelled slots can be re-reserved (old rows cleaned up)
- Fixed: Telegram chat_id pointed to legacy config — updated to current chat
- Fixed: Himalaya CLI v1.2 compatibility (removed `-a` flag, fixed `From:` header format)
- Fixed: Sender regex in e-Transfer parser (`Sent From:` pattern)
- Fixed: Himalaya JSON output normalization (string vs object)
- Fixed: Underpayment detection (e.g., $94.50 vs $95 deposit)

### Security
- HTTP Basic Auth on admin endpoints
- SQL injection protection via parameterized queries
- SQLite `BEGIN IMMEDIATE` for concurrent slot operations
- Telegram webhook validates chat_id
- Rate limiting prevents reservation spam
- Legacy LaunchAgents disabled (`com.pashynska.etransfer*`)

### Infrastructure
- Single canonical service at `/Users/andrzej/business/iryna/iryna-booking/`
- Legacy services disabled: `/Users/andrzej/business/iryna/booking/`, `/Users/andrzej/Iryna-Business/`
- Flask runs on port 5001
- Cloudflare Quick Tunnel for public access
- Background `timed_cron.py` process per booking (not permanent cron)
- File-based lock prevents duplicate payment checkers

### Documentation
- `docs/README_v1.md` — Full project documentation
- `docs/ARCHITECTURE.md` — System design and data flow
- `docs/API.md` — Complete API reference
- `docs/RUNBOOK.md` — Operations manual
- `docs/CHANGELOG.md` — This file
- `docs/agent-handoff/TECHNICAL_HANDOFF.md` — Detailed technical handoff

### Testing
- 5 regression tests in `tests/test_booking_flow.py`
- Covers: reserve→confirm flow, duplicate rejection, invalid slot, cancelled re-reservation, email triggering

### Known Limitations
- Notion API returns 401 (credentials need refresh)
- Cloudflare Quick Tunnel URL changes on restart
- Google Calendar requires OAuth credentials setup
- No permanent launchd service configured yet
- Deposit amount hardcoded in `event.json` (not per-booking)
