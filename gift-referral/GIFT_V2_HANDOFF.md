# Gift Certificates v2 — Handoff for the next agent

_Last updated: 2026-06-17. Branch: `feat/gift-referral-module`._

## Done & verified this round
- **Homepage "Gift Cards" button** — gold, pulsing, with shine.
  - `templates/index_v2.html` nav → `.nav-gift` (always visible).
  - `templates/base_landing.html` nav (Wedding/Family/Maternity pages) → `.nav-gift-cta`.
- **`/gift` certificate now shows a real photo per package** (random from a pool) + **Shuffle photo** button + a prominent **duration** pill.
  - Pools live in `gift_referral_catalog.py` → `GIFT_PHOTOS` (same-origin `/images/...`, brand `/static/og-image.jpg` fallback). Exposed via `public_catalog()` → consumed in `static/gift/gift.js` (`initGiftPhotos`, `randomPhoto`, `applyPhoto`).
  - `<img>` has an `onerror` fallback to the brand image, so a removed event photo never breaks the card.
  - Ivory style hides the seal (simpler/editorial); Noir & Botanical keep it.
- **Chosen photo is persisted securely**: new `photo_url` column (migration in `gift_referral_db._ensure_gift_columns`). Checkout runs `_safe_photo()` in `gift_referral_routes.py` — **only URLs in the curated catalog are stored**; anything else falls back to a pool default. Prevents arbitrary/attacker URLs.
- **Gift success page wow** (`templates/gift/gift_success.html`):
  - Share row: native **Web Share API**, WhatsApp, Email, Copy code.
  - **QR code** (client-side `qrcodejs` from cdnjs) → opens booking.
  - **Refer-a-friend $20 + $20** block — every purchaser auto-gets a `REF-...` code via `_referral_for()` (uses existing `referral_codes` table; $20 friend / $20 owner).

Verification: `pytest tests/test_gift_certificates.py` = 23 passed; render + card checkout + e-Transfer pending smoke; security test (malicious photo URL sanitized).

## Environment notes
- Sandbox has **no flyctl** and **cannot write `.git`**. Deploy + commit must be run by the user.
- Deploy ships the working tree: `flyctl deploy` from `01-Booking-System/`.

## TODO — next (payment-critical, do NOT ship blind)
### 1. Promo/referral code on the booking checkout
Goal: customer enters a code on the booking page → $20 off (referral) or gift-cert value applied.
- Validate server-side via the existing unified endpoint `POST /validate` (gift `GIFT-...` returns amount; referral `REF-...` returns `$20`).
- **Touch points** (all in the main app, must stay consistent):
  - `app.py` `/reserve` (POST, ~line 4722) — `deposit_amount` / `full_price` stored on the booking.
  - `stripe_create_checkout` (~5846) — `unit_amount` derives from the deposit.
  - `check_etransfer_v2.py` — **matches the incoming Interac amount against the expected deposit**. If a discount lowers what the client sends, the matcher's expected amount + memo must change too, or payments won't reconcile.
  - Balance math (`/pay-balance`, success pages).
- **Guards required**: cap discount ≤ price; never allow negative/zero-charge without intent; one code per booking; mark code used only after payment confirmed (idempotent).
- **Test**: `pytest` full suite + `tests/test_etransfer_audit_fixes.py` + a **real low-value test booking** (card and e-Transfer) before relying on it.

### 2. Trigger the referral payout
When a booking's deposit is confirmed, call `POST /referral/payment-confirmed/<booking_id>` (or `gift_db.confirm_referral_payment`) so the referrer gets their $20 credit. Wire this where deposits are confirmed (Stripe webhook/success + e-Transfer matcher).

### 3. Buyer's own photo on the certificate
- Add file input on `/gift`; handle multipart in `/gift/checkout`.
- Validate with Pillow (open+verify, re-encode to strip metadata), cap size/dimensions, accept jpeg/png/webp only.
- Save under a writable dir (Fly volume, e.g. `/data/gift_photos/<code>.jpg`); serve via a new `/gift/photo/<code>` route; set `photo_url` server-side (bypasses `_safe_photo`, which is for the curated pool only).
- Optionally embed into the PDF (`gift_referral_pdf.py`) via `reportlab` `drawImage` from the local file path (guard so PDF never fails if the image is missing).

## Quick reference
- Catalog/photos/durations: `gift_referral_catalog.py`
- Routes (checkout, success, validate, referral, admin): `gift_referral_routes.py`
- DB schema + migrations: `gift_referral_db.py`
- Live page: `templates/gift/gift_landing.html` + `static/gift/gift.{css,js}`
- Success/share: `templates/gift/gift_success.html`
- Standalone visual preview (no backend): `certificate_preview_v2.html`
