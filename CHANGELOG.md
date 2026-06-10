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
