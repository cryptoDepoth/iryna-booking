# Gift Certificates + Referral Program

Self-contained Flask module for Pashynska Photography.
Drop-in blueprint — no changes to existing production routes required.

---

## Run locally

```bash
cd gift-referral/
pip install flask stripe reportlab Pillow   # already in main requirements.txt
python3 gift_referral_app.py
```

Then open:

| URL | What |
|-----|------|
| http://localhost:5001/gift | Gift certificate landing page |
| http://localhost:5001/referral/REF-TEST-0001 | Referral landing (test code) |
| http://localhost:5001/referral/share/1?email=a@b.com&name=Test | Share page |
| http://localhost:5001/admin/gifts | Admin: all certificates |
| http://localhost:5001/admin/referrals | Admin: all referral codes |

**TEST_MODE is on by default** — no real Stripe charges, emails printed to console.

---

## Run tests

```bash
cd gift-referral/
python3 -m pytest tests/ -v
```

All 10 test suites should pass (30+ individual assertions).

---

## Integrate into main app

### 1. Copy the module

The entire `gift-referral/` directory can live anywhere — it's self-contained.

### 2. Register the blueprint in `app.py`

```python
import sys
sys.path.insert(0, '/path/to/gift-referral')

from gift_referral_routes import gift_referral_bp
import gift_referral_db as gift_db

# In create_app() or at module level:
gift_db.init_db()  # creates tables if not exist
app.register_blueprint(gift_referral_bp)
```

That's it. Routes `/gift`, `/referral/*`, `/admin/gifts`, `/admin/referrals` are now live.

### 3. Trigger referral reward when payment is confirmed

In the section of `app.py` that handles deposit payment confirmation (e.g., Stripe webhook):

```python
import requests

def notify_payment_confirmed(booking_id):
    # ... existing logic ...
    
    # Trigger referral reward if this booking used a referral code
    try:
        requests.post(f"http://localhost:5000/referral/payment-confirmed/{booking_id}")
    except Exception:
        pass  # Non-blocking
```

Or call the DB function directly (if co-located):

```python
from gift_referral_db import confirm_referral_payment
from gift_referral_email import send_referral_reward_email

use = confirm_referral_payment(booking_id)
if use:
    send_referral_reward_email(
        owner_email=use['owner_email'],
        owner_name=use['owner_name'],
        friend_name=use.get('referee_name', 'Your friend'),
        reward=use['reward_for_owner'],
        code=use['referral_code'],
    )
```

### 4. Record referral use when a friend books

In the booking confirmation handler, after saving the booking:

```python
from gift_referral_db import validate_referral_code, record_referral_use

promo_code = request.form.get('promo_code', '').strip().upper()
if promo_code.startswith('REF-'):
    validation = validate_referral_code(promo_code, referee_email=booking_email)
    if validation['valid']:
        record_referral_use(
            code=promo_code,
            referee_email=booking_email,
            referee_name=booking_name,
            referee_booking_id=booking_id,
            discount_applied=validation['discount'],
        )
```

### 5. Show referral share page after booking success

In `success.html` (or the success route), add a link:

```html
<a href="/referral/share/{{ booking.id }}?email={{ booking.email }}&name={{ booking.name }}">
  🎁 Get $20 off your next session — share your code
</a>
```

### 6. Validate codes in the booking form (AJAX)

Add this to `index_v2.html`:

```html
<!-- In the booking form -->
<div class="gift-validate-wrap">
  <div class="gift-validate-row">
    <input type="text" id="gift-code-input" placeholder="GIFT-XXXX-XXXX or REF-PASHYN-XXXX">
    <button type="button" id="gift-validate-btn" class="gift-validate-btn">Apply</button>
  </div>
  <div class="gift-code-status" id="gift-code-status"></div>
  <input type="hidden" id="promo_code_applied" name="promo_code">
</div>
<link rel="stylesheet" href="/gift-static/gift/gift.css">
<script src="/gift-static/gift/gift.js"></script>
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TEST_MODE` | `true` | Skip Stripe + print emails to console |
| `GIFT_REFERRAL_DB` | `gift_referral.db` | SQLite DB path |
| `STRIPE_SECRET_KEY` | — | Stripe secret key (required in production) |
| `STRIPE_SUCCESS_URL` | — | Base URL for Stripe redirect (e.g. `https://book.pashynskaphoto.com`) |
| `BOOKING_URL` | `https://book.pashynskaphoto.com` | Base URL shown in emails and templates |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP server |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | — | SMTP username |
| `SMTP_PASS` | — | SMTP password / app password |
| `FROM_EMAIL` | `Iryna Pashynska Photography <...>` | From address |
| `SECRET_KEY` | `dev-secret-key-change-in-prod` | Flask session secret |

---

## Deploy checklist

- [ ] Set `TEST_MODE=false` in production `.env`
- [ ] Set `STRIPE_SECRET_KEY` and `STRIPE_SUCCESS_URL`
- [ ] Set SMTP credentials (`SMTP_USER`, `SMTP_PASS`)
- [ ] Set `GIFT_REFERRAL_DB` to a persistent path (e.g. `/data/gift_referral.db` on Fly volume)
- [ ] Set `SECRET_KEY` to a long random string
- [ ] Run `python3 -m pytest tests/ -v` — all green
- [ ] Test a real gift purchase end-to-end (small amount, real Stripe test key)
- [ ] Verify PDF attaches to purchaser email
- [ ] Verify referral reward email fires after confirming a payment

---

## File structure

```
gift-referral/
├── gift_referral_app.py       ← standalone dev server (port 5001)
├── gift_referral_routes.py    ← Flask Blueprint (drop-in)
├── gift_referral_db.py        ← SQLite DB layer
├── gift_referral_pdf.py       ← reportlab PDF generation
├── gift_referral_email.py     ← email send logic
├── templates/
│   ├── gift/
│   │   ├── gift_landing.html  ← /gift
│   │   └── gift_success.html  ← /gift/success
│   └── referral/
│       ├── referral_landing.html  ← /referral/<code>
│       └── referral_share.html    ← /referral/share/<id>
├── static/gift/
│   ├── gift.css               ← brand styles
│   └── gift.js                ← package selection + AJAX validation
├── pdfs/                      ← generated PDF certificates
├── tests/
│   ├── test_gift_certificates.py
│   └── test_referral_codes.py
└── README.md
```

---

## Business rules implemented

1. Gift certificate code can only be redeemed once (`status → redeemed`)
2. Referral discount activates only on **payment confirmed**, not on booking
3. Owner cannot use their own referral code (email match check)
4. Max 10 uses per referral code
5. Expired certificates (12+ months) return clear error message
6. Session-type match enforced: `mini` cert cannot be used on `family` booking
7. `custom` session type certificates work for any session
8. Referral reward cannot be triggered twice for the same booking
