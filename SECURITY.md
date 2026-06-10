# SECURITY.md — Pashynska Photography Booking System

Last audit: **2026-06-10** (see `AUDIT_REPORT_2026-06-10.md`). Previous: `SECURITY_AUDIT_2026-05-13.md`.

## Threat model in one paragraph

Public booking site (Flask + SQLite on Fly.io) handling deposits via Stripe Checkout and
storing PII for ~1,700 real clients. Admin panel drives pricing, refunds and client data.
Biggest assets: client PII in `/data/bookings.db`, Stripe keys, admin credentials.

## Controls in place (verified 2026-06-10)

- **AuthN/AuthZ**: all `/admin*` routes behind `@admin_required`; browser session login with
  `hmac.compare_digest`, 10 attempts / 15 min / IP; session cookies Secure+HttpOnly+SameSite=Lax, 8h TTL.
- **SQL**: parameterized everywhere; dynamic WHERE/SET clauses built only from hardcoded fragments.
- **Double-booking**: `UNIQUE(date, time)` + `BEGIN IMMEDIATE` transactions.
- **XSS**: Jinja autoescape, zero `|safe`, no `render_template_string`; emails escape via `_html_escape`.
- **CSRF**: SameSite=Lax + JSON-only admin APIs; `form-action` limited in CSP.
- **Headers**: CSP (with frame-ancestors for Wfolio embed), HSTS, X-Frame-Options, nosniff.
- **Stripe**: webhook signature verified (`construct_event`), rejected if secret unset;
  idempotency keys on checkout creation.
- **Bots**: reCAPTCHA v3 on reserve, rate limits on login/assistant/analytics endpoints.
- **Dependencies**: `pip-audit` clean as of 2026-06-10.
- **Tests**: 327 tests; conftest blanks live credentials so the suite can never hit prod APIs.

## Rules

1. **Never commit**: `.env*` (except `*.example`), `*.db`, `token.json`, `credentials.json`,
   `FLY_API_TOKEN.txt`, anything from `02-Clients-CRM`. `.gitignore` enforces this — don't `git add -f`.
2. **Prod secrets live only in Fly**: `flyctl secrets set KEY=value -a iryna-booking`.
3. **Rotate immediately** if a secret ever lands in git, even in a private repo
   (history persists through clones and backups). Known incident: `.env.qa` ≤ 2026-06-10.
4. **Deploy** only after `python3 -m pytest tests/ -q` is green; never with `FLASK_DEBUG=1`.
5. **Client data** (`bookings.db`, CRM exports) never leaves the machine / Fly volume.

## Known accepted risks (review next audit)

- `?key=` admin API access leaks the key into access logs — migrate external callers
  (n8n, cron) to the `X-Admin-Key` header, then remove the query-param path.
- CSP allows `'unsafe-inline'` scripts (reCAPTCHA/inline handlers) — nonce migration pending.
- `events.yaml` has no backend file lock; concurrent admin+cron writes can still race
  (frontend race fixed 2026-06-10).
