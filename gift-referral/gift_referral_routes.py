"""
Flask Blueprint: gift_referral
Drop into main app with:
    from gift_referral_routes import gift_referral_bp
    app.register_blueprint(gift_referral_bp)
"""

import hashlib
import hmac
import os

import stripe
from flask import (
    Blueprint, abort, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

import gift_referral_db as db
from gift_referral_pdf import generate_gift_certificate_pdf, save_gift_pdf
from gift_referral_email import (
    send_gift_purchaser_email,
    send_gift_recipient_email,
    send_referral_reward_email,
    send_referral_welcome_email,
)

_here = os.path.dirname(os.path.abspath(__file__))

gift_referral_bp = Blueprint(
    "gift_referral",
    __name__,
    template_folder=os.path.join(_here, "templates"),
    static_folder=os.path.join(_here, "static"),
    static_url_path="/gift-static",
)

TEST_MODE         = os.environ.get("TEST_MODE", "true").lower() in ("1", "true", "yes")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL", "")
BOOKING_URL       = os.environ.get("BOOKING_URL", "https://book.pashynskaphoto.com")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

_TOKEN_SECRET = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")


def _make_credits_token(email: str) -> str:
    return hmac.new(_TOKEN_SECRET.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:16]


def _verify_credits_token(email: str, token: str) -> bool:
    return hmac.compare_digest(_make_credits_token(email), token)

PACKAGES = {
    "mini":       {"label": "Mini Session (30 min)",      "amount": 230.00, "amount_with_gst": 241.50},
    "family":     {"label": "Family Session (1 hour)",     "amount": 290.00, "amount_with_gst": 304.50},
    "maternity":  {"label": "Maternity Session (1 hour)",  "amount": 290.00, "amount_with_gst": 304.50},
    "individual": {"label": "Individual Session (1 hour)", "amount": 290.00, "amount_with_gst": 304.50},
    "custom":     {"label": "Custom Amount",               "amount": 0.0,    "amount_with_gst": 0.0},
}

# ---------------------------------------------------------------------------
# Gift Certificate routes
# ---------------------------------------------------------------------------

@gift_referral_bp.route("/gift")
def gift_landing():
    return render_template("gift/gift_landing.html", packages=PACKAGES, booking_url=BOOKING_URL)


@gift_referral_bp.route("/gift/checkout", methods=["POST"])
def gift_checkout():
    purchaser_name    = request.form.get("purchaser_name", "").strip()
    purchaser_email   = request.form.get("purchaser_email", "").strip().lower()
    recipient_name    = request.form.get("recipient_name", "").strip()
    recipient_email   = request.form.get("recipient_email", "").strip().lower()
    personal_message  = request.form.get("personal_message", "").strip()
    session_type      = request.form.get("session_type", "custom").strip()
    custom_amount_str = request.form.get("custom_amount", "0").strip()

    if not purchaser_name or not purchaser_email:
        return jsonify({"error": "Name and email are required"}), 400

    pkg = PACKAGES.get(session_type)
    if not pkg:
        return jsonify({"error": "Invalid session type"}), 400

    if session_type == "custom":
        try:
            amount = float(custom_amount_str)
            if amount < 50 or amount > 500:
                return jsonify({"error": "Custom amount must be between $50 and $500"}), 400
        except ValueError:
            return jsonify({"error": "Invalid amount"}), 400
        amount_with_gst = round(amount * 1.05, 2)
    else:
        amount          = pkg["amount"]
        amount_with_gst = pkg["amount_with_gst"]

    # Store form data in Flask session so we can retrieve it on success
    session["gift_form"] = {
        "purchaser_name":   purchaser_name,
        "purchaser_email":  purchaser_email,
        "recipient_name":   recipient_name,
        "recipient_email":  recipient_email,
        "personal_message": personal_message,
        "session_type":     session_type,
        "amount":           amount,
        "amount_with_gst":  amount_with_gst,
    }

    if TEST_MODE:
        # Skip Stripe — go straight to success with a mock session ID
        import secrets as _secrets
        mock_id = f"TEST_MOCK_{_secrets.token_hex(8).upper()}"
        return redirect(url_for("gift_referral.gift_success", session_id=mock_id))

    # --- Real Stripe Checkout ---
    success_url = (STRIPE_SUCCESS_URL or request.host_url.rstrip("/")) + "/gift/success?session_id={CHECKOUT_SESSION_ID}"
    cancel_url  = request.host_url.rstrip("/") + "/gift"

    stripe_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "cad",
                "product_data": {
                    "name": f"Gift Certificate — {pkg['label']}",
                    "description": f"Pashynska Photography · Recipient: {recipient_name or 'To be determined'}",
                },
                "unit_amount": int(amount_with_gst * 100),
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=purchaser_email,
        metadata={
            "gift_session_type":     session_type,
            "purchaser_name":        purchaser_name,
            "recipient_name":        recipient_name,
            "recipient_email":       recipient_email,
        },
    )
    return redirect(stripe_session.url)


