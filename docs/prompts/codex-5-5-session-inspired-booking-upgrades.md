# Codex 5.5 Prompt — Session-inspired booking upgrades without breaking current page

You are Codex 5.5 working inside the existing Pashynska Photography booking system.

Project path:
`/Users/andrzej/Iryna-Master/01-Booking-System`

Production app/domain:
- Fly app: `iryna-booking`
- Public booking URL: `https://book.pashynskaphoto.com`

## Non-negotiable rules

1. **Do not rebuild the app. Do not rewrite the current landing page.**
   - The current `templates/index_v2.html` drawer-based booking page is working and conversion-critical.
   - Preserve existing layout, event cards, drawer opening, slot selection, reserve flow, e-transfer primary payment flow, Stripe secondary flow, Telegram notifications, admin, Notion sync, confirmation email, calendar, and live success polling.

2. **Add features incrementally and safely.**
   - Prefer additive DB columns/tables and small helpers.
   - Use feature-compatible fallbacks: existing bookings/events without new fields must continue to work.
   - If an event has no add-ons/questionnaire/consent config, the client flow must behave exactly like before.

3. **Strict TDD.**
   - Write failing tests first.
   - Run targeted tests and confirm they fail for the expected reason.
   - Implement minimal code.
   - Run targeted tests and full suite.
   - Do not mark complete until tests pass.

4. **Do not create live production bookings unless explicitly approved.**
   - Use local tests and safe GET-only live smoke checks.
   - If a test booking is ever approved later, clean it up immediately.

5. **Preserve e-transfer as primary.**
   - Stripe/card remains optional/secondary.
   - Do not introduce Stripe-only friction like postal-code/card errors blocking the booking funnel.

6. **No tip system for now.**
   - Do not implement Session-style tips.
   - Upsells must be value-based add-ons, not tips.

7. **Do not commit secrets, CSVs, DBs, private payment history, or generated runtime files.**

## Context: what we are copying from Session.com

A competitor/Session.com booking flow has useful pieces:

- add-ons, e.g. short 30–60 sec reel
- amount due today / remaining balance wording
- questionnaire
- additional contract / model release
- main contract / agreement
- electronic signature
- email balance reminders
- visible selected booking summary with Change/Cancel

For our system, implement a simpler, conversion-safe version:

### Keep before-payment flow short

Before payment/deposit, collect only:
- basic contact fields that already exist
- optional add-ons
- one short terms checkbox
- marketing/model-release consent yes/no
- electronic signature name only, not heavy canvas signature yet

### Questionnaire strategy by session type

Mini sessions are intentionally fast and low-friction: clients arrive, shoot, and receive photos. **Do not show or require a questionnaire for mini sessions by default.** Mini-session clients may optionally leave a short note if an existing notes field exists, but do not add a mini-questionnaire step.

Questionnaires are for individual/custom/private sessions only, and they should happen **after the booking/payment is confirmed**. After confirmation, send the client an optional email link to fill a preparation questionnaire if they want to share preferences. The questionnaire must not block initial reservation, deposit/payment, or confirmation.

## Pricing/add-ons v1

Build add-ons so they can be configured in `events.yaml` per event. For the first implementation, use these business add-ons:

1. **10 Extra Edited Images — $50 CAD**
   - Add-on ID: `extra-10-edited-images`
   - This is a bundle/package. Do not implement a quantity picker yet.
   - Client-facing note: individual extra edited images can be purchased later from the gallery for `$10/image`, but the pre-booking add-on is only the 10-image package for `$50`.

2. **Short Vertical Behind-the-Scenes Reel — $50 CAD for mini sessions**
   - Add-on ID: `short-vertical-reel`
   - Short vertical video/reel **up to 1 minute** from the session, suitable for Instagram Reels, Stories, or family memories.
   - Client-facing wording should be polished, e.g. “Short Vertical Behind-the-Scenes Reel” or “Short Vertical Highlight Reel”. Use “behind-the-scenes” or “BTS” wording if needed; do not write “behind the stage.”
   - `$50 CAD` is the default price for mini sessions. For individual/custom/private sessions, price should be configurable per event in admin.

Do **not** implement “Full Gallery Upgrade” as a pre-booking add-on yet. Full edited gallery upgrades should be sold after the gallery preview, when the photographer knows how many strong images are available.

### Admin-configurable add-ons

The admin event editor should be able to control add-ons per event without editing YAML manually. Implement this pragmatically and safely:

- In `templates/admin_event.html` / event create-edit flow, add an **Add-ons** section.
- Provide checkboxes/toggles for built-in add-ons:
  - `10 Extra Edited Images`
  - `Short Vertical Behind-the-Scenes Reel`
