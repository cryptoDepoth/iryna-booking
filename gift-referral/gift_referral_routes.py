"""
Flask Blueprint: gift_referral
Drop into main app with:
    from gift_referral_routes import gift_referral_bp
    app.register_blueprint(gift_referral_bp)
"""

import hashlib
import hmac
import json
import os
import re

import stripe
from flask import (
    Blueprint, abort, jsonify, redirect, render_template, render_template_string,
    request, send_file, session, url_for,
)

import gift_referral_db as db
from gift_referral_catalog import (
    CERTIFICATE_STYLES,
    CUSTOM_BASES,
    GIFT_ADD_ONS,
    GIFT_PACKAGES,
    GIFT_PHOTOS,
    GIFT_PHOTO_FALLBACK,
    calculate_with_gst,
    public_catalog,
)
from gift_referral_pdf import generate_gift_certificate_pdf, save_gift_pdf
from gift_referral_email import (
    send_gift_purchaser_email,
    send_gift_recipient_email,
    send_gift_pending_payment_email,
    send_referral_invite_notification_email,
    send_referral_reward_email,
    send_referral_welcome_email,
)
from gift_security import (
    check_rate_limit,
    check_honeypot,
    validate_name_field,
    validate_email_field,
    validate_optional_email,
    validate_message_field,
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
INTERAC_EMAIL     = (
    os.environ.get("ETRANSFER_EMAIL")
    or os.environ.get("PHOTOGRAPHER_EMAIL")
    or os.environ.get("GMAIL_USER")
    or "iryna.pashynska@gmail.com"
)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

_TOKEN_SECRET = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")


def _make_credits_token(email: str) -> str:
    return hmac.new(_TOKEN_SECRET.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:16]


def _verify_credits_token(email: str, token: str) -> bool:
    return hmac.compare_digest(_make_credits_token(email), token)

PACKAGES = {
    key: {**value, "amount_with_gst": calculate_with_gst(value["amount"])}
    for key, value in GIFT_PACKAGES.items()
}

# Only certificate photos from our own curated catalog may be stored. This prevents
# arbitrary / attacker-supplied URLs from ever being persisted or rendered.
_ALLOWED_PHOTOS = {url for urls in GIFT_PHOTOS.values() for url in urls}
_ALLOWED_PHOTOS.add(GIFT_PHOTO_FALLBACK)


def _safe_photo(url: str, session_type: str) -> str:
    url = (url or "").strip()
    if url in _ALLOWED_PHOTOS:
        return url
    pool = GIFT_PHOTOS.get(session_type) or []
    return pool[0] if pool else GIFT_PHOTO_FALLBACK


def _selected_addons(addon_ids: list[str]) -> list[dict]:
    addons: list[dict] = []
    for addon_id in addon_ids:
        addon = GIFT_ADD_ONS.get(addon_id)
        if not addon:
            raise ValueError(f"Invalid add-on: {addon_id}")
        addons.append({
            "id": addon_id,
            "label": addon["label"],
            "amount": float(addon["amount"]),
        })
    return addons


def _package_label(session_type: str, custom_base: str, addons: list[dict]) -> str:
    if session_type == "custom":
        base = CUSTOM_BASES.get(custom_base, CUSTOM_BASES["custom_30"])
        label = f"Custom Gift Certificate - {base['label']}"
    else:
        label = GIFT_PACKAGES[session_type]["label"]
    if addons:
        label = f"{label} + " + " + ".join(addon["label"] for addon in addons)
    return label


def _referral_for(email: str, name: str) -> dict:
    """Get or create the purchaser's own referral code so they can share & earn $20."""
    email = (email or "").strip().lower()
    empty = {"ref_code": None, "referral_url": None, "referral_msg": None,
             "referral_friend": 20, "referral_owner": 20}
    if not email:
        return empty
    try:
        ref = db.get_referral_code_by_owner(email)
        if not ref:
            code = db.create_referral_code(email, name or "Friend")
            ref = db.get_referral_code(code)
        if not ref:
            return empty
    except Exception as exc:
        print(f"[REFERRAL CODE ERROR] {exc}")
        return empty
    url = f"{BOOKING_URL}/referral/{ref['code']}"
    friend = int(ref.get("discount_for_friend") or 20)
    owner = int(ref.get("reward_for_owner") or 20)
    msg = (f"I love Pashynska Photography in Calgary! Use my code {ref['code']} "
           f"for ${friend} off your first session: {url}")
    return {"ref_code": ref["code"], "referral_url": url, "referral_msg": msg,
            "referral_friend": friend, "referral_owner": owner}


def _validate_checkout_form(form) -> list[str]:
    """
    Run all input-validation checks on the checkout form.
    Returns a list of error strings (empty = all valid).
    """
    errors: list[str] = []

    def _check(ok: bool, msg: str) -> None:
        if not ok:
            errors.append(msg)

    ok, msg = validate_name_field(form.get("purchaser_name", ""), "Your name")
    _check(ok, msg)

    ok, msg = validate_email_field(form.get("purchaser_email", ""), "Your email")
    _check(ok, msg)

    # Recipient fields are optional but must pass validation if provided
    recipient_name = (form.get("recipient_name") or "").strip()
    if recipient_name:
        ok, msg = validate_name_field(recipient_name, "Recipient name")
        _check(ok, msg)

    ok, msg = validate_optional_email(form.get("recipient_email", ""), "Recipient email")
    _check(ok, msg)

    ok, msg = validate_message_field(form.get("personal_message", ""), "Personal message")
    _check(ok, msg)

    return errors


# ---------------------------------------------------------------------------
# Gift Certificate routes
# ---------------------------------------------------------------------------

@gift_referral_bp.route("/gift")
def gift_landing():
    return render_template(
        "gift/gift_landing.html",
        packages=PACKAGES,
        custom_bases=CUSTOM_BASES,
        add_ons=GIFT_ADD_ONS,
        certificate_styles=CERTIFICATE_STYLES,
        gift_config=public_catalog(),
        booking_url=BOOKING_URL,
    )


@gift_referral_bp.route("/gift/checkout", methods=["POST"])
def gift_checkout():
    # ------------------------------------------------------------------
    # 1. Honeypot — bots fill hidden fields, humans don't
    # ------------------------------------------------------------------
    if not check_honeypot(request.form):
        # Silently reject; don't hint that this is a bot filter
        return redirect(url_for("gift_referral.gift_landing"))

    # ------------------------------------------------------------------
    # 2. Rate limiting — max 3 gift-checkout POSTs per IP per hour
    # ------------------------------------------------------------------
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if not check_rate_limit(client_ip, max_requests=3, window_seconds=3600):
        return render_template(
            "gift/gift_landing.html",
            packages=PACKAGES,
            custom_bases=CUSTOM_BASES,
            add_ons=GIFT_ADD_ONS,
            certificate_styles=CERTIFICATE_STYLES,
            gift_config=public_catalog(),
            booking_url=BOOKING_URL,
            error="Too many requests. Please try again in an hour.",
        ), 429

    # ------------------------------------------------------------------
    # 3. Input validation
    # ------------------------------------------------------------------
    errors = _validate_checkout_form(request.form)
    if errors:
        return render_template(
            "gift/gift_landing.html",
            packages=PACKAGES,
            custom_bases=CUSTOM_BASES,
            add_ons=GIFT_ADD_ONS,
            certificate_styles=CERTIFICATE_STYLES,
            gift_config=public_catalog(),
            booking_url=BOOKING_URL,
            error=errors[0],
        ), 400

    # ------------------------------------------------------------------
    # 4. Parse form fields (only after passing all security checks)
    # ------------------------------------------------------------------
    purchaser_name    = request.form.get("purchaser_name", "").strip()
    purchaser_email   = request.form.get("purchaser_email", "").strip().lower()
    recipient_name    = request.form.get("recipient_name", "").strip()
    recipient_email   = request.form.get("recipient_email", "").strip().lower()
    personal_message  = request.form.get("personal_message", "").strip()
    session_type      = request.form.get("session_type", "custom").strip()
    custom_base       = request.form.get("custom_base", "custom_30").strip()
    certificate_style = request.form.get("certificate_style", "signature").strip()
    payment_method    = request.form.get("payment_method", "card").strip().lower()
    photo_url         = _safe_photo(request.form.get("gift_photo", ""), session_type)

    pkg = PACKAGES.get(session_type)
    if not pkg:
        return jsonify({"error": "Invalid session type"}), 400
    if certificate_style not in CERTIFICATE_STYLES:
        return jsonify({"error": "Invalid certificate style"}), 400

    try:
        addons = _selected_addons(request.form.getlist("gift_addons"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if session_type == "custom":
        base = CUSTOM_BASES.get(custom_base)
        if not base:
            return jsonify({"error": "Invalid custom package"}), 400
        amount = float(base["amount"])
    else:
        custom_base = ""
        amount = float(pkg["amount"])

    amount += sum(addon["amount"] for addon in addons)
    amount_with_gst = calculate_with_gst(amount)
    package_label = _package_label(session_type, custom_base or "custom_30", addons)
    addons_json = json.dumps(addons, separators=(",", ":"))

    if payment_method not in ("card", "interac"):
        return jsonify({"error": "Invalid payment method"}), 400

    # ------------------------------------------------------------------
    # 5a. e-Transfer path: create pending record, NO email to purchaser.
    #     Instructions are shown on the /gift/pending/<code> page.
    #     Admin confirms payment via /admin/gift-pending which triggers email.
    # ------------------------------------------------------------------
    if payment_method == "interac":
        code = db.create_gift_certificate(
            purchaser_email   = purchaser_email,
            purchaser_name    = purchaser_name,
            recipient_name    = recipient_name,
            recipient_email   = recipient_email,
            personal_message  = personal_message,
            session_type      = session_type,
            amount            = amount,
            amount_with_gst   = amount_with_gst,
            custom_base       = custom_base,
            certificate_style = certificate_style,
            package_label     = package_label,
            addons_json       = addons_json,
            photo_url         = photo_url,
            payment_method    = "interac",
            payment_status    = "pending",
            paid_amount       = 0.0,
            payment_reference = "Interac e-Transfer pending",
            status            = "pending_payment",
        )
        # Note: no email sent here. The pending page shows e-Transfer instructions.
        # Admin uses /admin/gift-pending to confirm payment and trigger delivery.
        print(f"[GIFT e-Transfer] Pending cert created: {code} for {purchaser_email} — awaiting admin confirmation")
        return redirect(url_for("gift_referral.gift_pending", code=code))

    # ------------------------------------------------------------------
    # 5b. Stripe path: store form in session, redirect to Stripe.
    #     Email is ONLY sent in /gift/success after verifying payment_status == 'paid'.
    # ------------------------------------------------------------------
    session["gift_form"] = {
        "purchaser_name":    purchaser_name,
        "purchaser_email":   purchaser_email,
        "recipient_name":    recipient_name,
        "recipient_email":   recipient_email,
        "personal_message":  personal_message,
        "session_type":      session_type,
        "custom_base":       custom_base,
        "certificate_style": certificate_style,
        "package_label":     package_label,
        "addons_json":       addons_json,
        "photo_url":         photo_url,
        "amount":            amount,
        "amount_with_gst":   amount_with_gst,
    }

    if TEST_MODE:
        import secrets as _secrets
        mock_id = f"TEST_MOCK_{_secrets.token_hex(8).upper()}"
        return redirect(url_for("gift_referral.gift_success", session_id=mock_id))

    success_url = (STRIPE_SUCCESS_URL or request.host_url.rstrip("/")) + "/gift/success?session_id={CHECKOUT_SESSION_ID}"
    cancel_url  = request.host_url.rstrip("/") + "/gift"

    stripe_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "cad",
                "product_data": {
                    "name": f"Gift Certificate — {pkg['label']}",
                    "description": f"{package_label} · Recipient: {recipient_name or 'To be determined'}",
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
            "gift_session_type":  session_type,
            "custom_base":        custom_base,
            "certificate_style":  certificate_style,
            "gift_addons":        ",".join(addon["id"] for addon in addons),
            "package_label":      package_label[:450],
            "purchaser_name":     purchaser_name,
            "recipient_name":     recipient_name,
            "recipient_email":    recipient_email,
        },
    )
    return redirect(stripe_session.url)


@gift_referral_bp.route("/gift/success")
def gift_success():
    stripe_session_id = request.args.get("session_id", "")
    form = session.pop("gift_form", None)

    is_mock = stripe_session_id.startswith("TEST_MOCK_")

    if not is_mock and not TEST_MODE:
        # --- Verify real Stripe payment BEFORE creating any record or sending email ---
        try:
            stripe_sess = stripe.checkout.Session.retrieve(stripe_session_id)
            if stripe_sess.payment_status != "paid":
                return render_template(
                    "gift/gift_success.html",
                    error="Payment not confirmed. Please contact us if you were charged.",
                )
            if not form:
                meta = stripe_sess.metadata or {}
                form = {
                    "purchaser_name":    meta.get("purchaser_name", ""),
                    "purchaser_email":   stripe_sess.customer_email or "",
                    "recipient_name":    meta.get("recipient_name", ""),
                    "recipient_email":   meta.get("recipient_email", ""),
                    "personal_message":  "",
                    "session_type":      meta.get("gift_session_type", "custom"),
                    "custom_base":       meta.get("custom_base", ""),
                    "certificate_style": meta.get("certificate_style", "signature"),
                    "package_label":     meta.get("package_label", ""),
                    "addons_json":       json.dumps(
                        _selected_addons([
                            item for item in meta.get("gift_addons", "").split(",") if item
                        ]),
                        separators=(",", ":"),
                    ),
                    "amount":          (stripe_sess.amount_total or 0) / 100 / 1.05,
                    "amount_with_gst": (stripe_sess.amount_total or 0) / 100,
                    "photo_url":       "",
                }
        except stripe.StripeError as e:
            return render_template("gift/gift_success.html", error=f"Payment verification failed: {e}")

    if not form:
        return render_template(
            "gift/gift_success.html",
            error="Session expired. If you completed payment, please email us with your receipt.",
        )

    # Idempotent: don't create a second cert if Stripe redirects twice
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
            custom_base       = form.get("custom_base", ""),
            certificate_style = form.get("certificate_style", "signature"),
            package_label     = form.get("package_label", ""),
            addons_json       = form.get("addons_json", "[]"),
            photo_url         = form.get("photo_url", ""),
            payment_method    = "stripe",
            payment_status    = "paid",
            paid_amount       = form["amount_with_gst"],
            stripe_session_id = stripe_session_id,
        )
        cert = db.get_gift_certificate(code)

    # Generate PDF, then send emails — only reaches here after payment verified
    pdf_path = None
    try:
        pdf_path = save_gift_pdf(cert)
        db.update_gift_pdf(code, pdf_path)
    except Exception as e:
        print(f"[PDF ERROR] {e}")

    send_gift_purchaser_email(cert, pdf_path=pdf_path)
    if cert.get("recipient_email"):
        send_gift_recipient_email(cert)

    referral = _referral_for(cert.get("purchaser_email", ""), cert.get("purchaser_name", ""))
    return render_template("gift/gift_success.html", cert=cert, pdf_path=pdf_path,
                           booking_url=BOOKING_URL, **referral)


@gift_referral_bp.route("/gift/pending/<code>")
def gift_pending(code):
    code = code.strip().upper()
    cert = db.get_gift_certificate(code)
    if not cert:
        abort(404)
    if cert.get("status") == "active":
        referral = _referral_for(cert.get("purchaser_email", ""), cert.get("purchaser_name", ""))
        return render_template("gift/gift_success.html", cert=cert, pdf_path=cert.get("pdf_path"),
                               booking_url=BOOKING_URL, **referral)
    bank_message = f"Gift certificate {code}"
    return render_template(
        "gift/gift_pending.html",
        cert=cert,
        interac_email=INTERAC_EMAIL,
        bank_message=bank_message,
        booking_url=BOOKING_URL,
    )


@gift_referral_bp.route("/gift/certificate/<code>")
def download_certificate(code):
    cert = db.get_gift_certificate(code)
    if not cert:
        abort(404)
    if cert.get("status") not in ("active", "redeemed"):
        abort(403)
    if cert.get("pdf_path") and os.path.exists(cert["pdf_path"]):
        return send_file(cert["pdf_path"], mimetype="application/pdf",
                         as_attachment=True, download_name=f"GiftCertificate_{code}.pdf")
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
    owner_email = request.args.get("email", "").strip().lower()
    owner_name  = request.args.get("name", "Photographer Friend").strip()

    if not owner_email:
        return render_template("referral/referral_share.html",
                               error="Missing owner email.", code=None)

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


@gift_referral_bp.route("/refer", methods=["GET", "POST"])
def refer_hub():
    """Self-serve referral page: anyone can get their own code and share it."""
    name  = (request.form.get("name") or request.args.get("name") or "").strip()
    email = (request.form.get("email") or request.args.get("email") or "").strip().lower()
    ref   = None
    if email and "@" in email and "." in email.split("@")[-1]:
        ref = db.get_referral_code_by_owner(email)
        if not ref:
            code = db.create_referral_code(email, name or "Friend")
            ref  = db.get_referral_code(code)
    referral_url = share_msg = credits_url = None
    if ref:
        referral_url = f"{BOOKING_URL}/referral/{ref['code']}"
        share_msg = (
            f"Hey! I love Pashynska Photography in Calgary. Use my code {ref['code']} "
            f"for ${int(ref['discount_for_friend'])} off your first session - book here: {referral_url}"
        )
        credits_url = f"{BOOKING_URL}/my-credits?email={email}&token={_make_credits_token(email)}"
    return render_template(
        "referral/referral_hub.html",
        booking_url=BOOKING_URL, ref=ref, name=name, email=email,
        referral_url=referral_url, share_msg=share_msg, credits_url=credits_url,
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
        return jsonify(db.validate_referral_code(code, email or None))

    g = db.validate_gift_certificate(code, session_type or None)
    if g["valid"]:
        return jsonify(g)
    r = db.validate_referral_code(code, email or None)
    return jsonify(r)


# ---------------------------------------------------------------------------
# Share referral code by email
# ---------------------------------------------------------------------------

@gift_referral_bp.route("/referral/send-welcome", methods=["POST"])
def referral_send_welcome():
    data          = request.get_json(silent=True) or {}
    code          = (data.get("code") or "").strip().upper()
    referee_email = (data.get("referee_email") or "").strip().lower()
    referee_name  = (data.get("referee_name") or "Friend").strip()

    if not code:
        return jsonify({"success": False, "error": "No referral code provided"}), 400
    if not referee_email or "@" not in referee_email:
        return jsonify({"success": False, "error": "Please provide a valid friend email"}), 400

    ref = db.get_referral_code(code)
    if not ref or ref["status"] != "active":
        return jsonify({"success": False, "error": "Invalid or expired referral code"}), 400

    validation = db.validate_referral_code(code, referee_email)
    if not validation.get("valid"):
        return jsonify({"success": False, "error": validation.get("error", "This code cannot be used")}), 400

    friend_ok = send_referral_welcome_email(
        referee_email=referee_email,
        referee_name=referee_name,
        owner_name=ref["owner_name"],
        discount=float(ref["discount_for_friend"]),
        code=code,
    )
    owner_ok = send_referral_invite_notification_email(
        owner_email=ref["owner_email"],
        owner_name=ref["owner_name"],
        friend_name=referee_name,
        friend_email=referee_email,
        code=code,
    )
    return jsonify({
        "success": bool(friend_ok),
        "friend_email_sent": friend_ok,
        "owner_notification_sent": owner_ok,
    })


# ---------------------------------------------------------------------------
# Webhook: called by main app when a friend's payment is confirmed
# ---------------------------------------------------------------------------

@gift_referral_bp.route("/referral/payment-confirmed/<int:booking_id>", methods=["POST"])
def referral_payment_confirmed(booking_id):
    use = db.confirm_referral_payment(booking_id)
    if not use:
        return jsonify({"triggered": False, "reason": "No pending referral use found"})
    if use.get("self_use"):
        return jsonify({"triggered": False, "reason": "Self-use referral, no reward"})

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
        "triggered":   True,
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

    if not TEST_MODE and not _verify_credits_token(email, token):
        return render_template("referral/my_credits.html",
                               error="Invalid or expired link. Please use the link from your email.",
                               credits=None)

    credits = db.get_credit_balance(email)
    history = db.get_credit_history(email) if credits else []
    ref     = db.get_referral_code_by_owner(email)

    referral_url = f"{BOOKING_URL}/referral/{ref['code']}" if ref else None
    share_msg    = (
        f"I just booked a photo session with Pashynska Photography! "
        f"Use my code {ref['code']} and get ${ref['discount_for_friend']:.0f} off your first session 📸  "
        f"{referral_url}"
    ) if ref else None

    return render_template(
        "referral/my_credits.html",
        email=email, credits=credits, history=history,
        ref=ref, referral_url=referral_url, share_msg=share_msg,
        booking_url=BOOKING_URL,
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

_ADMIN_CSS = """
body{font-family:sans-serif;margin:40px;background:#FAF7F2;color:#2C2C2C;}
h1{color:#C4973A;}h2{color:#555;}
table{border-collapse:collapse;width:100%;margin-bottom:40px;background:#fff;}
th{background:#C4973A;color:#fff;padding:10px 12px;text-align:left;font-size:13px;}
td{padding:8px 12px;border-bottom:1px solid #E8D5A3;font-size:13px;}
tr:hover td{background:#FEF9F2;}
.active{color:#2a7a2a;font-weight:bold;}
.pending_payment{color:#b7791f;font-weight:bold;}
.redeemed{color:#888;}.expired{color:#c00;}
a{color:#C4973A;}
.nav{margin-bottom:24px;}.nav a{margin-right:16px;font-size:14px;}
.btn{display:inline-block;padding:6px 14px;border-radius:4px;font-size:12px;
     font-weight:bold;text-decoration:none;cursor:pointer;border:none;}
.btn-confirm{background:#2a7a2a;color:#fff;}
.btn-confirm:hover{background:#1e5c1e;}
"""

_ADMIN_HEADER = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_ADMIN_CSS}</style></head><body>
<h1>Pashynska Photography — Gift &amp; Referral Admin</h1>
<div class="nav">
  <a href="/admin/gifts">Gift Certificates</a>
  <a href="/admin/gift-pending">Pending e-Transfer</a>
  <a href="/admin/referrals">Referral Codes</a>
  <a href="/gift">Gift Landing</a>
</div>"""

_ADMIN_FOOTER = "</body></html>"


@gift_referral_bp.route("/admin/gifts")
def admin_gifts():
    certs = db.list_gift_certificates()
    rows  = ""
    for c in certs:
        status_cls = c["status"] if c["status"] in ("active", "redeemed", "expired", "pending_payment") else ""
        rows += (
            f"<tr><td>{c['id']}</td>"
            f"<td><code>{c['code']}</code></td>"
            f"<td>{c['purchaser_name']}<br><small>{c['purchaser_email']}</small></td>"
            f"<td>{c.get('recipient_name','') or '—'}</td>"
            f"<td>{c.get('package_label') or c.get('session_type','') or 'custom'}"
            f"<br><small>{c.get('certificate_style') or 'signature'} · {c.get('payment_method') or 'stripe'}</small></td>"
            f"<td>${c['amount_with_gst']:.2f}</td>"
            f"<td class='{status_cls}'>{c['status']}</td>"
            f"<td>{c['created_at'][:10]}</td>"
            f"<td>{c.get('expires_at','') or '—'}</td>"
            f"<td>{'✓' if c.get('pdf_sent') else '—'}</td>"
            f"<td><a href='/gift/certificate/{c['code']}'>PDF</a></td></tr>"
        )
    return (
        _ADMIN_HEADER
        + f"<h2>Gift Certificates ({len(certs)})</h2>"
        + "<table><thead><tr>"
        + "<th>#</th><th>Code</th><th>Purchaser</th><th>Recipient</th>"
        + "<th>Session</th><th>Amount</th><th>Status</th>"
        + "<th>Created</th><th>Expires</th><th>PDF Sent</th><th>Download</th>"
        + "</tr></thead><tbody>"
        + (rows or "<tr><td colspan='11' style='color:#aaa;text-align:center'>No certificates yet</td></tr>")
        + "</tbody></table>"
        + _ADMIN_FOOTER
    )


@gift_referral_bp.route("/admin/gift-pending")
def admin_gift_pending():
    """
    Lists all e-Transfer gift certificates awaiting payment confirmation.
    Each row has a 'Confirm Payment' button that activates the cert and triggers emails.
    """
    all_certs = db.list_gift_certificates()
    pending   = [c for c in all_certs if c["status"] == "pending_payment"]

    rows = ""
    for c in pending:
        confirm_url  = url_for("gift_referral.admin_confirm_gift_payment", code=c["code"])
        cert_code    = c["code"]
        onclick_msg  = f"Confirm e-Transfer payment received for {cert_code}?"
        rows += (
            f"<tr>"
            f"<td>{c['id']}</td>"
            f"<td><code>{cert_code}</code></td>"
            f"<td>{c['purchaser_name']}<br><small>{c['purchaser_email']}</small></td>"
            f"<td>{c.get('recipient_name','') or '—'}<br><small>{c.get('recipient_email','') or ''}</small></td>"
            f"<td>{c.get('package_label') or c.get('session_type','') or 'custom'}</td>"
            f"<td><strong>${c['amount_with_gst']:.2f}</strong></td>"
            f"<td>{c['created_at'][:16]}</td>"
            f"<td>"
            f"<form method='POST' action='{confirm_url}' style='display:inline'>"
            f"<button type='submit' class='btn btn-confirm'"
            f" onclick=\"return confirm('{onclick_msg}')\">"
            f"✓ Confirm Payment</button>"
            f"</form>"
            f"</td>"
            f"</tr>"
        )

    return (
        _ADMIN_HEADER
        + f"<h2>Pending e-Transfer Payments ({len(pending)})</h2>"
        + "<p style='color:#666;font-size:13px;'>Click <strong>Confirm Payment</strong> "
        + f"once you see the e-Transfer in your bank. "
        + f"This activates the certificate and sends the PDF to the purchaser.</p>"
        + "<table><thead><tr>"
        + "<th>#</th><th>Code</th><th>Purchaser</th><th>Recipient</th>"
        + "<th>Package</th><th>Amount</th><th>Created</th><th>Action</th>"
        + "</tr></thead><tbody>"
        + (rows or "<tr><td colspan='8' style='color:#2a7a2a;text-align:center;padding:20px'>"
           "✓ No pending payments</td></tr>")
        + "</tbody></table>"
        + _ADMIN_FOOTER
    )


@gift_referral_bp.route("/admin/gift-pending/<code>/confirm", methods=["POST"])
def admin_confirm_gift_payment(code):
    """
    Confirms e-Transfer payment for a gift certificate.
    Activates the cert, generates PDF, and sends emails to purchaser (and recipient if provided).
    """
    code = code.strip().upper()
    cert = db.get_gift_certificate(code)

    if not cert:
        return _ADMIN_HEADER + "<p style='color:red'>Certificate not found.</p>" + _ADMIN_FOOTER, 404

    if cert["status"] != "pending_payment":
        return (
            _ADMIN_HEADER
            + f"<p>Certificate {code} is already <strong>{cert['status']}</strong>. "
            + f"<a href='/admin/gift-pending'>Back</a></p>"
            + _ADMIN_FOOTER
        )

    # Activate the certificate
    confirmed = db.mark_gift_payment_confirmed(code, cert["amount_with_gst"])
    if not confirmed:
        return _ADMIN_HEADER + "<p style='color:red'>Could not confirm payment — please check the database.</p>" + _ADMIN_FOOTER, 500

    # Reload cert with updated status
    cert = db.get_gift_certificate(code)

    # Generate PDF
    pdf_path = None
    try:
        pdf_path = save_gift_pdf(cert)
        db.update_gift_pdf(code, pdf_path)
        cert = db.get_gift_certificate(code)
    except Exception as exc:
        print(f"[GIFT PDF ERROR] {code}: {exc}")

    # Send emails now that payment is confirmed
    purchaser_ok = send_gift_purchaser_email(cert, pdf_path=pdf_path)
    recipient_ok = False
    if cert.get("recipient_email"):
        recipient_ok = send_gift_recipient_email(cert)

    result_html = (
        f"<h2>✓ Payment confirmed for {code}</h2>"
        f"<p><strong>Certificate status:</strong> active</p>"
        f"<p><strong>Purchaser email sent:</strong> {'✓' if purchaser_ok else '✗ (check logs)'}</p>"
        f"<p><strong>Recipient email sent:</strong> "
        + ("✓" if recipient_ok else ("✗ (no recipient email)" if not cert.get("recipient_email") else "✗ (check logs)"))
        + "</p>"
        f"<p><a href='/admin/gift-pending'>← Back to pending</a> · "
        f"<a href='/admin/gifts'>All certificates</a></p>"
    )
    return _ADMIN_HEADER + result_html + _ADMIN_FOOTER


@gift_referral_bp.route("/admin/referrals")
def admin_referrals():
    codes   = db.list_referral_codes()
    uses    = db.list_referral_uses()
    credits = db.list_credit_balances()

    credit_map = {c["owner_email"]: c for c in credits}

    code_rows = ""
    for r in codes:
        cr     = credit_map.get(r["owner_email"])
        bal    = f"${cr['balance']:.0f}" if cr else "—"
        earned = f"${cr['total_earned']:.0f}" if cr else "—"
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

    return (
        _ADMIN_HEADER
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
        + _ADMIN_FOOTER
    )