@gift_referral_bp.route("/gift/success")
def gift_success():
    stripe_session_id = request.args.get("session_id", "")
    form = session.pop("gift_form", None)

    is_mock = stripe_session_id.startswith("TEST_MOCK_")

    if not is_mock and not TEST_MODE:
        # Verify real Stripe session
        try:
            stripe_sess = stripe.checkout.Session.retrieve(stripe_session_id)
            if stripe_sess.payment_status != "paid":
                return render_template("gift/gift_success.html", error="Payment not confirmed. Please contact us.")
            # Pull metadata if form not in Flask session
            if not form:
                meta = stripe_sess.metadata or {}
                form = {
                    "purchaser_name":   meta.get("purchaser_name", ""),
                    "purchaser_email":  stripe_sess.customer_email or "",
                    "recipient_name":   meta.get("recipient_name", ""),
                    "recipient_email":  meta.get("recipient_email", ""),
                    "personal_message": "",
                    "session_type":     meta.get("gift_session_type", "custom"),
                    "amount":           (stripe_sess.amount_total or 0) / 100 / 1.05,
                    "amount_with_gst":  (stripe_sess.amount_total or 0) / 100,
                }
        except stripe.StripeError as e:
            return render_template("gift/gift_success.html", error=f"Payment verification failed: {e}")

    if not form:
        # No form data at all — maybe session expired
        return render_template("gift/gift_success.html",
                               error="Session expired. If you completed payment, please email us with your receipt.")

    # Check for duplicate (idempotent on stripe_session_id)
    existing = db.get_gift_certificate_by_stripe_session(stripe_session_id) if stripe_session_id else None
    if existing:
        code = existing["code"]
        cert = existing
    else:
        code = db.create_gift_certificate(
            purchaser_email   = form["purchaser_email"],
            purchaser_name    = form["purchaser_name"],
            recipient_name    = form.get("recipient_name", ""),
            recipient_email   = form.get("recipient_email", ""),
            personal_message  = form.get("personal_message", ""),
            session_type      = form["session_type"],
            amount            = form["amount"],
            amount_with_gst   = form["amount_with_gst"],
            stripe_session_id = stripe_session_id,
        )
        cert = db.get_gift_certificate(code)

    # Generate PDF
    pdf_path = None
    try:
        pdf_path = save_gift_pdf(cert)
        db.update_gift_pdf(code, pdf_path)
    except Exception as e:
        print(f"[PDF ERROR] {e}")

    # Send emails
    send_gift_purchaser_email(cert, pdf_path=pdf_path)
    if cert.get("recipient_email"):
        send_gift_recipient_email(cert)

    return render_template("gift/gift_success.html", cert=cert, pdf_path=pdf_path)


@gift_referral_bp.route("/gift/certificate/<code>")
def download_certificate(code):
    cert = db.get_gift_certificate(code)
    if not cert:
        abort(404)
    # Regenerate on the fly if PDF not on disk
    if cert.get("pdf_path") and os.path.exists(cert["pdf_path"]):
        return send_file(cert["pdf_path"], mimetype="application/pdf",
                         as_attachment=True, download_name=f"GiftCertificate_{code}.pdf")
    # Re-generate
    pdf_bytes = generate_gift_certificate_pdf(cert)
    import io
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name=f"GiftCertificate_{code}.pdf")


