# DOX: Templates

## Purpose

Jinja2 templates for booking UI, admin dashboard, email templates, client-facing pages.

## Ownership

- `templates/base.html` — root layout, navigation, Google Fonts, Tailwind
- `templates/mini_session.html` — mini-session booking wizard
- `templates/admin.html` — admin dashboard (🔒 Private Session button critical)
- `templates/email_*.html` — transactional emails

## Local Contracts

- Mobile-first responsive design (client bookings are 80%+ mobile)
- 🔒 Private Session button MUST route to `/private` — if missing after rollback, patch immediately
- CTAs route to `book.pashynskaphoto.com`
- Use existing Tailwind CDN, no new CSS frameworks

## Work Guidance

- Any template change requires visual regression check on mobile + desktop
- Admin changes require login flow verification
- Email templates require end-to-end delivery test (check spam folder)

## Child DOX Index

- None.
