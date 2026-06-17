"""
Standalone Flask app for local testing of the Gift + Referral module.
Run: python3 gift_referral_app.py
Then visit: http://localhost:5001/gift

TEST_MODE is on by default — no real Stripe or email.
"""

import os
import sys

# Ensure this directory is on the path so imports work when run directly
sys.path.insert(0, os.path.dirname(__file__))

# Force test mode for local dev
os.environ.setdefault("TEST_MODE", "true")
os.environ.setdefault("GIFT_REFERRAL_DB", os.path.join(os.path.dirname(__file__), "gift_referral.db"))
os.environ.setdefault("BOOKING_URL", "http://localhost:5001")

from flask import Flask, redirect, url_for
import gift_referral_db as db
from gift_referral_routes import gift_referral_bp

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
    static_url_path="/static",
)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")

# Register the blueprint (no url_prefix — routes like /gift, /referral are top-level)
app.register_blueprint(gift_referral_bp)


@app.route("/")
def index():
    return redirect(url_for("gift_referral.gift_landing"))


# Seed some test data for easy manual testing
def _seed_test_data():
    try:
        # Test gift certificate
        existing = db.get_gift_certificate("GIFT-TEST-0001")
        if not existing:
            db.create_gift_certificate(
                purchaser_email  = "purchaser@example.com",
                purchaser_name   = "John Smith",
                recipient_name   = "Jane Doe",
                recipient_email  = "jane@example.com",
                personal_message = "Hope you love it!",
                session_type     = "mini",
                amount           = 210.0,
                amount_with_gst  = 220.50,
                certificate_style= "signature",
                package_label    = "Mini Session",
                stripe_session_id= "TEST_SEED_GIFT",
            )
            # Manually set the code so it's predictable for tests
            conn = db.get_db()
            conn.execute(
                "UPDATE gift_certificates SET code = 'GIFT-TEST-0001' WHERE stripe_session_id = 'TEST_SEED_GIFT'"
            )
            conn.commit()
            conn.close()
            print("[seed] Created test gift certificate: GIFT-TEST-0001")

        # Test referral code
        existing_ref = db.get_referral_code("REF-TEST-0001")
        if not existing_ref:
            db.create_referral_code(
                owner_email      = "anna@example.com",
                owner_name       = "Anna Sample",
                owner_booking_id = 1,
            )
            conn = db.get_db()
            conn.execute(
                "UPDATE referral_codes SET code = 'REF-TEST-0001' WHERE owner_email = 'anna@example.com'"
            )
            conn.commit()
            conn.close()
            print("[seed] Created test referral code: REF-TEST-0001")

    except Exception as e:
        print(f"[seed] Warning: {e}")


if __name__ == "__main__":
    db.init_db()
    _seed_test_data()
    print()
    print("=" * 55)
    print("  Pashynska Photography — Gift & Referral Module")
    print("  TEST_MODE is ON (no real Stripe/email)")
    print("=" * 55)
    print("  GET  http://localhost:5001/gift")
    print("  POST http://localhost:5001/gift/validate")
    print("  GET  http://localhost:5001/referral/REF-TEST-0001")
    print("  GET  http://localhost:5001/referral/share/1?email=a@b.com&name=Test")
    print("  GET  http://localhost:5001/admin/gifts")
    print("  GET  http://localhost:5001/admin/referrals")
    print("=" * 55)
    print()
    app.run(host="0.0.0.0", port=5001, debug=True)