@gift_referral_bp.route("/gift/validate", methods=["POST"])
def gift_validate():
    data         = request.get_json(silent=True) or {}
    code         = (data.get("code") or request.form.get("code", "")).strip().upper()
    session_type = (data.get("session_type") or request.form.get("session_type", "")).strip()
    if not code:
        return jsonify({"valid": False, "error": "No code provided"}), 400
    result = db.validate_gift_certificate(code, session_type or None)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Referral routes
# ---------------------------------------------------------------------------

@gift_referral_bp.route("/referral/<code>")
def referral_landing(code):
    code = code.strip().upper()
    ref  = db.get_referral_code(code)
    if not ref or ref["status"] != "active":
        return render_template("referral/referral_landing.html",
                               error="This referral code is invalid or expired.",
                               code=code, ref=None)
    return render_template("referral/referral_landing.html",
                           ref=ref, code=code, booking_url=BOOKING_URL)


@gift_referral_bp.route("/referral/validate", methods=["POST"])
def referral_validate():
    data          = request.get_json(silent=True) or {}
    code          = (data.get("code") or request.form.get("code", "")).strip().upper()
    referee_email = (data.get("email") or request.form.get("email", "")).strip().lower()
    if not code:
        return jsonify({"valid": False, "error": "No code provided"}), 400
    result = db.validate_referral_code(code, referee_email or None)
    return jsonify(result)


@gift_referral_bp.route("/referral/share/<int:booking_id>")
def referral_share(booking_id):
    # In production, owner email comes from the booking record.
    # For demo, pull from query param or create a test code.
    owner_email = request.args.get("email", "").strip().lower()
    owner_name  = request.args.get("name", "Photographer Friend").strip()

    if not owner_email:
        return render_template("referral/referral_share.html",
                               error="Missing owner email.", code=None)

    # Get or create referral code for this owner
    ref = db.get_referral_code_by_owner(owner_email)
    if not ref:
        code = db.create_referral_code(owner_email, owner_name, owner_booking_id=booking_id)
        ref  = db.get_referral_code(code)

    referral_url  = f"{BOOKING_URL}/referral/{ref['code']}"
    share_msg     = (
        f"I just booked a photo session with Pashynska Photography! "
        f"Use my code {ref['code']} and get ${ref['discount_for_friend']:.0f} off your first session 📸  "
        f"{referral_url}"
    )
    credits       = db.get_credit_balance(owner_email)
    credits_token = _make_credits_token(owner_email)
    credits_url   = f"{BOOKING_URL}/my-credits?email={owner_email}&token={credits_token}"
    return render_template(
        "referral/referral_share.html",
        ref=ref, referral_url=referral_url, share_msg=share_msg,
        credits=credits, credits_url=credits_url,
    )


# Unified validate — tries gift first, then referral/credit
@gift_referral_bp.route("/validate", methods=["POST"])
def unified_validate():
    data         = request.get_json(silent=True) or {}
    code         = (data.get("code") or request.form.get("code", "")).strip().upper()
    session_type = (data.get("session_type") or request.form.get("session_type", "")).strip()
    email        = (data.get("email") or request.form.get("email", "")).strip().lower()
    if not code:
        return jsonify({"valid": False, "error": "No code provided"}), 400

    if code.startswith("GIFT-"):
        return jsonify(db.validate_gift_certificate(code, session_type or None))
    if code.startswith("REF-"):
        # Passing email allows credit_redemption flow when owner uses their own code
        return jsonify(db.validate_referral_code(code, email or None))

    # Try gift, then referral
    g = db.validate_gift_certificate(code, session_type or None)
    if g["valid"]:
        return jsonify(g)
    r = db.validate_referral_code(code, email or None)
    return jsonify(r)


# ---------------------------------------------------------------------------
# Webhook: called by main app when a friend's payment is confirmed
# ---------------------------------------------------------------------------

@gift_referral_bp.route("/referral/payment-confirmed/<int:booking_id>", methods=["POST"])
def referral_payment_confirmed(booking_id):
    """
    Main app calls this after confirming a deposit payment for booking_id.
    If this booking used a referral code, triggers the owner reward.
    """
    use = db.confirm_referral_payment(booking_id)
    if not use:
        return jsonify({"triggered": False, "reason": "No pending referral use found"})

    send_referral_reward_email(
        owner_email=use["owner_email"],
        owner_name=use["owner_name"],
        friend_name=use.get("referee_name") or "Your friend",
        reward=use["reward_for_owner"],
        code=use["referral_code"],
        new_balance=use.get("new_balance", use["reward_for_owner"]),
        total_earned=use.get("total_earned", use["reward_for_owner"]),
    )
    return jsonify({
        "triggered": True,
        "owner_email": use["owner_email"],
        "new_balance": use.get("new_balance"),
        "total_earned": use.get("total_earned"),
    })


