# Hermes Deployment: Gift Certificates + Referral System

## BEFORE DEPLOYING — Iryna needs to provide:

| Secret | Description |
|--------|-------------|
| `STRIPE_SECRET_KEY` | Live Stripe secret key (starts with `sk_live_...`) |
| `STRIPE_GIFT_PRICE_MINI` | Stripe Price ID for the Mini Session gift package (e.g. `price_...`) |
| `STRIPE_GIFT_PRICE_STANDARD` | Stripe Price ID for the Standard Session gift package |
| `STRIPE_GIFT_PRICE_EXTENDED` | Stripe Price ID for the Extended Session gift package |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret (starts with `whsec_...`) |

Create the Stripe Products/Prices in the Stripe Dashboard first, then copy the Price IDs here.

---

## Step-by-Step Deployment Instructions

### 1. Navigate to the project directory

```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System
```

### 2. Push the branch to origin and merge to main

```bash
git push origin feat/gift-referral-module
```

Then on GitHub, open a PR from `feat/gift-referral-module` → `main` and merge it.
Or fast-forward locally if on main:

```bash
git checkout main
git merge feat/gift-referral-module
git push origin main
```

### 3. Deploy to Fly.io

```bash
flyctl deploy --app iryna-booking
```

Wait for the deploy to finish (look for `✓ Deployment complete!`).

### 4. Set Stripe secrets (skip any already set)

Replace the placeholder values with Iryna's real keys:

```bash
flyctl secrets set STRIPE_SECRET_KEY=sk_live_REPLACE_ME --app iryna-booking
flyctl secrets set STRIPE_GIFT_PRICE_MINI=price_REPLACE_ME --app iryna-booking
flyctl secrets set STRIPE_GIFT_PRICE_STANDARD=price_REPLACE_ME --app iryna-booking
flyctl secrets set STRIPE_GIFT_PRICE_EXTENDED=price_REPLACE_ME --app iryna-booking
flyctl secrets set STRIPE_WEBHOOK_SECRET=whsec_REPLACE_ME --app iryna-booking
```

Setting secrets triggers an automatic re-deploy. Wait for it to finish.

### 5. Verify the deploy

```bash
# Gift landing page — expect 200
curl -s -o /dev/null -w "%{http_code}" https://book.pashynskaphoto.com/gift

# Referral landing page — expect 200 or 302 (redirect for unknown code)
curl -s -o /dev/null -w "%{http_code}" https://book.pashynskaphoto.com/referral/TEST

# My credits page — expect 200 or 302 (redirect if not logged in)
curl -s -o /dev/null -w "%{http_code}" https://book.pashynskaphoto.com/my-credits

# Health check — expect {"status": "ok"}
curl https://book.pashynskaphoto.com/healthz
```

### 6. Smoke test end-to-end (optional but recommended)

1. Open https://book.pashynskaphoto.com/gift in a browser
2. Click a package → confirm Stripe Checkout opens
3. Complete a test purchase with Stripe test card `4242 4242 4242 4242`
4. Confirm gift certificate PDF is emailed to the buyer
5. Open the referral link from the email → confirm $20 discount appears at booking

---

## Rollback

If anything breaks:

```bash
flyctl releases list --app iryna-booking   # find the previous release number
flyctl deploy --image <previous-image> --app iryna-booking
```