- Each enabled add-on should have an editable price field. Defaults:
  - mini sessions: reel `$50`, 10 extra edited images `$50`
  - individual/custom/private sessions: admin can override manually per event
- Add a simple “custom add-on” row/repeater if it fits existing admin architecture:
  - title
  - description
  - price
  - active checkbox
- Store add-ons in the event config (`events.yaml`) under the event’s `addons:` list.
- Do not overbuild inventory, coupons, taxes per add-on, or complex pricing rules. This is per-event flat-price add-ons only.
- Existing events without add-ons must continue to save/load exactly as before.

Use this sample config for tests/dev fixtures and as the target event config shape:

```yaml
events:
- id: example-event
  # existing fields...
  addons:
  - id: extra-10-edited-images
    title: 10 Extra Edited Images
    description: Add 10 additional professionally edited images to your final gallery. Best value. Individual extra edited images can be purchased later for $10/image.
    price: 50.0
    active: true
  - id: short-vertical-reel
    title: Short Vertical Behind-the-Scenes Reel
    description: Add a short vertical video up to 1 minute from your session, perfect for Instagram Reels, Stories, or family memories.
    price: 50.0
    active: true
  extra_photo_note: Additional edited images can be purchased after your gallery preview for $10/image.
  # Mini sessions should omit questionnaire or set enabled: false.
  questionnaire:
    enabled: false
    timing: after_confirmed_payment
    session_types: [individual, custom, private]
    optional: true
    fields:
    - id: session_goals
      label: What would you love to capture in this session?
      type: textarea
      required: false
    - id: participants
      label: Who will participate? Names/ages if helpful.
      type: textarea
      required: false
    - id: prep_preference
      label: Do you prefer a video consultation or a message with preparation tips?
      type: select
      required: false
      options:
      - Video consultation
      - Message with suggestions and tips
      - Not sure yet
  agreement:
    enabled: true
    require_terms: true
    require_marketing_choice: true
```

## Desired product behavior

### A. Add-ons

Client-facing:
- In the booking drawer, after slot selection and before final reserve/payment confirmation, show an optional **Add-ons** section only if the selected event has active add-ons.
- Each add-on shows title, short description, and price.
- Client can select/unselect add-ons.
- Show updated price summary:
  - session total / package total
  - amount due today / deposit
  - remaining balance
- Add-ons affect full price / remaining balance. They should **not** silently change the deposit unless we explicitly add per-add-on deposit rules later.

Backend:
- Validate selected add-on IDs against the selected event config.
- Ignore inactive/unknown add-ons with a safe validation error, not silent corruption.
- Store selected add-ons and computed add-on total on the booking.
- Store price snapshot at booking time so later config changes do not rewrite old bookings.
- Existing balance helpers must include add-ons in total/full price.

Admin:
- Booking detail/admin list should show selected add-ons and add-on total.
- Confirmation/admin Telegram notification should include add-ons if selected.
- Client confirmation email should include add-ons if selected.
- Event admin create/edit UI should let Iryna enable/disable built-in add-ons per event and manually adjust prices.
- Event admin should support adding a simple custom flat-price add-on when needed.

### B. Marketing/model-release consent

Client-facing:
- Add a short required choice near terms:
  - `Yes, I allow Pashynska Photography to use selected photos/videos from this session for portfolio, website, social media, ads, and marketing.`
  - `No, please keep my gallery private.`
- This is a choice, not forced agreement. The booking must be allowed with `No`.
- Require the client to choose yes/no, so Iryna knows the status.

Backend:
- Store `marketing_consent` as `yes`/`no`/nullable for legacy.
- Store `agreement_name` / electronic signature name.
- Store `agreement_accepted_at` timestamp when terms are accepted.
- Store an immutable snapshot/version of the terms text or at least a `terms_version` string.

Admin/email:
- Admin detail page must show marketing consent clearly.
- Confirmation email should not over-emphasize legal details, but can mention the client’s selected privacy/marketing preference.

### C. Simple booking terms agreement

Client-facing:
- Add a required checkbox:
  - `I agree to the booking terms, including the non-refundable reservation payment and rescheduling/cancellation policy shown for this session.`
- Add required `Electronic signature / full legal name` field.
- Do not show a huge full contract before payment.
- Do not add canvas signature yet.

Backend:
- Reject reserve if terms checkbox missing when event agreement is enabled.
- Reject reserve if signature name is empty when agreement is enabled.
- Store signature name and accepted timestamp.

### D. Questionnaire foundation — only for individual/custom/private sessions

