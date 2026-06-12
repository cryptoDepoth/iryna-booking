# DOX: Booking System

- DOX is highly performant AGENTS.md hierarchy installed here
- Agent must follow DOX instructions across any edits

## Purpose

Production mini-session photography booking platform for Pashynska Photography (Calgary). Handles mini-session scheduling, private session booking, e-Interac payments, Google Calendar sync, admin dashboard, automated reminders, and client communications.

## Ownership

- Andrzej — ops, deploy, infrastructure, QA
- Iryna Pashynska — content, pricing, business logic
- Claude Code / Hermes Agent — code changes, tests, deploys

## Local Contracts

- CTAs across all properties route to `book.pashynskaphoto.com`
- GBP prices use CAD; Products need photos
- Interac CSV/payment history stays OUT of Git/Docker — use admin import/reconciliation UI
- Minis stay fast/no questionnaire by default; optional post-confirmation
- Add-ons v1: 10 edited images $50, BTS reel $50
- Timezone: `America/Edmonton` (MT/MST) — `_local_now()` / `_local_today()` in app.py

## Work Guidance

- Avoid enterprise overengineering. Small-to-medium SaaS.
- Do not rewrite working systems unless absolutely necessary.
- Prioritize: reliability, simplicity, conversion rate, mobile UX, booking safety.
- Private session button 🔒 is critical — if missing after rollback, patch `admin.html` + Flask routing.
- Before deploying: `pytest` must pass 354 tests (was 352 before timezone fix).

## Verification

- Run `pytest` before any deploy (target: 354 green)
- Run `hermes live-qa` smoke test after deploy
- Check `book.pashynskaphoto.com` booking flow end-to-end after any templates/app.py changes

## User Preferences

- User says "по очереди" = execute sequentially, not parallel.
- User says "оба" = execute ALL options in parallel.
- User says "nie rabotaet" = immediate diagnosis + fix, not explanation.
- User expects structured emoji-rich Russian reports with direct verification links.
- Voice-driven interaction preferred for complex requests.
- End-to-end verification required for email delivery — always check spam + suggest whitelisting.
- NEVER auto-send drafts/content to Iryna's Gmail without prior explicit approval.

## Child DOX Index

- `tests/AGENTS.md` — test conventions, running tests, regression tests
- `templates/AGENTS.md` — template conventions, UI changes, mobile UX
- `docs/AGENTS.md` — documentation for agents, handoff files, plans
