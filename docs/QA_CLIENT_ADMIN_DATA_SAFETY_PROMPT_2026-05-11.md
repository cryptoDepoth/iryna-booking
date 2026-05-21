# Pashynska Booking — Full Client/Admin/Data Safety QA Prompt

Created: 2026-05-11 19:18 MDT
Project: `/Users/andrzej/business/iryna/iryna-booking`
Live site: `https://pashynska.agency`

## Immediate status from this run

- File created and verified locally.
- Local regression command executed:
  ```bash
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest tests/test_booking_flow.py tests/test_frontend_contract.py tests/test_etransfer_checker.py -q
  ```
- Result: **32 passed, 2 warnings** in 28.50s.
- Warnings: two `datetime.utcnow()` deprecation warnings in `app.py`; not blockers for current QA.
- Deploy/live full production booking flow was **not executed yet** because production deploy + synthetic production booking are side-effect actions and should be done only after confirming scope/cleanup.

## Purpose

This file is a reusable prompt + operating checklist for properly testing the Pashynska Photography booking site from three angles:

1. **Client side** — as a real outside customer who knows nothing about the system.
2. **Admin side** — as Iryna/admin managing sessions, clients, confirmations, payments, exports, and recovery.
3. **Data safety side** — ensuring client/payment/booking data is not lost, not duplicated, not stored only in one fragile place, and can be recovered after failures.

The goal is not just “tests pass”. The goal is: **the business can trust this system with real people, real bookings, real payments, and real admin workflow.**

---

# Copy-paste master prompt for Hermes / future agent

Use this prompt when Andrzej says: “test the site properly”, “check production”, “audit booking flow”, or “make sure client/admin side works”.

