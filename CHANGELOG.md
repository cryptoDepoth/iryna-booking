# Changelog — Pashynska Photography Booking System

All notable changes to this project are documented here.
For detailed release notes, see the `.releases/` directory.

## Format

Each release follows this pattern:

```
## [YYYY-MM-DD] — Title

### What's new
- [Feature] Brief description

### Fixed
- [Bugfix] Brief description

### Security
- [Security] Brief description

### Data / Config changes
- What changed in DB schema, env vars, events.yaml

### Rollback info
- Previous working version: `flyctl deploy --app iryna-booking --image=PREV_IMAGE`
- Backup dir: `.bak.YYYY-MM-DD/`
```

---

## [2026-06-12] — Meta tracking unified, landing pages styled, durable balance page (not yet deployed)

### What's new
- [Feature] Landing pages `/wedding` `/family` `/maternity` are now fully styled —
  added the missing `static/css/styles.css` (branded, responsive, conversion-
  focused) + Google Fonts to `base_landing.html`. Previously rendered unstyled
  because the referenced stylesheet did not exist.
- [Feature] Durable balance-payment page `GET /pay-balance?booking_id=&token=`
  (Interac e-Transfer + on-demand Stripe). Unlike the old one-time Stripe URL
  (expires ~24h), this link never expires, so it lives in the confirmation email
  (step 2 of "What happens next") and on the success page, and works whenever the
  client settles up after the shoot. `POST /pay-balance/checkout` mints a fresh
  Stripe session on click.
- [Feature] Server-side Meta Purchase via Conversions API, gated on
  `META_CAPI_TOKEN` (fires on confirmed payment through any path; dedup with the
  browser pixel via `event_id=purchase.<id>`). See `META_ADS_ANALYTICS_PLAYBOOK_2026-06-12.md`.

### Fixed
- [Bugfix] Meta Pixel was inconsistent: `index_v2.html` fired `1806…` while the ad
  account/analytics used `1335…`. Unified to one `META_PIXEL_ID` (=`1335137335347797`)
  injected into every template via context processor — paid traffic now reports to
  the pixel the campaign optimizes against.
- [Bugfix] `Purchase` was never fired anywhere; `payment.html`/`success.html` had no
  pixel (funnel broke on navigation). Pixel + mapped events added to both.
- [Bugfix] Auto-confirmed e-Transfer clients never received a balance link; the
  admin path emailed an expiring Stripe URL. Both now use the durable `/pay-balance`
  link via `_client_email_context`.
- [Bugfix] Reverted a broken WIP tracking edit in `index_v2.html` (Jinja in a JS
  template literal + a malformed double-fire of `slot_selected`).

### Data / Config changes
- New env vars (documented in `.env.example`): `META_PIXEL_ID` (default `1335…`),
  `META_CAPI_TOKEN` (server Purchase; no-op until set), `META_CAPI_API_VERSION`,
  `META_TEST_EVENT_CODE`. No DB schema changes.
- Activate server Purchase: `fly secrets set META_CAPI_TOKEN=...`.

### Tests
- +15 tests (now 405 green): `test_meta_pixel_and_capi.py` (7), `test_balance_page.py` (8).

### Rollback info
- All changes are template/route/CSS additions + one config default; revert the
  commit and redeploy. No destructive migrations.

## [2026-06-11] — Private sessions: full deposit-style payment flow (not yet deployed)

### What's new
- [Feature] Private session booking now reuses the complete deposit machinery:
  client receives an email with a /payment page link where they choose Interac
  e-Transfer (auto-confirmed by the Gmail watcher) or Stripe Checkout
  (auto-confirmed by webhook); the page live-polls /booking-status and a
  confirmation email is sent automatically.
- [Feature] Admin modal: «Отправить письмо со ссылкой» + «Уже оплачено»
  checkboxes; payment link with Copy button shown after creation
  («Сгенерировать инвойс» removed — superseded by the booking-bound link).
- [Feature] e-Transfer matcher keeps private bookings matchable for 45 days
  (reserved or pending_payment) — clients pay emailed links days later.

### Fixed
- [Bugfix] /payment timer is now server-computed (TIMER_SECONDS) instead of a
  fake client-side 15:00 sessionStorage countdown; hidden entirely for private
  sessions and pending_payment (private bookings no longer get kicked off the
  page after 15 minutes).
- [Bugfix] /payment redirects finished bookings: confirmed → /success,
  expired/cancelled → landing (no payment form for dead bookings).