Implement foundation now, UI minimal and conversion-safe:
- Support per-event questionnaire config in `events.yaml`.
- **Mini sessions must not show or require a questionnaire by default.** If `session_type: mini`, omit questionnaire UI unless an event explicitly overrides this later.
- Questionnaires are for `individual`, `custom`, `private`, or other non-mini session types.
- Questionnaire timing should be `after_confirmed_payment`: send/show the optional questionnaire link only after the booking is confirmed/paid, not before deposit.
- Store questionnaire answers as JSON on booking or in an additive table.
- The post-booking questionnaire endpoint must be identity-safe with `booking_id + confirmation_token` and should be linked from confirmation/success email for eligible non-mini sessions.
- The questionnaire is optional unless a future event config explicitly says otherwise; do not block confirmation.

Minimum acceptable v1:
- DB storage exists.
- Admin detail can display answers if present.
- Mini-session reserve/payment flow has no questionnaire friction.
- Eligible non-mini confirmed bookings can receive/show an optional questionnaire link.
- Tests prove questionnaire fields do not break existing reserve flow.

### E. Better amount due / remaining balance wording

Where the drawer/payment/success/admin/email currently show payment amounts, make the language explicit:
- `Amount due today: $X CAD`
- `Remaining balance: $Y CAD`
- `Selected add-ons: $Z CAD` when applicable

Do not remove existing e-transfer instructions or card payment option.

### F. Change/Cancel visible summary

In the drawer/payment flow, keep/show a compact selected booking summary:
- date
- time
- session title
- location if available
- `Change time` / back-to-slot selection action before final reservation

Do not add a public cancel-paid-booking feature in this task. For unpaid holds, existing expiration/cancel mechanisms should remain.

## Suggested implementation approach

### Task 0 — Read-only discovery

Run:

```bash
pwd
git status --short
python3 --version
python3 -m pytest tests/test_booking_flow.py -q
python3 -m pytest tests/test_balance_payment_request.py -q
```

Inspect, at minimum:
- `app.py`
- `events.yaml`
- `templates/index_v2.html`
- `templates/admin.html`
- `templates/booking_detail.html`
- `templates/success.html`
- `tests/test_booking_flow.py`
- `tests/test_balance_payment_request.py`
- `tests/test_frontend_contract.py`
- `tests/test_admin_booking_detail.py`

Find existing helpers before adding new ones:
- `_booking_total_price`
- `_booking_balance_due`
- `_booking_paid_amount`
- `reserve_slot`
- event loading helpers
- DB migration/init function
- email rendering/sending helpers
- Telegram/admin notification helpers

### Task 1 — Add pure pricing helpers with tests first

Create or extend tests, likely in:
- `tests/test_addons_and_agreement.py` or existing relevant test file

Test behaviors:
- Event with no add-ons: selected add-ons total is `0`, full price unchanged.
- Event with active add-ons: selected IDs compute correct total.
- Unknown selected add-on ID raises/returns validation error.
- Inactive add-on cannot be selected.
- Price snapshot: booking total = base full price + selected add-ons.
- Balance due = full price with add-ons - paid/deposit, never below zero.

Implementation guidance:
- Add pure helper(s), for example:
  - `_event_active_addons(event) -> list[dict]`
  - `_validate_selected_addons(event, selected_ids) -> tuple[list[dict], float]`
  - `_booking_addons_total(booking) -> float`
  - update `_booking_total_price(booking, event)` carefully to include stored add-on total where present.
- Keep existing behavior if fields missing.

Run:

```bash
python3 -m pytest tests/test_addons_and_agreement.py -q
python3 -m pytest tests/test_balance_payment_request.py -q
```

### Task 2 — Add additive DB migrations with tests first

Add columns to `bookings` only if missing. Suggested fields:
- `selected_addons_json TEXT`
- `addons_total REAL DEFAULT 0`
- `marketing_consent TEXT`
- `agreement_name TEXT`
- `agreement_accepted_at TEXT`
- `terms_version TEXT`
- `questionnaire_answers_json TEXT`

Use existing migration style in `app.py` around the current `ALTER TABLE bookings ADD COLUMN ...` block.

Tests:
- Fresh DB initializes with new columns.
- Existing/legacy rows without new values still load and render.
- Reserve flow does not require new fields for events without agreement config.

Run targeted DB/init tests and existing booking flow tests.

### Task 3 — Backend reserve API support with tests first

Extend `POST /reserve` safely.

Expected request additions:

```json
{
  "addons": ["highlight-reel"],
  "marketing_consent": "yes",
  "terms_accepted": true,
  "agreement_name": "Client Legal Name",
  "questionnaire_answers": {
    "family_members": "..."
  }
}
```