```text
You are testing the Pashynska Photography booking platform as a brutally practical QA engineer, product strategist, and reliability engineer.

Target repo:
/Users/andrzej/business/iryna/iryna-booking

Live production site:
https://pashynska.agency

Primary rule:
Do not only run local unit tests. Test the system as a real client and as an admin. Verify local, deployed, browser, backend, database, and data-recovery behavior.

Production safety rule:
Do not create real production bookings that pollute the business unless Andrzej explicitly approves. If a production test booking is necessary, use clearly synthetic client data and clean it up immediately after verification. Never trigger real Stripe charges or real e-Transfer payments during QA.

Gmail cleanup rule:
When tests send emails to fake, typo, or synthetic addresses, Gmail may receive bounce/error messages such as “Mail Delivery Subsystem”, “Delivery Status Notification”, “Address not found”, “Message not delivered”, or `mailer-daemon`. These test-generated bounce emails must be deleted immediately or at the end of the test run so Andrzej’s/Iryna’s inbox is not cluttered. Include this cleanup in the QA report.

Test personas:
1. External client:
   - does not know the business internals;
   - lands on the site from Instagram or Google;
   - wants to understand what session is available, price, date, time, and how to pay;
   - books on mobile first;
   - expects payment/status page to be clear and live;
   - expects confirmation, calendar buttons, and follow-up to be obvious.

2. Admin/Iryna:
   - wants to see who booked, when, for what, payment status, amount, contact info, Instagram, notes;
   - wants to confirm/cancel without technical work;
   - wants confidence that client info is saved even if Telegram/Notion/email fails;
   - wants export/backups/search/filtering;
   - wants simple recovery after accidental cancellation, failed deploy, server restart, or missed payment detection.

3. Reliability/data-safety auditor:
   - assumes server can restart, Notion can fail, Telegram can fail, email can fail, Cloudflare/Fly can cache or route incorrectly;
   - checks that SQLite/local DB, production persistent volume, Notion sync, CSV/export, backups, and audit logs are consistent;
   - checks that no single failure silently loses client/payment data.

Required workflow:

PHASE 0 — Context and safety
- Inspect git status and do not overwrite Andrzej’s uncommitted work.
- Read AGENTS.md and docs/RUNBOOK.md.
- Identify active app path, active database path, live domain, Fly app, and current deployment commit/version.
- Decide whether this is a safe local-only QA, deploy QA, or production live QA.
- If production full booking is needed, ask Andrzej for explicit approval first.

PHASE 1 — Local automated tests
- Run the existing pytest suite.
- Include booking flow, frontend contract, e-Transfer checker, admin confirmation, status polling, cancellation/rebook, and security/token tests.
- If a test fails, do not blindly patch. Find root cause first.
- Save command output summary to a QA report file.

PHASE 2 — Local API/contract smoke
- Start the local Flask app on port 5001.
- Verify:
  - GET / returns 200.
  - GET /events returns valid events and at least one bookable event when expected.
  - GET /slots/<date>?event_id=<id> returns slots with the JSON shape the frontend expects.
  - POST /reserve works with synthetic data and creates a safe test booking.
  - POST /confirm transitions to pending_payment and creates/syncs required records.
  - GET /booking-status requires the correct identity token if that is part of current design.
  - cancellation releases the slot.
- Compare frontend JavaScript assumptions against actual JSON fields. No silent empty arrays.

PHASE 3 — Local browser client flow
Use browser automation like a real customer:
- Open local site.
- Check visual first impression: premium, clear, trustworthy, no broken layout.
- Check mobile-ish flow where possible.
- Click a session/event.
- Confirm drawer/page opens quickly and shows:
  - title;
  - date;
  - price/deposit;
  - location if configured;
  - available times;
  - clear CTA.
- Select slot.
- Fill client form with synthetic data.
- Continue to payment.
- Verify payment instructions are clear.
- Verify status page says pending until DB/admin confirms.
- Confirm no premature confetti or “confirmed” message.
- Confirm no JS console errors after every navigation and major interaction.

PHASE 4 — Local browser admin flow
Use admin like Iryna:
- Open admin login.
- Verify login works and failed login is handled safely.
- Confirm dashboard shows bookings/events clearly.
- Verify latest synthetic booking appears with all client fields:
  - name;
  - email;
  - phone;
  - Instagram;
  - session/event;
  - date/time;
  - amount/deposit;
  - status;
  - created_at/reserved_until;
  - payment method/source if available.
- Confirm paid manually.
- Verify DB status becomes confirmed, paid=1.
- Verify client status page switches to confirmed.
- Verify calendar CTAs appear only after confirmation.
- Cancel a synthetic booking and verify the slot is released.
- Test search/filter/export if present.

PHASE 5 — Data storage and loss-prevention audit
Verify data is stored in multiple recoverable layers:
- SQLite/production persistent volume is authoritative.
- Notion sync is secondary, not the only source of truth.
- Telegram notification is a notification layer, not the only record.
- Email/Interac checker has processed-email ledger to avoid duplicate processing.
- Audit trail exists or is proposed for:
  - booking created;
  - client confirmed details;
  - payment pending;
  - admin confirmed;
  - Telegram confirmed;
  - Stripe webhook confirmed;
  - e-Transfer auto-confirmed;
  - cancelled;
  - expired;
  - reminder sent;
  - export/backup created.
- Verify backups:
  - DB backup script exists or propose it;
  - backups are timestamped;
  - backups exclude secrets;
  - restore procedure is documented and tested at least locally.

PHASE 6 — Deploy verification
Before deploy:
- Run tests.
- Confirm git diff is intentional.
- Commit only if Andrzej wants a commit.

Deploy:
- Use the current project’s Fly deployment workflow.
- Wait for machine health.
- Verify live site serves the new code, not stale HTML.

After deploy:
- curl live domain and Fly domain if available.
- Verify /events and /slots contracts on production.
- Open https://pashynska.agency in browser.
- Repeat safe smoke client flow.
- Repeat admin smoke.
- Check console errors.
- Do not leave synthetic bookings on production; clean up or mark cancelled.

PHASE 7 — Gmail cleanup after email tests
- If any test sends email to fake/synthetic/invalid addresses, search Gmail for generated bounce messages:
  - Mail Delivery Subsystem;
  - Delivery Status Notification;
  - Address not found;
  - Message not delivered;
  - mailer-daemon / postmaster.
- Delete only bounce/error emails that were generated by the current test run.
- Do not delete real client emails.
- Record in the QA report how many test bounce messages were removed.

PHASE 8 — Report
Create a markdown QA report with:
- timestamp;
- git branch/commit;
- local test results;
- live deploy result;
- pages/endpoints tested;
- browser evidence/screenshots if available;
- issues found grouped by Critical/High/Medium/Low;
- exact reproduction steps;
- recommended fixes;
- what was not tested and why;
- whether production is safe for clients.

Final output to Andrzej must be direct:
- “Ready for clients” or “Not ready yet”.
- Top blockers.
- What I fixed.
- What still needs approval.
- Attach the QA report file.
```

---

# Client-side test checklist

## First impression / landing