- [Bugfix] Stripe checkout amount comes from booking.deposit_amount (private =
  full price); 0-amount checkouts rejected; private product label no longer
  says "Deposit".
- [Bugfix] /confirm keeps reserved_until NULL for private sessions so the
  expiry sweep can never release an admin-created dedicated slot.

### Data / Config changes
- bookings.deposit_amount now stores the full price for private sessions
  (drives /payment, Stripe amount and e-Transfer expected-amount matching).
- ADMIN_PASSWORD rotated in local .env (2026-06-11); mirror to Fly:
  `flyctl secrets set ADMIN_PASSWORD=... -a iryna-booking`.

### Tests
- +11 tests (tests/test_private_session_flow.py): 345 passed, 1 skipped.

---

## [2026-06-10] — Security audit + lost-feature restore (not yet deployed)

### What's new
- [Feature] (WIP, committed) "Pay Remaining Balance" Stripe button in confirmation email

### Fixed
- [Bugfix] /admin/confirm no longer 500s when Stripe is unreachable; None-safe balance math
- [Bugfix] admin.html: restored add-ons editor, batch photo upload, photo carousel lost in 8f87f8e rollback
- [Bugfix] saveAllEventSettings: sequential writes (Promise.all race could revert price edits)
- [Bugfix] pytest is hermetic — no more live Stripe/Telegram/n8n calls from tests

### Security
- [Security] .env.qa with real ADMIN_PASSWORD untracked (** rotate password + ADMIN_KEY — see AUDIT_REPORT_2026-06-10.md §4 **)
- [Security] Dependencies: flask 3.1.3, requests 2.33.0, python-dotenv 1.2.2, Pillow 12.2.0 (pip-audit: 10 → 0 vulns)
- [Security] Werkzeug debug opt-in via FLASK_DEBUG=1 (was hardcoded debug=True on 0.0.0.0)
- [Security] Local .env: real SECRET_KEY generated (placeholder before), duplicate ADMIN_PASSWORD removed

### Data / Config changes
- .gitignore: all .env.* ignored except *.example; booking.db/qa/booking.db untracked
- requirements.txt versions bumped (run pip install -r requirements.txt)

### Rollback info
- Pre-audit state: commit 2272e93 (+ WIP preserved as 8ab645d)
- Full audit: AUDIT_REPORT_2026-06-10.md · Tests: 326 passed, 1 skipped

---

## [2026-05-16] — Booking detail card + reCAPTCHA fix + email improvements

### What's new
- [Feature] **Booking detail page** (`/admin/booking/<id>`) — click any client in admin table to see full card. Shows: client info, payment history, status timeline, actions.
- [Feature] **Reschedule** — move booking to another slot (admin only). Email + Telegram + Notion sync.
- [Feature] **Balance request** — send remaining balance request to client from admin.
- [Feature] **PDF Invoice** — generate invoice PDF for any booking.
- [Feature] **Wfolio gallery delivery** — paste Wfolio link, send gallery email to client.
- [Feature] **Google Review request** — send email asking client for Google review after session.
- [Feature] Confirmation email now includes:
  - 📍 Maps card with Google Maps + Apple Maps buttons (if `location_url` set in events.yaml)
  - 📅 "Add to Calendar" button (Google Calendar link)
  - ⏰ "Arrive 5–10 minutes early" reminder
  - 💰 Clear balance payment options listed

### Fixed
- [Bugfix] **"Verification failed" on booking** — reCAPTCHA v3 was firing `execute()` before script loaded. Added `grecaptcha.ready()` guard + toast "Please wait a moment".
- [Bugfix] **reCAPTCHA fail-closed** — if no `STRIPE_WEBHOOK_SECRET` set, webhook returns 503 instead of accepting unsigned events.

### Security
- [Security] Admin password rotated via `flyctl secrets set` (was in plaintext)
- [Security] Cookie `SECURE`, `HTTPONLY`, `SAMESITE=Lax`, 8h lifetime
- [Security] XSS fix: `|e` → `|tojson` in JS context for client names
- [Security] HSTS + CSP (with `frame-ancestors` for Wfolio) + Permissions-Policy
- [Security] `datetime.utcnow()` → timezone-aware `datetime.now(UTC)`
- [Security] 5× bare `except:` → `except Exception`
- [Security] Booking status compared via `hmac.compare_digest`
- [Security] HTML escaping in confirmation email fields

### Data / Config changes
- New (optional) field in `events.yaml`: `location_url` — for Maps card in email

### Rollback info
- Previous working version: see `.releases/2026-05-16-booking-card.md`
- Backup dir: `.bak.2026-05-16/`
