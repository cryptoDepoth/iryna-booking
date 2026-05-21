# Fix report — Calendar buttons / Telegram confirm / deploy

## What was wrong

1. The live production site was still serving the old `success.html` template.
   - Live HTML did not include `booking-premium.css`.
   - Live HTML had the old calendar block:
     `id="calendar-buttons" style="display:none; ..."`
   - That is why the screenshot showed no Google/Apple calendar buttons.

2. Telegram inline confirmation felt broken/slow because the webhook did slow work synchronously before updating Telegram:
   - Google Calendar event creation
   - Notion sync
   - client email
   - Telegram message edit

3. Fly logs also showed an actual OOM event before this fix:
   - `Out of memory: Killed process ... gunicorn`
   - App is currently configured with 256MB RAM, which can cause sluggishness under admin/uploads/background work.

## What was fixed

1. Success page calendar CTA:
   - Added visible animated Google Calendar and Apple Calendar buttons.
   - Moved the CTA higher, immediately after the confirmed status section.
   - The CTA is visible only after booking is actually confirmed.

2. Telegram confirm:
   - Booking DB update happens immediately.
   - Telegram message is updated immediately.
   - Slow side effects now run in background thread.

3. Performance:
   - Reduced heavy confetti/petal count.
   - Disabled expensive mobile backdrop filters.
   - Limited hover transitions to real hover devices.

4. Security/robustness:
   - Telegram HTML now escapes client-provided fields.

## Verification

Local tests:
- `33 passed in 26.28s`

Live deploy:
- App: `iryna-booking`
- Fly machine version: `88`
- Image: `deployment-01KRCVBFEBA1SV8KJNGDF7Z7W4`

Live checks after deploy:
- `https://pashynska.agency/` → `200 text/html`
- `https://pashynska.agency/static/css/booking-premium.css?v=21st-1` → `200 text/css`
- Live `success.html` now contains:
  - `calendar-panel`
  - `Add your session to your calendar`
  - `booking-premium.css`
  - `google-cal-link`
  - `apple-cal-link`

## Important note

If the booking is still pending / verifying payment, the calendar CTA is intentionally hidden. It appears only when the booking becomes confirmed.

For `booking_id=1`, live HTML currently shows `Verifying Payment…`, so `calendar-panel` exists but does not have `show`. That means no calendar buttons on that pending page is expected.

After Telegram confirm succeeds on a real booking, the page should switch to confirmed and show the buttons.

## Remaining recommendation

Because logs showed OOM on a 256MB Fly machine, consider increasing Fly memory to 512MB or 1GB if the site continues feeling slow during admin uploads or background tasks.