- Page loads fast enough on live domain.
- No broken photos, missing fonts, or weird spacing.
- Client immediately understands:
  - what is being offered;
  - date/session options;
  - price/deposit;
  - how to book;
  - how to contact Iryna.
- CTA is obvious on desktop and mobile.
- If no active sessions exist, client gets a clear Instagram DM CTA instead of dead links.

## Booking drawer / flow

- Events load from `/events`.
- Event cards do not fail if clicked before JS data fully loads.
- Slot API uses exact contract: `/slots/<date>?event_id=<event_id>`.
- Slot objects match what frontend expects.
- Available slots render correctly.
- Already-booked/cancelled/expired slots behave correctly.
- Form validation catches:
  - empty name;
  - bad email;
  - missing phone;
  - weird Instagram handle;
  - double submit;
  - refreshing payment/status page.

## Payment/status page

- Pending booking shows pending/verifying, not confirmed.
- Confirmed booking shows confirmed immediately on first render.
- Status polling works without silent 500s.
- Google/Apple Calendar buttons appear only after confirmation.
- Calendar data uses correct event time, duration, and timezone.
- Payment instructions are simple and not scary.
- No confetti until actual DB-confirmed status.

## Edge cases

- Two clients try the same slot.
- Client abandons payment.
- Client refreshes after reserve.
- Client comes back later with booking URL.
- Client mistypes email/phone.
- Client uses mobile Safari-like layout.
- Network/API call fails during slot loading.

---

# Admin-side test checklist

## Login/admin dashboard

- Admin login is protected.
- Failed login does not leak details.
- Dashboard loads without JS errors.
- Admin can quickly see:
  - today/upcoming bookings;
  - pending payments;
  - confirmed clients;
  - cancelled/expired bookings;
  - source/payment method;
  - amount/deposit;
  - contact details.

## Booking management

- Admin can confirm payment manually.
- Manual confirm has side-effect parity with Stripe/e-Transfer/Telegram confirm:
  - DB confirmed;
  - paid=1;
  - Telegram admin notification if expected;
  - Notion sync/update;
  - confirmation email;
  - calendar event;
  - client status page updates.
- Admin can cancel/release a booking.
- Cancelled slot becomes bookable again.
- Admin can search/filter by:
  - date;
  - status;
  - client name;
  - email;
  - Instagram;
  - event/session.
- Admin can export bookings to CSV/JSON.

## Suggested admin improvements

High-impact admin features to consider:

1. **Client CRM view**
   - One page per client with all bookings, notes, contact info, payment history, and follow-up status.

2. **Audit log timeline**
   - Every booking has a timeline: created → pending → paid/confirmed → email sent → calendar created → Notion synced.

3. **Backup/export button**
   - Admin can download encrypted or plain CSV/JSON backup of bookings.

4. **Recovery dashboard**
   - Shows risky states:
     - pending too long;
     - paid but not confirmed;
     - confirmed but no email sent;
     - confirmed but Notion sync failed;
     - duplicate amount/payment ambiguity;
     - booking with missing contact info.

5. **Follow-up/reminder automation**
   - Pending payment reminder.
   - 24h/48h before shoot reminder.
   - Post-session thank-you / review request.

6. **Duplicate/race warning**
   - Admin warning if two bookings conflict on same event/date/time.

7. **Safe test mode**
   - Hidden QA event/session that can be booked and cleaned up without touching real client inventory.

---

# Data safety / anti-loss design

## Principle

The system should never rely on only one fragile channel. Telegram, Notion, email, Stripe, and e-Transfer detection are integrations. The authoritative source should be the booking database, backed up and auditable.

## Recommended layers

1. **Primary source of truth**
   - SQLite DB on persistent production volume.
   - Strict status fields: reserved, pending_payment, confirmed, cancelled, expired.
   - Identity-safe tokens for client status/payment links.

2. **Audit log table**
   Suggested table: `booking_audit_log`
   - id
   - booking_id
   - action
   - actor: client/admin/telegram/stripe/etransfer/system
   - payload_json
   - created_at
   - success/failure
   - error_message

3. **Backup system**
   - Nightly DB backup.
   - Backup before deploy.
   - Backup before schema migration.
   - Keep last 30 daily backups and last 12 monthly backups.
   - Store at least two places:
     - local project backups folder or Fly volume backup;
     - external storage such as Google Drive/Dropbox/S3/R2.
   - Never include `.env` or raw secrets in exported backups.

