# Iryna Booking — Technical Handoff

_Last updated: 2026-05-03_

## Canonical service

There is only one active booking service now:

- Root: `/Users/andrzej/business/iryna/iryna-booking`
- Flask app: `app.py`
- SQLite DB: `bookings.db`
- Event config: `event.json`
- Templates: `templates/index.html`, `templates/payment.html`, `templates/success.html`, `templates/admin.html`
- Active payment checker: `timed_cron.py`
- e-Transfer parser/checker logic: `check_etransfer.py` and shared logic inside `timed_cron.py`
- Telegram admin notifications: `app.py` sends Bot API messages after `/confirm`
- Telegram callback webhook: `POST /telegram/webhook`
- Tests: `tests/test_booking_flow.py`
- Logs: `logs/`

Legacy services are intentionally disabled:

- `/Users/andrzej/Iryna-Business/etransfer_crm.py`
- `/Users/andrzej/business/iryna/booking/check_etransfer.py`

Their old launchd plists were moved to:

- `/Users/andrzej/business/iryna/iryna-booking/disabled_launchagents/`

Do not re-enable those legacy services unless the owner explicitly asks.

## Booking flow

The client flow is intentionally built around `booking_id`.

1. Client opens `/`.
2. Browser loads available slots from `/slots/<event_date>`.
3. Client chooses a real generated slot.
4. Frontend calls `POST /reserve` with `{time}`.
5. `/reserve` creates a SQLite row with:
   - `status='reserved'`
   - empty client details
   - `reserved_until` about 15 minutes in the future
   - returns `booking_id`
6. Client submits contact details and is redirected to `/payment?booking_id=...&time=...&name=...`.
7. Client sends e-Transfer manually.
8. Client clicks “I’ve Sent the Payment”.
9. Payment page calls `POST /confirm` with `booking_id` and contact details.
10. `/confirm` updates ONLY the existing reserved row:
    - no insert
    - no upsert by `(date,time)`
    - no overwrite of already pending/confirmed bookings
    - `status='pending_payment'`
    - `reserved_until` extended to about 20 minutes
11. `/confirm` starts `timed_cron.py --booking-id <id>` in background.
12. `/confirm` sends a Telegram admin notification with booking details and inline actions:
    - `✅ Confirm paid` → `POST /telegram/webhook` callback → `status='confirmed'`, `paid=1`, `confirmed=1`
    - `❌ Cancel / release` → `POST /telegram/webhook` callback → `status='cancelled'`, slot released from availability
    - Admin link and Instagram shortcut are included in the message.
13. Client lands on `/success?booking_id=<id>`.
14. Slot remains unavailable while `reserved_until` is active or booking is confirmed.
15. `timed_cron.py` checks Gmail/e-Transfer through Himalaya every 60 seconds for up to 20 minutes.
16. If payment matches, booking becomes:
    - `status='confirmed'`
    - `paid=1`
    - `confirmed=1`
17. If no payment arrives before timeout, unpaid pending rows are expired/released by cleanup.

## Safety rules

- `/confirm` must always require `booking_id`.
- `/confirm` must never create a booking directly.
- `/confirm` must never use `ON CONFLICT(date,time) DO UPDATE` for client data.
- `/reserve` must reject fake times such as `99:99`.
- A slot is valid only if it appears in `generate_slots(load_event_config())`.
- Do not log or paste tokens, API keys, Notion secrets, Telegram tokens, or admin passwords.
- Notion can be disabled safely by leaving `NOTION_API_KEY` empty.

## Important routes

- `GET /` — public booking page.
- `GET /slots/<date>` — returns available slots as JSON.
- `POST /reserve` — reserves a slot and returns `booking_id`.
- `GET /payment?booking_id=...` — payment instructions page.
- `POST /confirm` — client says payment was sent; starts active checker.
- `GET /success?booking_id=...` — verification-in-progress page.
- `GET|POST /expired` — releases expired unpaid reservations.
- `GET /admin` — admin dashboard, protected by HTTP Basic Auth if `ADMIN_PASSWORD` is set.
- `POST /admin/confirm` — manually confirm a booking.
- `POST /admin/cancel` — cancel a booking.
- `POST /telegram/webhook` — Telegram inline button callbacks for `confirm:<booking_id>` and `cancel:<booking_id>`.
- `GET|POST /admin/event` — read/update `event.json`.

## Configuration

Primary config file:

- `.env` — secrets and runtime options. Never commit or paste values.

Event/business config:

- `event.json`

Typical runtime values:

- Flask port: `5001`
- Public demo tunnel: Cloudflare Quick Tunnel to `http://localhost:5001`
- e-Transfer recipient email: configured in `event.json`
- Deposit amount: `event.json.deposit` / `event.json.deposit_due`
- Telegram bot token/chat IDs: `.env` keys `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `TELEGRAM_ADMIN_CHAT_ID`.
  - Important 2026-05-03 fix: active `.env` must point to the current Telegram recipient chat, not a copied legacy chat ID from `/Users/andrzej/business/iryna/booking`.
  - Verify with a direct Bot API `sendMessage` before declaring notifications fixed.
- Public base URL for Telegram buttons/webhook context: `.env` key `BOOKING_BASE_URL`

Telegram webhook setup for the current public URL:

```bash
python3 - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv('.env')
base=os.environ['BOOKING_BASE_URL'].rstrip('/')
token=os.environ['TELEGRAM_BOT_TOKEN']
r=requests.post(f'https://api.telegram.org/bot{token}/setWebhook', json={
    'url': f'{base}/telegram/webhook',
    'allowed_updates': ['callback_query'],
}, timeout=15)
print(r.json().get('ok'))
PY
```

## Tests

Run from project root:

```bash
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest tests/test_booking_flow.py -q
```

Expected result:

```text
4 passed
```

These tests cover:

- Direct `/confirm` without reservation is rejected.
- `/confirm` cannot overwrite existing booking data.
- Fake slot `99:99` is rejected.
- Successful `reserve → confirm` keeps slot hidden/unavailable.

Syntax check:

```bash
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m py_compile app.py timed_cron.py check_etransfer.py sync_notion.py gcal_helper.py
```

## Manual launch

```bash
cd /Users/andrzej/business/iryna/iryna-booking
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 app.py
```

Health check:

```bash
curl -I http://127.0.0.1:5001/
```

Cloudflare Quick Tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:5001
```

The Quick Tunnel URL changes after restart.

## Current known external dependency status

- Flask/SQLite local flow is covered by automated tests.
- e-Transfer active runner starts and exits cleanly when no matching booking exists.
- Real auto-confirmation depends on Himalaya Gmail access and real Interac notification emails.
- Notion API previously returned unauthorized with current secret/config. Treat Notion as optional until the token/database access is fixed by the owner. Do not expose the token.

## What changed on 2026-05-03

- Added regression tests: `tests/test_booking_flow.py`.
- Reworked `/reserve` validation and HTTP status codes.
- Reworked `/confirm` to require `booking_id` and update only existing active reservations.
- Removed unsafe client-side upsert by `(date,time)`.
- Frontend now carries `booking_id` from reserve page to payment page.
- `/confirm` starts `timed_cron.py --booking-id <id>` after the client clicks paid.
- `timed_cron.py` now supports `--booking-id` and lock cleanup.
- Disabled legacy launchd jobs pointing to old projects.

## If a future agent continues work

Start here:

1. Read this file.
2. Run tests.
3. Check `git diff` before editing.
4. Never re-enable legacy launchd jobs.
5. Fix Notion auth/schema only if the owner provides valid access or explicitly asks.
6. Before sending a public link, run the full client flow manually and verify SQLite rows.
