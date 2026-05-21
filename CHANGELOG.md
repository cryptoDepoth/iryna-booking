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