4. **Export system**
   - Admin CSV/JSON export.
   - Optional automatic daily CSV sent/saved to a secure folder.
   - Export includes booking/client/payment statuses but avoids leaking secret tokens unless explicitly needed.

5. **Integration reconciliation**
   - Notion sync failures should be visible and retryable.
   - Telegram delivery failure should be logged.
   - Email send failure should be logged and retryable.
   - Stripe/e-Transfer confirmations should be idempotent.
   - Duplicate payment amount ambiguity should notify admin instead of auto-confirming the wrong person.

6. **Restore procedure**
   - Document exact restore command.
   - Test restore locally from a backup.
   - After restore, run consistency checks:
     - no duplicate active bookings per event/date/time;
     - every confirmed booking has client contact;
     - every pending booking has reserved_until;
     - no orphan payment confirmations.

---

# Local → deploy → live verification sequence

## 1. Local preflight

```bash
cd /Users/andrzej/business/iryna/iryna-booking
git status --short
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest tests/test_booking_flow.py tests/test_frontend_contract.py tests/test_etransfer_checker.py -q
```

## 2. Local server

```bash
cd /Users/andrzej/business/iryna/iryna-booking
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 app.py
```

Open:

```text
http://127.0.0.1:5001/
```

## 3. Local API checks

```bash
curl -s http://127.0.0.1:5001/ | head
curl -s http://127.0.0.1:5001/events | python3 -m json.tool | head -80
```

Then use the first event date/id to check slots:

```bash
curl -s 'http://127.0.0.1:5001/slots/YYYY-MM-DD?event_id=EVENT_ID' | python3 -m json.tool
```

## 4. Local browser E2E

- Client flow.
- Admin flow.
- Status polling.
- Confirm/cancel.
- Console errors.
- Screenshots.

## 5. Deploy

Only after local checks pass:

```bash
flyctl deploy --remote-only --yes
```

Then verify:

```bash
curl -I https://pashynska.agency
curl -s https://pashynska.agency/events | python3 -m json.tool | head -80
```

## 6. Live browser smoke

- Open production site.
- Verify visual load.
- Verify events/slots.
- Safe booking smoke only if approved.
- Admin smoke.
- Clean up synthetic booking if created.

---

# Definition of “ready for real clients”

The site is ready only if:

- Local tests pass.
- Live site serves current deployed version.
- Client can see sessions and slots.
- Client can reserve and reach payment/status page.
- Pending/confirmed states are truthful.
- Admin can see, confirm, cancel, and recover bookings.
- Client data is stored in DB before any fragile notification/integration.
- Backups/export/recovery are documented or implemented.
- No critical/high JS console errors exist in main flows.
- Test-generated Gmail bounce/error messages are deleted after email tests.
- Synthetic production test data is cleaned up.
- QA report says “Ready for clients”.

---

# Immediate implementation backlog

## Critical / High

1. Add or verify `booking_audit_log` for every state transition.
2. Add backup/export command and admin-visible export button if not already present.
3. Add recovery dashboard for risky states.
4. Add safe QA/test event mode for full production tests without polluting real sessions.
5. Ensure confirmation side-effect parity across admin, Telegram, Stripe, and e-Transfer paths.
6. Ensure live status page cannot show confirmed/confetti before DB says confirmed.
7. Ensure production backups are not only “in the cloud” and not only Notion.

## Medium

1. Improve admin client CRM view.
2. Add retry queue for Notion/email/Telegram failures.
3. Add daily summary to admin: new bookings, pending payments, tomorrow’s sessions, risky states.
4. Add client reminder automation.
5. Add admin notes per client/booking.

## Nice-to-have

1. Browser screenshot regression snapshots for key pages.
2. Mobile viewport automated tests.
3. Admin activity search/filter polish.
4. One-click “send reminder” and “copy client message”.

---

# Final reporting format for Andrzej

```text
Status: Ready / Not ready

Tested:
- Local pytest: pass/fail
- Local browser client flow: pass/fail
- Local admin flow: pass/fail
- Deploy: pass/fail/not done
- Live browser client smoke: pass/fail/not done
- Live admin smoke: pass/fail/not done
- Data safety/backups: pass/fail/partial

Top blockers:
1. ...
2. ...
3. ...

Fixed now:
- ...

Needs approval:
- Production test booking yes/no.
- Deploy yes/no.
- Add backup/audit/admin recovery features yes/no.

Report file:
/path/to/report.md
```