Tests:
- Existing minimal reserve payload for old/no-agreement event still succeeds.
- Reserve with valid add-on stores JSON and total.
- Reserve with unknown add-on returns 400 and clear JSON error.
- Reserve with inactive add-on returns 400.
- Event with agreement enabled rejects missing `terms_accepted`.
- Event with agreement enabled rejects missing `agreement_name`.
- Event with `require_marketing_choice` rejects missing/invalid consent but allows both `yes` and `no`.
- Questionnaire answers are not requested/stored during reserve for mini sessions by default.
- Questionnaire answers are optional and post-confirmation for eligible non-mini sessions.
- Email sanitization and recaptcha soft-fallback behavior must not regress.

Do not break token generation, status, reserved_until, event_id, deposit_amount, full_price, Telegram notification, Notion sync, or timed e-transfer checker startup.

Run:

```bash
python3 -m pytest tests/test_addons_and_agreement.py -q
python3 -m pytest tests/test_booking_flow.py -q
python3 -m pytest tests/test_recaptcha_gate.py -q
```

### Task 4 — Frontend drawer UI with tests/contract checks first

Modify `templates/index_v2.html` minimally.

Frontend requirements:
- Render add-ons section only when selected event contains active add-ons.
- Keep existing slot selection and reserve button IDs/functions intact unless tests are updated intentionally.
- Add fields for marketing consent, terms checkbox, electronic signature name only when agreement enabled.
- Keep e-transfer primary and Stripe secondary hierarchy.
- Show amount due today / remaining balance summary.
- If no add-ons/agreement config, UI should look and behave like before.
- Add accessible labels and mobile-safe touch targets.

Tests:
- Extend `tests/test_frontend_contract.py` or landing tests to assert existing critical hooks still exist:
  - `/events` fetch contract
  - `/slots/<date>?event_id=<id>` contract
  - reserve POST hook still exists
  - drawer still opens
  - e-transfer/payment text still present
- Add tests/HTML assertions for new markers:
  - add-ons rendering function/markup exists
  - reserve payload includes selected add-ons/consent/signature when present
  - existing fields/selectors remain

If using browser automation locally, test without creating production bookings.

### Task 4.5 — Admin event editor add-on configuration with tests first

Modify the existing event create/edit admin flow without breaking event publishing.

Requirements:
- Add an add-ons section to the event admin UI.
- Built-in toggles:
  - `10 Extra Edited Images` default `$50`
  - `Short Vertical Behind-the-Scenes Reel` default `$50` for mini sessions
- Include editable price fields so individual/custom/private events can use a different price.
- If simple and compatible with current admin code, add support for custom flat-price add-ons with title/description/price/active.
- Persist to `events.yaml` as `addons:` under the event.
- Loading an existing event with add-ons should prefill the admin UI.
- Loading/saving existing events without add-ons should not add junk fields or break.

Tests:
- Admin event create/update can save built-in add-ons to `events.yaml`.
- Admin event update can disable an add-on.
- Admin event update can override reel price for an individual/custom event.
- Existing event without add-ons still saves correctly.
- Custom add-on input is validated and HTML-escaped in frontend/admin displays.

Run:

```bash
python3 -m pytest tests/test_admin_event_organizer.py -q
python3 -m pytest tests/test_addons_and_agreement.py -q
```

### Task 5 — Admin + booking detail display with tests first

Modify as needed:
- `templates/admin.html`
- `templates/booking_detail.html`
- admin API/route helpers in `app.py`

Display:
- selected add-ons list + total
- marketing consent yes/no/legacy unknown
- agreement name + accepted timestamp + terms version
- questionnaire answers if present

Tests:
- Admin detail renders legacy booking without new fields.
- Admin detail renders booking with add-ons/consent/agreement/questionnaire.
- HTML escapes client-provided values; no raw `<script>` from questionnaire/signature/add-on text.

Run:

```bash
python3 -m pytest tests/test_admin_booking_detail.py -q
python3 -m pytest tests/test_booking_detail_template.py -q
```

### Task 6 — Email + Telegram notification updates with tests first

Update client/admin communication without breaking existing side effects.

Email:
- Confirmation email includes add-ons if any.
- Confirmation email includes total/deposit/remaining balance wording where appropriate.
- Email safely escapes add-on names and questionnaire/consent values.

Telegram/admin notification:
- New booking notification includes selected add-ons and marketing consent if available.
- Existing Telegram buttons and webhook behavior unchanged.