# ---------------------------------------------------------------------------
# My Credits page
# ---------------------------------------------------------------------------

@gift_referral_bp.route("/my-credits")
def my_credits():
    email = request.args.get("email", "").strip().lower()
    token = request.args.get("token", "")

    if not email:
        return render_template("referral/my_credits.html",
                               error="Missing email parameter.", credits=None)

    # In TEST_MODE skip token check so it's easy to demo
    if not TEST_MODE and not _verify_credits_token(email, token):
        return render_template("referral/my_credits.html",
                               error="Invalid or expired link. Please use the link from your email.",
                               credits=None)

    credits = db.get_credit_balance(email)
    history = db.get_credit_history(email) if credits else []
    ref     = db.get_referral_code_by_owner(email)

    referral_url  = f"{BOOKING_URL}/referral/{ref['code']}" if ref else None
    share_msg     = (
        f"I just booked a photo session with Pashynska Photography! "
        f"Use my code {ref['code']} and get ${ref['discount_for_friend']:.0f} off your first session 📸  "
        f"{referral_url}"
    ) if ref else None

    return render_template(
        "referral/my_credits.html",
        email=email,
        credits=credits,
        history=history,
        ref=ref,
        referral_url=referral_url,
        share_msg=share_msg,
        booking_url=BOOKING_URL,
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

_ADMIN_HTML_HEADER = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body{font-family:sans-serif;margin:40px;background:#FAF7F2;color:#2C2C2C;}
h1{color:#C4973A;}h2{color:#555;}
table{border-collapse:collapse;width:100%;margin-bottom:40px;background:#fff;}
th{background:#C4973A;color:#fff;padding:10px 12px;text-align:left;font-size:13px;}
td{padding:8px 12px;border-bottom:1px solid #E8D5A3;font-size:13px;}
tr:hover td{background:#FEF9F2;}
.active{color:#2a7a2a;font-weight:bold;}
.redeemed{color:#888;}
.expired{color:#c00;}
a{color:#C4973A;}
.nav{margin-bottom:24px;}
.nav a{margin-right:16px;font-size:14px;}
</style></head><body>
<h1>Pashynska Photography — Gift &amp; Referral Admin</h1>
<div class="nav">
  <a href="/admin/gifts">Gift Certificates</a>
  <a href="/admin/referrals">Referral Codes</a>
  <a href="/gift">Gift Landing</a>
</div>"""

_ADMIN_HTML_FOOTER = "</body></html>"


@gift_referral_bp.route("/admin/gifts")
def admin_gifts():
    certs = db.list_gift_certificates()
    rows  = ""
    for c in certs:
        status_cls = c["status"] if c["status"] in ("active", "redeemed", "expired") else ""
        rows += (
            f"<tr><td>{c['id']}</td>"
            f"<td><code>{c['code']}</code></td>"
            f"<td>{c['purchaser_name']}<br><small>{c['purchaser_email']}</small></td>"
            f"<td>{c.get('recipient_name','') or '—'}</td>"
            f"<td>{c.get('session_type','') or 'custom'}</td>"
            f"<td>${c['amount_with_gst']:.2f}</td>"
            f"<td class='{status_cls}'>{c['status']}</td>"
            f"<td>{c['created_at'][:10]}</td>"
            f"<td>{c.get('expires_at','') or '—'}</td>"
            f"<td>{'✓' if c.get('pdf_sent') else '—'}</td>"
            f"<td><a href='/gift/certificate/{c['code']}'>PDF</a></td></tr>"
        )
    html = (
        _ADMIN_HTML_HEADER
        + f"<h2>Gift Certificates ({len(certs)})</h2>"
        + "<table><thead><tr>"
        + "<th>#</th><th>Code</th><th>Purchaser</th><th>Recipient</th>"
        + "<th>Session</th><th>Amount</th><th>Status</th>"
        + "<th>Created</th><th>Expires</th><th>PDF Sent</th><th>Download</th>"
        + "</tr></thead><tbody>"
        + rows
        + "</tbody></table>"
        + _ADMIN_HTML_FOOTER
    )
    return html


@gift_referral_bp.route("/admin/referrals")
def admin_referrals():
    codes   = db.list_referral_codes()
    uses    = db.list_referral_uses()
    credits = db.list_credit_balances()

    # Build a quick lookup: owner_email → balance
    credit_map = {c["owner_email"]: c for c in credits}

    code_rows = ""
    for r in codes:
        cr      = credit_map.get(r["owner_email"])
        bal     = f"${cr['balance']:.0f}" if cr else "—"
        earned  = f"${cr['total_earned']:.0f}" if cr else "—"
        credits_url = f"/my-credits?email={r['owner_email']}&token={_make_credits_token(r['owner_email'])}"
        code_rows += (
            f"<tr><td>{r['id']}</td>"
            f"<td><code>{r['code']}</code></td>"
            f"<td>{r['owner_name']}<br><small>{r['owner_email']}</small></td>"
            f"<td>{r['uses_count']}/{r['max_uses']}</td>"
            f"<td>${r['discount_for_friend']:.0f} / ${r['reward_for_owner']:.0f}</td>"
            f"<td style='color:#C4973A;font-weight:bold'>{bal}</td>"
            f"<td>{earned}</td>"
            f"<td>{r['status']}</td>"
            f"<td>{r['created_at'][:10]}</td>"
            f"<td>{r.get('expires_at','') or '—'}</td>"
            f"<td><a href='{credits_url}'>Credits</a></td></tr>"
        )

    use_rows = ""
    for u in uses:
        confirmed = "✓" if u.get("payment_confirmed") else "—"
        rewarded  = "✓" if u.get("reward_triggered") else "—"
        use_rows += (
            f"<tr><td>{u['id']}</td>"
            f"<td><code>{u['code']}</code></td>"
            f"<td>{u.get('referee_name','') or '—'}<br><small>{u['referee_email']}</small></td>"
            f"<td>${u.get('discount_applied',0):.0f}</td>"
            f"<td>{confirmed}</td><td>{rewarded}</td>"
            f"<td>{u['created_at'][:10]}</td></tr>"
        )

    credit_rows = ""
    for c in credits:
        c_email = c["owner_email"]
        c_token = _make_credits_token(c_email)
        credit_rows += (
            f"<tr><td>{c['owner_name']}<br><small>{c_email}</small></td>"
            f"<td style='color:#C4973A;font-weight:bold;font-size:16px'>${c['balance']:.0f}</td>"
            f"<td>${c['total_earned']:.0f}</td>"
            f"<td>{c['updated_at'][:10]}</td>"
            f"<td><a href='/my-credits?email={c_email}&token={c_token}'>View</a></td></tr>"
        )

    html = (
        _ADMIN_HTML_HEADER
        + f"<h2>Credit Balances ({len(credits)} owners with credits)</h2>"
        + "<table><thead><tr>"
        + "<th>Owner</th><th>Balance</th><th>All-time Earned</th><th>Last Updated</th><th>Details</th>"
        + "</tr></thead><tbody>"
        + (credit_rows or "<tr><td colspan='5' style='color:#aaa;text-align:center'>No credits yet</td></tr>")
        + "</tbody></table>"
        + f"<h2>Referral Codes ({len(codes)})</h2>"
        + "<table><thead><tr>"
        + "<th>#</th><th>Code</th><th>Owner</th><th>Uses</th>"
        + "<th>Discount / Reward</th><th>Balance</th><th>Earned</th>"
        + "<th>Status</th><th>Created</th><th>Expires</th><th>Credits</th>"
        + "</tr></thead><tbody>"
        + code_rows
        + "</tbody></table>"
        + f"<h2>Referral Uses ({len(uses)})</h2>"
        + "<table><thead><tr>"
        + "<th>#</th><th>Code</th><th>Referee</th><th>Discount</th>"
        + "<th>Paid</th><th>Rewarded</th><th>Date</th>"
        + "</tr></thead><tbody>"
        + use_rows
        + "</tbody></table>"
        + _ADMIN_HTML_FOOTER
    )
    return html
