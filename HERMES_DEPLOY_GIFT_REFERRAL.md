# Hermes Deployment: Gift Certificates + Referral System

## BEFORE DEPLOYING — What's actually needed

**Good news:** No new Stripe products or price IDs to create. Gift checkout uses dynamic `price_data` — prices are defined in code.

**Check what's already set:**
```bash
flyctl secrets list --app iryna-booking | grep -E "STRIPE|TEST_MODE"
```

| Secret | Status | Notes |
|--------|--------|-------|
| `STRIPE_SECRET_KEY` | ✅ already set | Used by existing deposit flow |
| `TEST_MODE` | ⚠️ likely NOT set | Defaults to `"true"` → bypasses Stripe for gifts! Must set to `false` |
| `STRIPE_WEBHOOK_SECRET` | optional | Only needed if gift success relies on webhook (it doesn't — uses sync Stripe API verify) |

**The only required action before deploy:** set `TEST_MODE=false`.

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

### 4. Set TEST_MODE=false (the only required new secret)

```bash
# CRITICAL — the gift checkout defaults TEST_MODE to "true" if not set.
# Without this, clicking "Purchase Gift Certificate" skips Stripe entirely.
flyctl secrets set TEST_MODE=false --app iryna-booking
```

Setting a secret triggers an automatic re-deploy. Wait for `✓ Deployment complete!`.

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