Tests:
- Existing confirmation email tests still pass.
- Add content assertions for selected add-ons and amount wording.
- Add XSS escape test.
- Add Telegram HTML escaping test if notification composition helper exists.

Run:

```bash
python3 -m pytest tests/test_confirmation_email_design.py -q
python3 -m pytest tests/test_regression_known_bugs.py -q
```

### Task 7 — Identity-safe optional post-confirmation questionnaire endpoint for non-mini sessions

Implement only for eligible non-mini sessions; mini sessions should not show this by default.

Requirements:
- Route: `GET/POST /questionnaire?booking_id=<id>&token=<confirmation_token>` or similar.
- Must require token via constant-time check, like existing identity-safe success/payment pages.
- Must not expose other bookings.
- Must only be offered after booking is confirmed/paid, or at minimum must display copy saying it is an optional preparation questionnaire after booking.
- Saves answers to `questionnaire_answers_json`.
- Confirmation/success email should include the questionnaire link only for eligible non-mini events.
- Success page may link to it only for eligible non-mini events.

Tests:
- Mini-session confirmation email does **not** include questionnaire link.
- Non-mini eligible confirmation email includes optional questionnaire link.
- Missing/wrong token rejected.
- Correct token can save answers.
- Answers displayed in admin.

### Task 8 — Full verification

Run targeted tests first, then full suite:

```bash
python3 -m pytest tests/test_addons_and_agreement.py -q
python3 -m pytest tests/test_booking_flow.py -q
python3 -m pytest tests/test_balance_payment_request.py -q
python3 -m pytest tests/test_frontend_contract.py -q
python3 -m pytest tests/test_admin_booking_detail.py -q
python3 -m pytest tests/test_confirmation_email_design.py -q
python3 -m pytest tests/ -q
```

Production-safe live smoke after code is ready/deployed by maintainer:

```bash
curl -sS https://book.pashynskaphoto.com/ | head
curl -sS https://book.pashynskaphoto.com/events | python3 -m json.tool >/tmp/events.json
python3 - <<'PY'
import json
p='/tmp/events.json'
data=json.load(open(p))
print('events:', len(data.get('events', [])))
assert data.get('events') is not None
PY
```

If a local browser smoke is available:
- Open local app.
- Open a booking drawer.
- Select a slot.
- Verify add-ons show only for configured event.
- Select an add-on.
- Verify amount due/remaining balance updates.
- Verify terms/marketing/signature validation.
- Do not submit to production.

## Acceptance criteria

The implementation is complete only if:

- Existing events with no add-ons/agreement still book successfully.
- Current public page/drawer does not visually break.
- Existing reserve/payment/success/admin/email tests pass.
- Add-ons are configurable per event in `events.yaml` and through the admin event editor.
- Selected add-ons are validated, stored, shown in admin/email, and included in total/balance calculations.
- Marketing consent yes/no is required only for configured events and stored/displayed.
- Terms checkbox + electronic signature name are required only for configured events and stored/displayed.
- Questionnaire foundation exists but mini sessions have no questionnaire friction; eligible non-mini sessions get an optional post-confirmation questionnaire link.
- All client-provided content is escaped in HTML/email/Telegram contexts.
- Full test suite passes.
- No production booking pollution.

## After implementation, report back in Russian

Use this report format:

```markdown
✅ Готово: Session-inspired booking upgrades

Что добавлено:
- ...
- add-ons можно включать/выключать и настраивать по цене в админке события

Что специально НЕ трогал:
- текущую страницу не переписывал
- e-transfer остался primary
- Stripe/card остался secondary
- tips/coupons/gift cards не добавлял

Тесты:
- `python3 -m pytest ...` → result
- full suite → result

Проверка безопасности:
- legacy bookings/events OK
- unknown add-ons rejected
- XSS escaping OK
- token-protected questionnaire if implemented

Что нужно от Анджея дальше:
- подтвердить, к каким конкретно событиям добавить add-ons в `events.yaml`
- подтвердить, какие события считаются individual/custom/private и должны получать optional questionnaire link
```

## Important implementation notes from existing project memory

- Preserve booking CTAs to `book.pashynskaphoto.com`.
- Public assistant/booking site must guide clients to actual booking drawer, not Instagram DM, when functional sessions exist.
- Finance/payment data must stay out of Git/Docker.
- Do not hardcode payment amounts in JS; always compute from event/booking snapshot.
- For frontend/backend contracts, verify exact JSON shape before filtering client-side.
- The slots endpoint often requires `?event_id=<event_id>`; do not regress this.
- Confirmation paths need side-effect parity: admin, Telegram, Stripe, e-transfer should keep DB/email/Telegram/calendar/Notion behavior consistent.
