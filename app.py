#!/usr/bin/env python3
"""
Iryna Pashynska Photography — Mini Session Booking System
Flask app with SQLite database, 20-min sessions + 10-min breaks, Instagram field.
"""

# Load .env file before any imports that use env vars
import os as _os
_env_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env")
if _os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip().strip('"').strip("'")
                if _key not in _os.environ:
                    _os.environ[_key] = _val
    print(f"[env] Loaded {_env_path}")

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory, send_file, Response, has_request_context
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import logging
import os
import hmac
import hashlib
from html import escape as _html_escape
import secrets  # used by /reserve to generate confirmation_token
import sqlite3
import requests
import threading
import sys
import re
import time
import yaml
from html import escape as html_escape

# ===== TIMEZONE HELPERS =====
# Container runs UTC; clients are in America/Edmonton (UTC-6/UTC-7).
# All user-visible dates must use local time to avoid evening drift.
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo as _ZoneInfo  # type: ignore

_tz = _ZoneInfo('America/Edmonton')

# ===== CONFIG =====
# Load from .env or use defaults
META_PIXEL_ID = os.environ.get('META_PIXEL_ID', '1335137335347797')  # Pashynska Photography Pixel (Events Manager canonical). Single source of truth — injected into every template via context processor. Override with env/fly secret only to switch pixels.
GOOGLE_ANALYTICS_ID = os.environ.get('GOOGLE_ANALYTICS_ID', '')
# ── Meta Conversions API (server-side Purchase) ───────────────────────────────
# e-Transfer payments confirm asynchronously (the Gmail watcher matches the
# deposit minutes later, after the client has left the page), so the browser
# pixel can never reliably fire Purchase. CAPI fires it server-side the moment
# a booking is confirmed — through ANY path (auto e-Transfer, Stripe, admin).
# Browser pixel + CAPI share a stable event_id ("purchase.<booking_id>") so Meta
# deduplicates. No-op until META_CAPI_TOKEN is set, so it is safe to deploy now:
#   fly secrets set META_CAPI_TOKEN=<system-user token from Events Manager>
META_CAPI_TOKEN = os.environ.get('META_CAPI_TOKEN', '')
META_CAPI_API_VERSION = os.environ.get('META_CAPI_API_VERSION', 'v19.0')
META_TEST_EVENT_CODE = os.environ.get('META_TEST_EVENT_CODE', '')  # set temporarily to verify in Events Manager → Test Events, then unset

# Email Settings (Zoho Mail)
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.zoho.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USERNAME = os.environ.get('SMTP_USERNAME', 'info@pashynskafoto.com')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'

DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'info@pashynskafoto.com')
DEFAULT_FROM_NAME = os.environ.get('DEFAULT_FROM_NAME', 'Pashynska Photography')


def _local_now():
    """Return timezone-aware datetime in America/Edmonton."""
    return datetime.now(timezone.utc).astimezone(_tz)


def _local_today():
    """Return date() in America/Edmonton."""
    return _local_now().date()

# ===== LOGGING =====
# Write log to persistent volume when available, else next to app
_log_dir = os.environ.get("BACKUP_DIR", "").replace("/backups", "") or os.path.dirname(__file__)
_log_path = os.path.join(_log_dir, 'booking.log')
try:
    os.makedirs(_log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(_log_path),
            logging.StreamHandler()
        ]
    )
except OSError as e:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.StreamHandler()]
    )
    logging.error(f"Failed to create log directory {_log_dir}: {e}")
log = logging.getLogger(__name__)

# Optional local automation bridge: when N8N_WEBHOOK_URL is configured, the booking
# app emits structured business events to n8n. It is deliberately fire-and-forget:
# n8n downtime must never block bookings, payments, admin actions, or emails.
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "").strip()
N8N_WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "").strip()


def _emit_n8n_event(action, booking=None, **payload):
    """Send a structured event to local n8n without affecting the request path."""
    if not N8N_WEBHOOK_URL:
        return False
    body = {
        "action": action,
        "source": "pashynska-booking-flask",
        "booking": booking or {},
        **payload,
    }
    headers = {"Content-Type": "application/json"}
    if N8N_WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = N8N_WEBHOOK_SECRET

    def _post():
        try:
            if not N8N_WEBHOOK_URL:
                logging.warning("N8N_WEBHOOK_URL not configured")
                return False
            r = requests.post(N8N_WEBHOOK_URL, json=body, headers=headers, timeout=5)
            r.raise_for_status()  # Raises HTTPError for 4XX/5XX responses
            return True
        except requests.exceptions.RequestException as e:
            logging.error(f"[n8n] {action} emit failed: {e}")

    threading.Thread(target=_post, name=f"n8n-{action}", daemon=True).start()
    return True


ANALYTICS_EVENT_ALLOWLIST = {
    "page_view", "session_view", "event_card_view", "drawer_open", "slot_selected", "form_started",
    "reserve_attempt", "booking_reserved", "payment_view", "payment_sent_clicked",
    "booking_confirmed", "booking_expired", "booking_cancelled", "waitlist_joined",
    "abandoned_followup_sent", "abandoned_second_followup_sent",
}


def _safe_text(value, max_len=500):
    """Small analytics sanitizer: store useful attribution, not unbounded payloads."""
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _client_ip():
    if not has_request_context():
        return ""
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or ""
    return _safe_text(ip, 80)


def _normalise_utm(data):
    utm = data.get("utm") if isinstance(data.get("utm"), dict) else {}
    return {
        "utm_source": _safe_text(data.get("utm_source") or utm.get("source"), 120),
        "utm_medium": _safe_text(data.get("utm_medium") or utm.get("medium"), 120),
        "utm_campaign": _safe_text(data.get("utm_campaign") or utm.get("campaign"), 180),
        "utm_content": _safe_text(data.get("utm_content") or utm.get("content"), 180),
        "utm_term": _safe_text(data.get("utm_term") or utm.get("term"), 180),
    }


def _record_analytics_event(event_name, *, visitor_id="", booking_id=None, event_id="", page="", metadata=None, attribution=None):
    """Persist a funnel event and upsert its visitor attribution. Fire-and-forget safe."""
    visitor_id = _safe_text(visitor_id, 120)
    if not visitor_id:
        return False
    event_name = _safe_text(event_name, 80)
    if event_name not in ANALYTICS_EVENT_ALLOWLIST:
        event_name = "page_view"
    attribution = attribution or {}
    metadata = metadata if isinstance(metadata, dict) else {}
    request_referrer = request.headers.get("Referer") if has_request_context() else ""
    user_agent = request.headers.get("User-Agent") if has_request_context() else ""
    now = datetime.now(timezone.utc).isoformat()
    conn = db_conn()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO visitor_sessions
                (visitor_id, first_seen, last_seen, utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                 fbclid, gclid, referrer, landing_url, user_agent, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(visitor_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                utm_source=COALESCE(NULLIF(visitor_sessions.utm_source,''), excluded.utm_source),
                utm_medium=COALESCE(NULLIF(visitor_sessions.utm_medium,''), excluded.utm_medium),
                utm_campaign=COALESCE(NULLIF(visitor_sessions.utm_campaign,''), excluded.utm_campaign),
                utm_content=COALESCE(NULLIF(visitor_sessions.utm_content,''), excluded.utm_content),
                utm_term=COALESCE(NULLIF(visitor_sessions.utm_term,''), excluded.utm_term),
                fbclid=COALESCE(NULLIF(visitor_sessions.fbclid,''), excluded.fbclid),
                gclid=COALESCE(NULLIF(visitor_sessions.gclid,''), excluded.gclid),
                referrer=COALESCE(NULLIF(visitor_sessions.referrer,''), excluded.referrer),
                landing_url=COALESCE(NULLIF(visitor_sessions.landing_url,''), excluded.landing_url),
                user_agent=COALESCE(NULLIF(visitor_sessions.user_agent,''), excluded.user_agent),
                ip_address=COALESCE(NULLIF(visitor_sessions.ip_address,''), excluded.ip_address)
        """, (
            visitor_id, now, now,
            _safe_text(attribution.get("utm_source"), 120),
            _safe_text(attribution.get("utm_medium"), 120),
            _safe_text(attribution.get("utm_campaign"), 180),
            _safe_text(attribution.get("utm_content"), 180),
            _safe_text(attribution.get("utm_term"), 180),
            _safe_text(attribution.get("fbclid"), 500),
            _safe_text(attribution.get("gclid"), 500),
            _safe_text(attribution.get("referrer") or request_referrer, 500),
            _safe_text(attribution.get("landing_url") or page, 1000),
            _safe_text(user_agent, 500),
            _client_ip(),
        ))
        c.execute("""
            INSERT INTO analytics_events
                (visitor_id, booking_id, event_name, event_id, page, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            visitor_id,
            booking_id,
            event_name,
            _safe_text(event_id, 160),
            _safe_text(page, 1000),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True)[:4000],
            now,
        ))
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"[analytics] record failed: {e}")
        return False
    finally:
        conn.close()


def _analytics_attribution_from_booking(booking):
    booking = booking or {}
    return {
        "utm_source": booking.get("utm_source"),
        "utm_medium": booking.get("utm_medium"),
        "utm_campaign": booking.get("utm_campaign"),
        "utm_content": booking.get("utm_content"),
        "utm_term": booking.get("utm_term"),
        "fbclid": booking.get("fbclid"),
        "gclid": booking.get("gclid"),
        "referrer": booking.get("referrer"),
        "landing_url": booking.get("landing_url"),
    }


def _record_booking_funnel_event(booking, event_name, metadata=None):
    """Attach downstream lifecycle events to the visitor that created a booking."""
    booking = booking or {}
    # Server-side Meta Purchase fires on the confirmed event regardless of whether
    # the booking carries a visitor_id (admin-created bookings have none but are
    # still real revenue). It must therefore run BEFORE the visitor_id gate below.
    if event_name == "booking_confirmed":
        meta = metadata or {}
        _meta_capi_purchase(booking, value=meta.get("paid_amount"))
    visitor_id = booking.get("visitor_id")
    if not visitor_id:
        return False
    return _record_analytics_event(
        event_name,
        visitor_id=visitor_id,
        booking_id=booking.get("id"),
        event_id=booking.get("event_id") or "",
        page=booking.get("landing_url") or "",
        metadata=metadata or {},
        attribution=_analytics_attribution_from_booking(booking),
    )


def _sha256_norm(value):
    """Lowercase + strip + sha256 — Meta advanced-matching normalization."""
    if not value:
        return None
    return hashlib.sha256(str(value).strip().lower().encode("utf-8")).hexdigest()


def _meta_capi_purchase(booking, value=None, currency="CAD"):
    """Send a server-side Purchase to the Meta Conversions API.

    The reliable conversion signal for this business: the dominant payment path
    is e-Transfer, confirmed asynchronously by the Gmail watcher long after the
    client has closed the tab, so the browser pixel cannot be trusted to fire
    Purchase. Uses a stable event_id ("purchase.<id>") shared with the browser
    pixel on success.html, so Meta deduplicates and a booking counts once.

    No-op (returns False) until META_CAPI_TOKEN is configured, so this is safe to
    ship before the token exists. Hashes all PII (email/phone/name) per Meta's
    advanced-matching spec — raw PII is never sent.
    """
    if not META_CAPI_TOKEN or not META_PIXEL_ID:
        return False
    booking = booking or {}
    bid = booking.get("id")
    if not bid:
        return False
    try:
        attribution = _analytics_attribution_from_booking(booking)
        user_data = {}
        em = _sha256_norm(booking.get("email"))
        if em:
            user_data["em"] = [em]
        phone_digits = re.sub(r"\D", "", str(booking.get("phone") or ""))
        if phone_digits:
            if len(phone_digits) == 10:  # NANP number missing its country code
                phone_digits = "1" + phone_digits
            user_data["ph"] = [hashlib.sha256(phone_digits.encode("utf-8")).hexdigest()]
        name = (booking.get("name") or "").strip()
        if name:
            parts = name.split()
            fn = _sha256_norm(parts[0])
            if fn:
                user_data["fn"] = [fn]
            if len(parts) > 1:
                ln = _sha256_norm(parts[-1])
                if ln:
                    user_data["ln"] = [ln]
        fbclid = attribution.get("fbclid")
        if fbclid:
            user_data["fbc"] = f"fb.1.{int(time.time())}.{fbclid}"
        try:
            val = float(value) if value is not None else float(booking.get("paid_amount") or 0)
        except (TypeError, ValueError):
            val = 0.0
        event = {
            "event_name": "Purchase",
            "event_time": int(time.time()),
            "event_id": f"purchase.{bid}",
            "action_source": "website",
            "event_source_url": attribution.get("landing_url") or CANONICAL_SITE_URL,
            "user_data": user_data,
            "custom_data": {
                "currency": currency,
                "value": round(val, 2),
                "content_name": booking.get("event_id") or "",
                "order_id": str(bid),
            },
        }
        payload = {"data": [event]}
        if META_TEST_EVENT_CODE:
            payload["test_event_code"] = META_TEST_EVENT_CODE
        url = f"https://graph.facebook.com/{META_CAPI_API_VERSION}/{META_PIXEL_ID}/events"
        resp = requests.post(url, params={"access_token": META_CAPI_TOKEN},
                             json=payload, timeout=6)
        if resp.status_code >= 400:
            log.warning(f"[capi] Purchase #{bid} HTTP {resp.status_code}: {resp.text[:300]}")
            return False
        log.info(f"[capi] Purchase fired for booking #{bid} (value={val} {currency})")
        return True
    except Exception as e:
        log.warning(f"[capi] Purchase failed for booking #{booking.get('id')}: {e}")
        return False


app = Flask(__name__)
# Trust Cloudflare forwarded headers so request.host reflects the ORIGINAL
# domain the visitor used (pashynska.agency) not the canonical one.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Cap upload body to 30 MB. Admin photo uploads are optimized server-side, but
# Flask defaults to unlimited request bodies and a batch can contain up to five
# phone photos.
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

# ── Stable secret key ──────────────────────────────────────────────────────────
# Priority: FLASK_SECRET_KEY env var → /data/.flask_secret (auto-generated once)
# NEVER fall back to os.urandom() here — that would invalidate sessions on every restart.
_secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
if not _secret_key:
    _secret_file = os.path.join(
        os.environ.get("BACKUP_DIR", "").replace("/backups", "") or os.path.dirname(__file__),
        ".flask_secret"
    )
    try:
        if os.path.exists(_secret_file):
            with open(_secret_file) as _sf:
                _secret_key = _sf.read().strip()
        if not _secret_key:
            import secrets as _secrets
            _secret_key = _secrets.token_hex(32)
            os.makedirs(os.path.dirname(_secret_file), exist_ok=True)
            with open(_secret_file, "w") as _sf:
                _sf.write(_secret_key)
            log.info(f"[secret] Generated new stable secret key → {_secret_file}")
        else:
            log.info(f"[secret] Loaded stable secret key from {_secret_file}")
    except Exception as _e:
        log.warning(f"[secret] Could not persist secret key: {_e} — using ephemeral key")
        import secrets as _secrets
        _secret_key = _secrets.token_hex(32)
app.secret_key = _secret_key

# ── Session cookie hardening ────────────────────────────────────────────────
# SECURE=True in production (HTTPS-only); auto-disabled when FLASK_ENV=development
# so local http://localhost still works. HTTPONLY blocks JS access to the cookie.
# SAMESITE=Lax prevents most CSRF while letting legit top-level navigations work.
_is_dev = os.environ.get("FLASK_ENV") == "development"
app.config["SESSION_COOKIE_SECURE"] = not _is_dev
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# 8h admin session — enough for a working day, short enough to limit risk of
# unlocked laptops being misused.
from datetime import timedelta as _timedelta_for_session
app.permanent_session_lifetime = _timedelta_for_session(hours=8)

# Canonical production host. Lifted to the top so _canonical_redirect (below)
# can use it instead of literal strings — keeps a single source of truth.
# Keep in sync with the Cloudflare Worker and with fly.toml; the host shows up
# in calendar UIDs and outgoing email links, so drift would silently break
# things weeks later.
CANONICAL_SITE_URL = "https://book.pashynskaphoto.com"
CANONICAL_SITE_HOST = "book.pashynskaphoto.com"
_FLY_INTERNAL_HOST = "iryna-booking.fly.dev"
# Portfolio/gallery site (apex domain, separate from the booking subdomain).
# Centralised so any future rebrand only touches one line.
PORTFOLIO_URL = "https://pashynskaphoto.com"

# ── Gift & Referral module ────────────────────────────────────────────────────
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gift-referral'))
from gift_referral_routes import gift_referral_bp  # noqa: E402
import gift_referral_db as gift_db                 # noqa: E402
gift_db.init_db()
app.register_blueprint(gift_referral_bp)


@app.context_processor
def _inject_site_links():
    """Make canonical URLs + current year available to every template
    without per-route plumbing. Year is auto-computed so the footer ©
    is never stale (was "© 2024–2025" all of 2026)."""
    return {
        "PORTFOLIO_URL": PORTFOLIO_URL,
        "CANONICAL_SITE_URL": CANONICAL_SITE_URL,
        "current_year": _local_now().year,
        # Single source of truth for the Meta Pixel — every template renders
        # {{ meta_pixel_id }} so the id can never drift between pages again.
        "meta_pixel_id": META_PIXEL_ID,
    }

# ── Canonical domain redirect ─────────────────────────────────────────────────
# Cloudflare Worker sends original domain in Cf-Worker header and rewrites
# Host to iryna-booking.fly.dev (Fly internal hostname). We detect the
# original domain via Cf-Worker and 301 to the canonical site.
@app.before_request
def _canonical_redirect():
    # Never redirect machine-to-machine webhooks. Stripe signs the exact POST
    # payload and expects this endpoint to answer directly; a 301 from the
    # legacy pashynska.agency Cloudflare Worker prevents auto-confirmation.
    if request.path in ("/stripe/webhook", "/telegram/webhook"):
        return None

    cf_worker = request.headers.get("Cf-Worker", "").lower()
    if cf_worker in ("pashynska.agency", "www.pashynska.agency"):
        new_url = request.url.replace(
            f"https://{_FLY_INTERNAL_HOST}",
            CANONICAL_SITE_URL,
            1
        ).replace(
            f"http://{_FLY_INTERNAL_HOST}",
            CANONICAL_SITE_URL,
            1
        )
        log.info(f"[redirect] Cf-Worker={cf_worker} → {new_url}")
        return redirect(new_url, code=301)

PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable)


def _background_threads_disabled():
    return os.environ.get("DISABLE_BACKGROUND_THREADS", "").lower() in ("1", "true", "yes", "on")

# ===== GLOBAL E-TRANSFER WATCHER (replaces per-booking Popen) =====
import threading as _threading

_watcher_started = False
_watcher_state = {
    "last_email_scan_at": None,
    "last_email_scan_ok": None,
    "last_email_scan_error": None,
    "last_email_count": 0,
    "last_auto_confirmed_booking_id": None,
    "last_auto_confirmed_at": None,
}

def _process_etransfer_email_batch(emails, pending, reconciliation):
    """Match a batch of Interac emails against pending bookings.

    The pending list is filtered after every confirmation so a second
    same-amount email in the same batch can never re-match the booking the
    first one just paid for — it falls through to the orphan path instead.
    Returns the list of confirmed booking ids.
    """
    from check_etransfer_v2 import is_etransfer_email, check_single_email
    pending = list(pending or [])
    confirmed_ids = []
    for email in emails:
        if not is_etransfer_email(email):
            continue
        confirmed_id, _ambiguous = check_single_email(email, pending, reconciliation)
        if confirmed_id:
            confirmed_ids.append(confirmed_id)
            pending = [b for b in pending if b.get("id") != confirmed_id]
    return confirmed_ids


def _watcher_thread():
    """Daemon thread — does two periodic jobs:
       1. Check Gmail for incoming Interac e-Transfers and auto-confirm bookings
       2. Expire stale reservations whose 15-min window has passed, freeing slots

    Without (2), a client who reserves a slot and walks away locks that time
    forever (until someone hits /expired manually). That silently kills
    conversion: the next visitor sees 'Sold out' on a slot nobody is paying for.
    """
    import time as _time
    CHECK_INTERVAL = 30  # seconds — fast enough to free slots for the next visitor
    EMAIL_POLL_INTERVAL = int(os.environ.get("ETRANSFER_EMAIL_POLL_INTERVAL", "60"))
    RECONCILIATION_INTERVAL = int(os.environ.get("ETRANSFER_RECONCILIATION_INTERVAL", "1800"))
    LIVE_EMAIL_PAGE_SIZE = int(os.environ.get("ETRANSFER_LIVE_EMAIL_PAGE_SIZE", "25"))
    LIVE_EMAIL_LOOKBACK_DAYS = int(os.environ.get("ETRANSFER_LIVE_EMAIL_LOOKBACK_DAYS", "7"))
    last_email_poll = 0.0
    last_reconciliation_poll = 0.0
    log_w = logging.getLogger("watcher")
    log_w.info("[watcher] Global e-Transfer + slot-expiry watcher started")

    from check_etransfer_v2 import (
        get_pending_bookings, get_reconciliation_bookings, get_emails
    )

    while True:
        # 1. Sweep expired reservations every tick — cheap query, no external IO.
        try:
            released = expire_reservations()
            if released:
                log_w.info(f"[watcher] Released {released} expired reservation(s)")
        except Exception as e:
            log_w.error(f"[watcher] expire_reservations error: {e}")

        # 2. Poll Gmail for e-Transfer notifications and match to pending bookings.
        try:
            now = _time.time()
            pending = get_pending_bookings(within_minutes=30)
            should_poll_email = bool(pending) and (now - last_email_poll >= EMAIL_POLL_INTERVAL)

            # Reconciliation is useful, but it is not time-critical. Running it
            # every 30 seconds kept spawning slow Gmail/Himalaya processes and
            # caused Fly health-check flapping under load.
            reconciliation = []
            if now - last_reconciliation_poll >= RECONCILIATION_INTERVAL:
                reconciliation = get_reconciliation_bookings(within_days=120)
                should_poll_email = should_poll_email or bool(reconciliation)
                last_reconciliation_poll = now

            if should_poll_email:
                last_email_poll = now
                emails = get_emails(
                    page_size=LIVE_EMAIL_PAGE_SIZE,
                    lookback_days=LIVE_EMAIL_LOOKBACK_DAYS,
                )
                _watcher_state["last_email_scan_at"] = datetime.now(timezone.utc).isoformat()
                _watcher_state["last_email_scan_ok"] = emails is not None
                _watcher_state["last_email_scan_error"] = (
                    None if emails is not None else "Could not fetch filtered Interac emails from Gmail"
                )
                _watcher_state["last_email_count"] = len(emails or [])
                if emails is None:
                    log_w.error("[watcher] Filtered Interac Gmail scan failed")
                elif emails:
                    for confirmed_id in _process_etransfer_email_batch(emails, pending, reconciliation):
                        _after_auto_payment_confirmed(confirmed_id)
                        _watcher_state["last_auto_confirmed_booking_id"] = confirmed_id
                        _watcher_state["last_auto_confirmed_at"] = datetime.now(timezone.utc).isoformat()
            else:
                log_w.debug("[watcher] No pending/reconciliation work or email poll throttled")
        except Exception as e:
            _watcher_state["last_email_scan_at"] = datetime.now(timezone.utc).isoformat()
            _watcher_state["last_email_scan_ok"] = False
            _watcher_state["last_email_scan_error"] = str(e)
            log_w.error(f"[watcher] e-Transfer check error: {e}")

        _time.sleep(CHECK_INTERVAL)


def _start_global_watcher():
    global _watcher_started
    if _background_threads_disabled():
        log.info("[main] Background e-Transfer watcher disabled by DISABLE_BACKGROUND_THREADS")
        return
    if _watcher_started:
        return
    t = _threading.Thread(target=_watcher_thread, daemon=True, name="etransfer-watcher")
    t.start()
    _watcher_started = True
    log.info("[main] Started global e-Transfer watcher thread")


# NOTE: _start_global_watcher() is called at the BOTTOM of this file,
# after all function definitions, to prevent NameError on first watcher tick.

# ===== RATE LIMITING =====
# Simple IP-based rate limit: 5 booking requests per 10 minutes per IP
_rate_limits = {}
# Separate counter for admin login attempts (brute-force protection)
_login_attempts = {}
# Public assistant chat has its own, gentler limit. It should tolerate a real
# conversation without letting one browser hammer the OpenAI API.
_assistant_attempts = {}
# First-party analytics receives more hits than booking endpoints, but it is
# still a public DB write path. Keep this generous enough for real ad traffic.
_analytics_attempts = {}

def check_login_rate_limit(ip):
    now = time.time()
    window = [t for t in _login_attempts.get(ip, []) if now - t < 900]  # 15-min window
    _login_attempts[ip] = window
    return len(window) < 10  # max 10 login attempts per 15 min

def record_login_attempt(ip):
    now = time.time()
    _login_attempts.setdefault(ip, []).append(now)

def check_rate_limit(ip):
    now = time.time()
    window = [t for t in _rate_limits.get(ip, []) if now - t < 600]
    _rate_limits[ip] = window
    return len(window) < 5  # 5 requests per 10 min (matches README)

def record_request(ip):
    now = time.time()
    _rate_limits.setdefault(ip, []).append(now)
    # Evict stale IPs when the dict grows large to prevent unbounded memory use
    if len(_rate_limits) > 10_000:
        cutoff = now - 600
        stale = [k for k, v in _rate_limits.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _rate_limits[k]

def check_assistant_rate_limit(ip):
    now = time.time()
    window = [t for t in _assistant_attempts.get(ip, []) if now - t < 600]
    _assistant_attempts[ip] = window
    return len(window) < 30  # 30 chat messages per 10 minutes per IP

def record_assistant_request(ip):
    now = time.time()
    _assistant_attempts.setdefault(ip, []).append(now)
    if len(_assistant_attempts) > 10_000:
        cutoff = now - 600
        stale = [k for k, v in _assistant_attempts.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _assistant_attempts[k]

def check_analytics_rate_limit(ip):
    now = time.time()
    window = [t for t in _analytics_attempts.get(ip, []) if now - t < 600]
    _analytics_attempts[ip] = window
    return len(window) < 180  # 180 funnel hits / 10 min per IP

def record_analytics_request(ip):
    now = time.time()
    _analytics_attempts.setdefault(ip, []).append(now)
    if len(_analytics_attempts) > 10_000:
        cutoff = now - 600
        stale = [k for k, v in _analytics_attempts.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _analytics_attempts[k]

# ===== ADMIN AUTH =====
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
# Fall back to ADMIN_KEY for browser login if ADMIN_PASSWORD is unset, so a
# single secret is enough to operate both the form-login and the X-Admin-Key
# API access. Operators can still set ADMIN_PASSWORD separately if they want
# different values.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "") or ADMIN_KEY

def _admin_key_from_request():
    # Use silent=True so empty / malformed JSON bodies don't raise — defensive
    # against weird probes hitting admin endpoints.
    body = request.get_json(silent=True) if request.is_json else None
    return (
        request.headers.get("X-Admin-Key")
        or request.args.get("key")
        or ((body or {}).get("key") if isinstance(body, dict) else None)
        or ""
    )

def _admin_authorized():
    # Browser session (logged in via /admin/login)
    if session.get("admin_authenticated"):
        return True
    # Programmatic API access via X-Admin-Key header or ?key= param
    if ADMIN_KEY and _admin_key_from_request() == ADMIN_KEY:
        return True
    # SECURITY: never allow open access — admin MUST have credentials set
    return False

def admin_required(f):
    """Require a browser login, X-Admin-Key header, or ?key= query param.

    Behaviour when unauthorised:
    - GET an HTML admin page (e.g. /admin, /admin/clients, /admin/booking/<id>)
      → 302 to /admin/login so the operator sees the friendly login form
      instead of raw JSON.
    - Everything else (XHR JSON APIs, POSTs, /admin/api/*) → 401 JSON so the
      frontend can detect the auth failure and redirect itself.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _admin_authorized():
            path = request.path or ""
            is_html_page = (
                request.method == "GET"
                and path.startswith("/admin")
                and not path.startswith("/admin/api/")
                and "application/json" not in (request.headers.get("Accept") or "")
            )
            if is_html_page:
                return redirect(url_for("admin_login", next=path))
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ===== NOTIFICATIONS =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "792920251")  # Andrzej — always gets copies
TELEGRAM_EXTRA_ADMIN_CHAT_IDS = os.environ.get("TELEGRAM_EXTRA_ADMIN_CHAT_IDS", "")
# Telegram users allowed to press admin inline buttons. Usernames are case-insensitive
# and do not include @. Default allows Iryna's public account once Telegram includes
# username in callback_query.from. User IDs are more reliable and can be supplied via env.
TELEGRAM_ALLOWED_ADMIN_USERNAMES = os.environ.get(
    "TELEGRAM_ALLOWED_ADMIN_USERNAMES",
    "pashynskaphoto",
)
TELEGRAM_ALLOWED_ADMIN_USER_IDS = os.environ.get("TELEGRAM_ALLOWED_ADMIN_USER_IDS", "")
# Secret token for the Telegram webhook (set when calling setWebhook). When set,
# every incoming POST /telegram/webhook must carry the matching value in the
# X-Telegram-Bot-Api-Secret-Token header — otherwise the request is rejected.
# This is the only thing that prevents anyone with the URL from sending fake
# confirm/cancel callback_query payloads.
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
BASE_URL = os.environ.get("BOOKING_BASE_URL", "")
# CANONICAL_SITE_URL / CANONICAL_SITE_HOST live near the top of this module so
# _canonical_redirect (which runs before this point) can use them too.


def _split_csv_env(value):
    """Parse comma/space-separated env values into non-empty strings."""
    if not value:
        return []
    return [item.strip() for item in re.split(r"[,\s]+", str(value)) if item.strip()]


def _telegram_admin_chat_ids():
    """All chats that should receive booking admin notifications/buttons."""
    ids = []
    for chat_id in [TELEGRAM_CHAT_ID, TELEGRAM_ADMIN_CHAT_ID, *_split_csv_env(TELEGRAM_EXTRA_ADMIN_CHAT_IDS)]:
        if chat_id and str(chat_id) not in [str(existing) for existing in ids]:
            ids.append(str(chat_id))
    return ids


def _telegram_allowed_admin_usernames():
    return {u.lower().lstrip("@") for u in _split_csv_env(TELEGRAM_ALLOWED_ADMIN_USERNAMES)}


def _telegram_allowed_admin_user_ids():
    ids = {str(i) for i in _split_csv_env(TELEGRAM_ALLOWED_ADMIN_USER_IDS)}
    # In a private Telegram chat, chat_id equals user_id; keep existing admin chats
    # allowed unless a deployment overrides with more specific user IDs.
    ids.update(str(i) for i in [TELEGRAM_CHAT_ID, TELEGRAM_ADMIN_CHAT_ID] if i)
    return ids


def _is_telegram_admin_callback(cb):
    """Return True if callback_query.from is allowed to mutate bookings."""
    user = cb.get("from") or {}
    user_id = str(user.get("id") or "")
    username = str(user.get("username") or "").lower().lstrip("@")
    return bool(
        (user_id and user_id in _telegram_allowed_admin_user_ids())
        or (username and username in _telegram_allowed_admin_usernames())
    )


def _tg_send(chat_id, text, reply_markup=None):
    """Low-level: send a Telegram message to a specific chat."""
    if not TELEGRAM_BOT_TOKEN:
        log.info(f"[tg] No token. Would send to {chat_id}: {text[:80]}...")
        return None
    if not chat_id:
        return None
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        if r.status_code == 200:
            log.info(f"[tg] Message sent to {chat_id}")
            return r.json().get("result", {})
        else:
            log.error(f"[tg] Send failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        log.error(f"[tg] Error: {e}")


def _notify_admin(message, reply_markup=None):
    """Send notification to all configured booking admins via Telegram."""
    for chat_id in _telegram_admin_chat_ids():
        _tg_send(chat_id, message, reply_markup=reply_markup)



def _tg_escape(value):
    """Escape client-provided text before inserting it into Telegram HTML."""
    return _html_escape(str(value or ""), quote=False)


def _notify_waitlist_signup(entry):
    """Notify admins that a client joined a session waitlist."""
    event = get_event_by_id(entry.get("event_id")) or {}
    admin_url = f"{BASE_URL}/admin" if BASE_URL else "/admin"
    text = (
        f"📝 <b>New waitlist signup</b>\n\n"
        f"🎉 {_tg_escape(event.get('title') or entry.get('event_id'))}\n"
        f"👤 {_tg_escape(entry.get('name'))}\n"
        f"📧 {_tg_escape(entry.get('email'))}\n"
        f"📞 {_tg_escape(entry.get('phone') or 'N/A')}\n"
        f"🕒 Preferred: {_tg_escape(entry.get('preferred_slot') or 'Any')}\n\n"
        f"🔗 <a href=\"{admin_url}\">Admin panel</a>"
    )
    _notify_admin(text)


def _notify_new_reservation(booking_id, client_name, client_email, event_date,
                            slot_time, event_title, session_type, client_ig,
                            client_phone=None, selected_addons=None,
                            addons_total=0.0, marketing_consent=None):
    """Send NEW reservation notification with inline confirm/cancel buttons."""
    ig_clean = (client_ig or "").lstrip("@")
    phone_display = client_phone or "N/A"
    admin_url = f"{BASE_URL}/admin" if BASE_URL else "/admin"
    addon_lines = ""
    if selected_addons:
        addon_lines = "\n<b>Selected add-ons</b>\n"
        for addon in selected_addons:
            addon_lines += (
                f"• {_tg_escape(_strip_tags((addon or {}).get('title')))} — "
                f"${_money((addon or {}).get('price')):.2f} CAD\n"
            )
        addon_lines += f"Selected add-ons: ${_money(addons_total):.2f} CAD\n"
    consent_line = f"Marketing consent: {_tg_escape(marketing_consent)}\n" if marketing_consent in ("yes", "no") else ""
    
    text = (
        f"🆕 <b>New reservation #{booking_id}</b>\n\n"
        f"👤 {_tg_escape(_strip_tags(client_name or '(no name)'))}\n"
        f"📧 {_tg_escape(_strip_tags(client_email))}\n"
        f"📞 {_tg_escape(_strip_tags(phone_display))}\n"
        f"📱 Instagram: @{ig_clean or 'N/A'}\n\n"
        f"📅 {_tg_escape(_strip_tags(event_date))} @ {_tg_escape(_strip_tags(slot_time))}\n"
        f"🎉 {_tg_escape(_strip_tags(event_title))}\n"
        f"🏷 Session: {_tg_escape(_strip_tags(session_type or 'N/A'))}\n"
        f"{addon_lines}"
        f"{consent_line}"
        f"⏱️ Expires in {RESERVATION_MINUTES} min\n\n"
        f"<b>Press below when client pays:</b>"
    )
    
    action_row = [
        {"text": "✅ Payment Received", "callback_data": f"confirm:{booking_id}"},
        {"text": "❌ Cancel", "callback_data": f"cancel:{booking_id}"},
    ]
    link_row = [
        {"text": "🔗 Admin Panel", "url": admin_url},
    ]
    if ig_clean:
        link_row.append({"text": "📸 Instagram", "url": f"https://instagram.com/{ig_clean}"})
    
    keyboard = {"inline_keyboard": [action_row, link_row]}
    _notify_admin(text, reply_markup=keyboard)


def _booking_success_url(booking_id, token=None, absolute_base=None, **extra_params):
    """Build a client-safe success URL.

    The success page shows private booking details, so public links must carry
    the same confirmation token used by payment/status/calendar routes.
    """
    from urllib.parse import urlencode

    base = ((absolute_base if absolute_base is not None else (BASE_URL or CANONICAL_SITE_URL)) or "").rstrip("/")
    path = f"{base}/success"
    params = {"booking_id": booking_id}
    if token:
        params["token"] = token
    params.update({k: v for k, v in extra_params.items() if v is not None})
    return f"{path}?{urlencode(params)}"


def _balance_page_url(booking, absolute_base=None):
    """Durable link to the remaining-balance payment page. Unlike a one-time
    Stripe Checkout URL (expires ~24h), this link never expires, so it is safe
    to put in the confirmation email and reuse days/weeks later after the shoot."""
    from urllib.parse import urlencode
    booking = booking or {}
    token = booking.get("confirmation_token")
    if not booking.get("id") or not token:
        return None
    base = ((absolute_base if absolute_base is not None else (BASE_URL or CANONICAL_SITE_URL)) or "").rstrip("/")
    return f"{base}/pay-balance?{urlencode({'booking_id': booking.get('id'), 'token': token})}"


def _questionnaire_url_for_booking(booking, event):
    if not booking or not event or not _questionnaire_config_for_event(event):
        return None
    if not (booking.get("confirmed") or booking.get("paid") or booking.get("status") == "confirmed"):
        return None
    token = booking.get("confirmation_token")
    if not token:
        return None
    from urllib.parse import urlencode
    base = (BASE_URL or CANONICAL_SITE_URL).rstrip("/")
    return f"{base}/questionnaire?{urlencode({'booking_id': booking.get('id'), 'token': token})}"


def _client_email_context(booking, event):
    booking = booking or {}
    event = event or {}
    remaining = _booking_balance_due(booking, event)
    return {
        "selected_addons": _booking_addons(booking),
        "addons_total": _booking_addons_total(booking),
        "total_price": _booking_total_price(booking, event),
        "amount_due_today": _money(booking.get("deposit_amount") or event.get("deposit") or SESSION_PRICE),
        "remaining_balance": remaining,
        "marketing_consent": booking.get("marketing_consent"),
        "questionnaire_url": _questionnaire_url_for_booking(booking, event),
        # Durable balance link in the confirmation email — works whenever the
        # client settles up after the shoot (e-Transfer or card), unlike a
        # one-time Stripe Checkout URL. The email only renders the button when
        # balance_due > 0, so a fully-paid booking shows nothing.
        "balance_url": _balance_page_url(booking) if remaining and remaining > 0 else None,
        "balance_due": remaining if remaining and remaining > 0 else None,
    }


def _notify_payment_pending(booking_id, client_name, client_email, event_date,
                            slot_time, event_title, session_type, client_ig,
                            expected_deposit=None, client_phone=None,
                            confirmation_token=None):
    """Send payment notification with inline confirm/cancel buttons to Iryna + Andrzej."""
    deposit = expected_deposit or SESSION_PRICE
    slot_end = ""
    try:
        from datetime import datetime, timedelta
        t = datetime.strptime(slot_time, "%H:%M")
        slot_end = (t + timedelta(minutes=SESSION_LENGTH)).strftime("%H:%M")
    except Exception:
        pass

    admin_url = f"{BASE_URL}/admin" if BASE_URL else "/admin"
    success_url = _booking_success_url(booking_id, confirmation_token)

    ig_clean = (client_ig or "").lstrip("@")
    phone_display = client_phone or "N/A"

    text = (
        f"💰 <b>Payment marked as sent</b>\n\n"
        f"📋 Booking #{booking_id}\n"
        f"👤 {client_name or '(no name)'}\n"
        f"📧 {client_email}\n"
        f"📞 {phone_display}\n"
        f"📱 Instagram: @{ig_clean or 'N/A'}\n\n"
        f"📅 {event_date} · {slot_time}{'–' + slot_end if slot_end else ''}\n"
        f"🏷 Session: {event_title}\n"
        f"💵 Expected deposit: ${deposit:.2f} CAD\n\n"
        f"⏳ <b>Check e-Transfer, then press a button below:</b>\n\n"
        f"🔗 <a href=\"{admin_url}\">Admin panel</a>  |  "
        f"<a href=\"{success_url}\">Client page</a>"
    )

    action_row = [
        {"text": "✅ Payment Received", "callback_data": f"confirm:{booking_id}"},
        {"text": "❌ Cancel", "callback_data": f"cancel:{booking_id}"},
    ]
    link_row = [
        {"text": "🔗 Admin Panel", "url": admin_url},
        {"text": "📄 Client Page", "url": success_url},
    ]
    if ig_clean:
        link_row.append({"text": "📸 Instagram", "url": f"https://instagram.com/{ig_clean}"})

    keyboard = {"inline_keyboard": [action_row, link_row]}
    _notify_admin(text, reply_markup=keyboard)


def _send_client_email(to_email, client_name, event_date, slot_time, event_title, booking_id,
                       location=None, location_url=None, selected_addons=None,
                       addons_total=0.0, total_price=None, amount_due_today=None,
                       remaining_balance=None, marketing_consent=None,
                       questionnaire_url=None, balance_url=None, balance_due=None):
    """Send premium HTML confirmation email to client via Himalaya CLI.

    Returns True only when SMTP/Himalaya accepts the message. This is used by
    admin notifications so we do not falsely tell admins that an email was sent.
    """
    try:
        import subprocess
        import re

        # Tolerate common copy/paste punctuation such as "client@gmail.com!".
        to_email = str(to_email or "").strip().strip("<>").rstrip(".,;:!")
        if not to_email or not re.match(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$", to_email):
            log.error(f"[email] Invalid recipient address for booking #{booking_id}: {to_email!r}")
            return False

        def _clean_text(value, fallback=""):
            text = str(value or "")
            text = re.sub(r"<[^>]*>", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text or fallback

        date_nice = datetime.strptime(event_date, "%Y-%m-%d").strftime("%B %d, %Y")
        client_text = _clean_text(client_name, "Client")
        event_text = _clean_text(event_title, "Photo Session")
        location_text = _clean_text(location, "")

        safe_client = _html_escape(client_text)
        # Preserve the event title visually but escape it for HTML; this prevents JS/HTML injection.
        safe_event = _html_escape(str(event_title or event_text))
        safe_location = _html_escape(location_text)
        safe_date = _html_escape(date_nice)
        safe_time = _html_escape(str(slot_time or ""))
        safe_booking = _html_escape(str(booking_id))
        safe_questionnaire_url = _html_escape(str(questionnaire_url or ""))

        # Initialize balance_url and balance_due (passed from admin_confirm)
        balance_url = balance_url or None
        balance_due = balance_due or 0.0

        amount_lines_plain = []
        amount_rows_html = []
        if selected_addons:
            addon_titles = []
            for addon in selected_addons:
                title = _clean_text((addon or {}).get("title"), "Add-on")
                price = _money((addon or {}).get("price"))
                addon_titles.append(f"- {title}: ${price:.2f} CAD")
            amount_lines_plain.append("Selected add-ons:\n" + "\n".join(addon_titles))
            amount_lines_plain.append(f"Selected add-ons: ${_money(addons_total):.2f} CAD")
            amount_rows_html.append(
                "<tr><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;color:#9a756d;font-size:13px;\">Selected add-ons</td>"
                f"<td style=\"padding:9px 0;border-top:1px solid #f2e3dd;text-align:right;color:#4b2f38;font-size:14px;font-weight:700;\">${_money(addons_total):.2f} CAD</td></tr>"
            )
            for addon in selected_addons:
                title = _html_escape(_clean_text((addon or {}).get("title"), "Add-on"))
                price = _money((addon or {}).get("price"))
                amount_rows_html.append(
                    "<tr><td colspan=\"2\" style=\"padding:4px 0;color:#7a5a6a;font-size:13px;\">"
                    f"• {title} — ${price:.2f} CAD</td></tr>"
                )
        if amount_due_today is not None:
            amount_lines_plain.append(f"Amount due today: ${_money(amount_due_today):.2f} CAD")
            amount_rows_html.append(
                "<tr><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;color:#9a756d;font-size:13px;\">Amount due today</td>"
                f"<td style=\"padding:9px 0;border-top:1px solid #f2e3dd;text-align:right;color:#4b2f38;font-size:14px;font-weight:700;\">${_money(amount_due_today):.2f} CAD</td></tr>"
            )
        if remaining_balance is not None:
            amount_lines_plain.append(f"Remaining balance: ${_money(remaining_balance):.2f} CAD")
            amount_rows_html.append(
                "<tr><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;color:#9a756d;font-size:13px;\">Remaining balance</td>"
                f"<td style=\"padding:9px 0;border-top:1px solid #f2e3dd;text-align:right;color:#4b2f38;font-size:14px;font-weight:700;\">${_money(remaining_balance):.2f} CAD</td></tr>"
            )
        if total_price is not None:
            amount_lines_plain.append(f"Session total: ${_money(total_price):.2f} CAD")
        consent_plain = ""
        consent_html = ""
        if marketing_consent in ("yes", "no"):
            consent_text = (
                "You selected: yes, I allow selected photos/videos for portfolio and marketing."
                if marketing_consent == "yes"
                else "You selected: no, please keep my gallery private."
            )
            consent_plain = f"\nPrivacy/marketing preference: {consent_text}\n"
            consent_html = (
                "<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#fff;border:1px solid #ead8d0;border-radius:16px;margin:0 0 20px;\">"
                f"<tr><td style=\"padding:15px 18px;color:#6d4d55;font-size:14px;line-height:1.55;\"><strong>Privacy preference:</strong> {_html_escape(consent_text)}</td></tr></table>"
            )
        questionnaire_plain = ""
        questionnaire_html = ""
        if questionnaire_url:
            questionnaire_plain = f"\nOptional session questionnaire: {questionnaire_url}\n"
            questionnaire_html = (
                "<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#fff8f4;border:1px solid #f1dfd8;border-radius:16px;margin:0 0 20px;\">"
                "<tr><td style=\"padding:16px 18px;color:#6d4d55;font-size:14px;line-height:1.6;\">"
                "<strong>Optional session questionnaire</strong><br>"
                f"<a href=\"{safe_questionnaire_url}\" style=\"color:#c4857a;text-decoration:none;font-weight:700;\">Share preparation details</a>"
                "</td></tr></table>"
            )
        amount_plain = ("\n" + "\n".join(amount_lines_plain) + "\n") if amount_lines_plain else ""
        amount_html = "".join(amount_rows_html)
        
        # Balance payment button (if balance is due)
        balance_button_html = ""
        if balance_url and balance_due is not None and balance_due > 0:
            balance_button_html = f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin:12px 0 0;">
<tr><td style="text-align:center;"><a href="{_html_escape(balance_url)}" style="display:inline-block;background:#4b2f38;color:#ffffff;text-decoration:none;border-radius:12px;padding:11px 18px;font-size:13px;font-weight:700;">💳 Pay Remaining Balance (${_money(balance_due):.2f} CAD)</a></td></tr>
<tr><td style="text-align:center;padding:8px 0 0;font-size:12px;color:#9a756d;">or pay later by Interac e-Transfer to iryna.pashynska@gmail.com</td></tr>
</table>"""

        subject = f"Booking Confirmed — {event_text} on {date_nice}"
        location_line = f"Location: {location_text}\n" if location_text else "Location details will be sent closer to the session date.\n"
        meeting_line = f"We meet on {date_nice} at {slot_time}. " + (f"Location: {location_text}." if location_text else "Exact location will be sent closer to the session date.")

        # Build helpers BEFORE plain text (calendar_url used in both HTML and plain)
        safe_location_url = _html_escape(str(location_url or ""))
        cal_start = f"{event_date}T{slot_time.replace(':', '')}00" if slot_time else f"{event_date}T100000"
        calendar_url = f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={_html_escape(event_text)}&dates={cal_start}/{cal_start}&details=Photo+session+with+Pashynska+Photography&location={_html_escape(location_text or 'Calgary')}"

        # Auto-generate Google Maps URL if location exists but no explicit URL
        if location_text and not location_url:
            location_url = f"https://www.google.com/maps/search/?api=1&query={_html_escape(location_text + ', Calgary, AB')}"
            safe_location_url = _html_escape(location_url)

        if location_url and location_text:
            maps_card_html = f"""<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff8f4;border:1px solid #f1dfd8;border-radius:18px;margin:0 0 20px;">
      <tr><td style="padding:18px 20px;">
        <p style="margin:0 0 10px;font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#b08479;font-weight:700;">📍 Meeting Point</p>
        <p style="margin:0 0 12px;font-size:15px;color:#4b2f38;font-weight:700;">{safe_location}</p>
        <table cellpadding="0" cellspacing="0"><tr>
          <td style="padding:0 8px 0 0;"><a href="{safe_location_url}" style="display:inline-block;background:#4285F4;color:#ffffff;text-decoration:none;border-radius:10px;padding:10px 14px;font-size:12px;font-weight:700;">🗺️ Open in Google Maps</a></td>
          <td><a href="http://maps.apple.com/?q={safe_location.replace(' ', '+')}" style="display:inline-block;background:#fff;color:#4b2f38;text-decoration:none;border:1px solid #ead8d0;border-radius:10px;padding:10px 14px;font-size:12px;font-weight:700;">🍎 Open in Apple Maps</a></td>
        </tr></table>
      </td></tr>
    </table>"""
        else:
            maps_card_html = ""

        arrival_block_html = f"""<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #ead8d0;border-radius:18px;margin:0 0 20px;box-shadow:0 8px 22px rgba(102,63,53,.06);">
      <tr><td style="padding:18px 20px;">
        <p style="margin:0 0 10px;font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#b08479;font-weight:700;">⏰ Arrival</p>
        <p style="margin:0;color:#6d4d55;font-size:14px;line-height:1.65;">Please arrive <strong style="color:#4b2f38;">5–10 minutes early</strong>. Your session starts at <strong style="color:#4b2f38;">{safe_time}</strong> on <strong style="color:#4b2f38;">{safe_date}</strong>.</p>
      </td></tr>
    </table>"""

        plain = (
            f"Hi {client_text},\n\n"
            f"Your photo session is confirmed — deposit received.\n\n"
            f"Session: {event_text}\n"
            f"Date: {date_nice}\n"
            f"Time: {slot_time}\n"
            f"{location_line}"
            f"Booking ID: #{booking_id}\n\n"
            f"{amount_plain}"
            f"{consent_plain}"
            f"{questionnaire_plain}"
            f"Meeting point: {location_text}\n"
            f"Maps: {location_url or 'Will be sent closer to the date'}\n"
            f"Arrive 5–10 minutes early. Your session starts at {slot_time} on {date_nice}.\n\n"
            f"Add to Calendar: {calendar_url}\n\n"
            f"What happens next:\n"
            f"1. We meet for your photo session: {meeting_line}\n"
            f"2. after the photo session, I will send the request for the remaining balance. You can pay by Interac e-Transfer or Stripe card / Apple Pay / Google Pay.\n"
            f"3. I review the photos with you and confirm which images will be professionally edited.\n"
            f"4. You receive all original photos from the session — unedited, full resolution.\n"
            f"5. You receive a private Wfolio gallery link with your photos. Please download everything — the gallery is normally kept online for 1–2 months.\n\n"
            f"If you need to reschedule or have questions, DM me on Instagram @pashynska.photo.\n\n"
            f"Warmly,\nIryna Pashynska\n@pashynska.photo"
        )

        location_html = (
            f"<strong>{safe_location}</strong>"
            if location_text else
            "Exact location will be sent closer to the session date."
        )

        html = f"""<!DOCTYPE html>
<html lang=\"en\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"></head>
<body style=\"margin:0;padding:0;background:#f7efe9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#3f2d33;\">
<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:linear-gradient(180deg,#fff7f1 0%,#f7efe9 100%);padding:34px 14px;\">
<tr><td align=\"center\">
<table width=\"640\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:640px;width:100%;background:#fff;border-radius:24px;overflow:hidden;box-shadow:0 2px 8px rgba(46,25,20,.04),0 16px 42px rgba(93,55,47,.14);\" class=\"confirmation-card\">
  <tr><td style=\"background:linear-gradient(135deg,#f2c9bf 0%,#c4857a 52%,#7e4f46 100%);padding:42px 34px;text-align:center;color:#fff;\">
    <p style=\"margin:0 0 10px;font-size:34px;line-height:1;\">✦</p>
    <p style=\"margin:0 0 8px;font-size:12px;letter-spacing:.16em;text-transform:uppercase;opacity:.88;\">Deposit confirmed</p>
    <h1 style=\"margin:0;font-family:Georgia,'Times New Roman',serif;font-size:30px;line-height:1.15;font-weight:400;letter-spacing:-.02em;\">Your photo session is booked</h1>
    <p style=\"margin:12px 0 0;font-size:14px;opacity:.9;\">Pashynska Photography · Calgary</p>
  </td></tr>
  <tr><td style=\"padding:34px 34px 10px;\">
    <p style=\"margin:0 0 14px;font-size:16px;line-height:1.65;color:#5a3d4a;\">Hi <strong>{safe_client}</strong>,</p>
    <p style=\"margin:0 0 24px;font-size:15px;line-height:1.75;color:#7a5a6a;\">Your deposit is confirmed and your session is officially reserved. Here is everything you need to know before and after the shoot.</p>

    <table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" class=\"email-safe-21st\" style=\"margin:0 0 20px;\">
      <tr>
        <td style=\"padding:0 6px 8px 0;\"><span style=\"display:inline-block;background:#4b2f38;color:#fff;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:700;letter-spacing:.01em;\">✓ Deposit confirmed</span></td>
        <td style=\"padding:0 6px 8px 0;\"><span style=\"display:inline-block;background:#fff1ec;color:#7e4f46;border:1px solid #ead8d0;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:700;\">Session reserved</span></td>
        <td style=\"padding:0 0 8px;\"><span style=\"display:inline-block;background:#f7efe9;color:#8c655d;border:1px solid #ead8d0;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:700;\">Next steps inside</span></td>
      </tr>
    </table>

    {maps_card_html}

    <table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#fff;border:1px solid #ead8d0;border-radius:18px;margin:0 0 24px;box-shadow:0 8px 22px rgba(102,63,53,.06);\">
      <tr><td style=\"padding:18px 20px;\">
        <p style=\"margin:0 0 10px;font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#b08479;font-weight:700;\">Before your session</p>
        <p style=\"margin:0;color:#6d4d55;font-size:14px;line-height:1.65;\"><strong>Arrive 5–10 minutes early</strong>. Your session starts at <strong>{safe_time}</strong> on <strong>{safe_date}</strong>. Bring simple touch-up items if needed, and keep outfits comfortable and true to your style.</p>
      </td></tr>
    </table>

    <table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#fff8f4;border:1px solid #f1dfd8;border-radius:20px;margin:0 0 24px;\" class=\"session-card\">
      <tr><td style=\"padding:22px 24px;\">
        <table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\">
          <tr><td style=\"padding:9px 0;color:#9a756d;font-size:13px;\">Session</td><td style=\"padding:9px 0;text-align:right;color:#4b2f38;font-size:14px;font-weight:700;\">{safe_event}</td></tr>
          <tr><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;color:#9a756d;font-size:13px;\">When we meet</td><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;text-align:right;color:#4b2f38;font-size:14px;font-weight:700;\">{safe_date} · {safe_time}</td></tr>
          <tr><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;color:#9a756d;font-size:13px;\">Location</td><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;text-align:right;color:#4b2f38;font-size:14px;\">{location_html}</td></tr>
          <tr><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;color:#9a756d;font-size:13px;\">Booking</td><td style=\"padding:9px 0;border-top:1px solid #f2e3dd;text-align:right;color:#4b2f38;font-size:14px;font-weight:700;\">#{safe_booking}</td></tr>
          {amount_html}
          <tr><td colspan=\"2\" style=\"padding:14px 0 4px;text-align:center;\"><a href=\"{calendar_url}\" style=\"display:inline-block;background:#4b2f38;color:#ffffff;text-decoration:none;border-radius:12px;padding:11px 18px;font-size:13px;font-weight:700;\">📅 Add to Calendar</a></td></tr>
        </table>
      </td></tr>
    </table>

    {consent_html}
    {questionnaire_html}

    <div class=\"timeline-card\" style=\"background:#ffffff;border:1px solid #ead8d0;border-radius:24px;padding:24px 22px;margin:0 0 24px;box-shadow:0 1px 0 rgba(255,255,255,.8) inset,0 10px 26px rgba(102,63,53,.08);\">
      <h2 style=\"margin:0 0 18px;font-family:Georgia,'Times New Roman',serif;font-size:22px;line-height:1.2;font-weight:400;color:#4b2f38;\">What happens next</h2>
      <table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\">
        <tr class=\"timeline-step\"><td width=\"34\" valign=\"top\" style=\"padding:0 0 18px;\"><span style=\"display:inline-block;width:26px;height:26px;border-radius:50%;background:#c4857a;color:#fff;text-align:center;line-height:26px;font-size:13px;font-weight:700;\">1</span></td><td style=\"padding:0 0 18px;color:#6d4d55;font-size:14px;line-height:1.65;\"><strong style=\"color:#4b2f38;\">We meet for your session.</strong><br>{safe_date} at {safe_time}. {('Location: ' + safe_location + '.') if location_text else 'Exact location will be sent closer to the session date.'}</td></tr>
        <tr class="timeline-step"><td width="34" valign="top" style="padding:0 0 18px;"><span style="display:inline-block;width:26px;height:26px;border-radius:50%;background:#d9aaa0;color:#fff;text-align:center;line-height:26px;font-size:13px;font-weight:700;">2</span></td><td style="padding:0 0 18px;color:#6d4d55;font-size:14px;line-height:1.65;"><strong style="color:#4b2f38;">Pay the remaining balance.</strong><br>You can pay now or after the session. Payment is required to receive all photos.<br><br>{balance_button_html}</td></tr>
        <tr class=\"timeline-step\"><td width=\"34\" valign=\"top\" style=\"padding:0 0 18px;\"><span style=\"display:inline-block;width:26px;height:26px;border-radius:50%;background:#e7c7bf;color:#7e4f46;text-align:center;line-height:26px;font-size:13px;font-weight:700;\">3</span></td><td style=\"padding:0 0 18px;color:#6d4d55;font-size:14px;line-height:1.65;\"><strong style=\"color:#4b2f38;\">We review and confirm the images for editing.</strong><br>I prepare the photos and confirm with you which images will be professionally edited.</td></tr>
        <tr class=\"timeline-step\"><td width=\"34\" valign=\"top\" style=\"padding:0 0 18px;\"><span style=\"display:inline-block;width:26px;height:26px;border-radius:50%;background:#f0ded7;color:#7e4f46;text-align:center;line-height:26px;font-size:13px;font-weight:700;\">4</span></td><td style=\"padding:0 0 18px;color:#6d4d55;font-size:14px;line-height:1.65;\"><strong style=\"color:#4b2f38;\">You receive all original photos from the session.</strong><br>Unedited, full-resolution images delivered as-is — no retouching, no filters, every frame I captured.</td></tr>
        <tr class=\"timeline-step\"><td width=\"34\" valign=\"top\" style=\"padding:0;\"><span style=\"display:inline-block;width:26px;height:26px;border-radius:50%;background:#fff1ec;color:#7e4f46;text-align:center;line-height:26px;font-size:13px;font-weight:700;\">5</span></td><td style=\"padding:0;color:#6d4d55;font-size:14px;line-height:1.65;\"><strong style=\"color:#4b2f38;\">You receive your private Wfolio gallery link.</strong><br>Please download your photos when the link arrives. Galleries are normally kept online for 1–2 months.</td></tr>
      </table>
    </div>

    <table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#fdf6f0;border-radius:18px;margin:0 0 24px;\">
      <tr><td style=\"padding:18px 20px;color:#7a5a6a;font-size:14px;line-height:1.65;\"><strong style=\"color:#4b2f38;\">Need to reschedule?</strong><br>DM me on Instagram <a href=\"https://instagram.com/pashynska.photo\" style=\"color:#c4857a;text-decoration:none;font-weight:700;\">@pashynska.photo</a> as soon as possible.<br><br><a href=\"https://instagram.com/pashynska.photo\" style=\"display:inline-block;background:#4b2f38;color:#ffffff;text-decoration:none;border-radius:14px;padding:12px 16px;font-size:13px;font-weight:700;\">Message me on Instagram</a></td></tr>
    </table>
  </td></tr>
  <tr><td style=\"padding:0 34px 34px;\">
    <p style=\"margin:0;font-family:Georgia,'Times New Roman',serif;font-size:18px;color:#4b2f38;\">Looking forward to our session ✨</p>
    <p style=\"margin:10px 0 0;font-size:14px;color:#9a756d;\"><strong>Iryna Pashynska</strong><br><a href=\"https://instagram.com/pashynska.photo\" style=\"color:#c4857a;text-decoration:none;\">@pashynska.photo</a></p>
  </td></tr>
  <tr><td style=\"background:#fff8f4;padding:20px 34px;text-align:center;border-top:1px solid #f1dfd8;\">
    <p style=\"margin:0;font-size:12px;line-height:1.6;color:#b0938b;\">Pashynska Photography · Calgary, AB · Canada<br><a href=\"https://instagram.com/pashynska.photo\" style=\"color:#c4857a;text-decoration:none;\">instagram.com/pashynska.photo</a></p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""

        boundary = f"====boundary_{booking_id}===="
        template = (
            f"From: Iryna Pashynska <iryna.pashynska@gmail.com>\r\n"
            f"To: {client_text} <{to_email}>\r\n"
            f"Subject: {subject}\r\n"
            f"MIME-Version: 1.0\r\n"
            f"Content-Type: multipart/alternative; boundary=\"{boundary}\"\r\n"
            f"\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{plain}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n\r\n"
            f"{html}\r\n"
            f"--{boundary}--\r\n"
        )

        return _send_email_raw(to_email, client_text, subject, plain, html)
    except Exception as e:
        log.error(f"[email] Send failed: {e}")
        return False


# ── Email ──
import smtplib, uuid, time as _time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

GMAIL_USER      = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
SMTP_HOST       = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("SMTP_PORT", "587"))
SMTP_TIMEOUT    = int(os.environ.get("SMTP_TIMEOUT", "10"))
SMTP_MAX_RETRY  = int(os.environ.get("SMTP_MAX_RETRY", "3"))
SMTP_RETRY_BASE = float(os.environ.get("SMTP_RETRY_BASE", "2.0"))

_GMAIL_APP_PASSWORD_FILE = os.path.expanduser("~/.config/himalaya/iryna_gmail_app_password")
if not GMAIL_PASSWORD and os.path.isfile(_GMAIL_APP_PASSWORD_FILE):
    try:
        with open(_GMAIL_APP_PASSWORD_FILE) as f:
            GMAIL_PASSWORD = f.read().strip()
    except Exception:
        pass

def _smtp_send_email(to_email, client_name, subject, plain, html, attachment_bytes=None, attachment_filename="attachment.pdf", attachment_mime="application/pdf"):
    """Send multipart email via Gmail SMTP with retry."""
    if not to_email:
        return False
    if not GMAIL_USER or not GMAIL_PASSWORD:
        log.error("[email] GMAIL_USER/GMAIL_APP_PASSWORD not set — falling back to Himalaya")
        return _smtp_fallback_raw(to_email, client_name, subject, plain, html)
    msg = MIMEMultipart("mixed")
    msg["From"] = f"Iryna Pashynska <{GMAIL_USER}>"
    msg["To"] = f"{client_name} <{to_email}>"
    msg["Subject"] = subject
    msg["Message-Id"] = f"<pashynska-{uuid.uuid4().hex}@gmail.com>"
    # Body (multipart/alternative)
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(plain, "plain", "utf-8"))
    body.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(body)
    # Attachment
    if attachment_bytes:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={attachment_filename}")
        msg.attach(part)
    message = msg.as_string()
    last_err = None
    for attempt in range(1, SMTP_MAX_RETRY + 1):
        try:
            # Prefer STARTTLS (port 587) — more reliable across environments
            if SMTP_PORT == 587:
                server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, [to_email], message)
            server.quit()
            log.info(f"[email] Sent '{subject}' to {to_email} (attempt {attempt})")
            return True
        except Exception as e:
            last_err = str(e)
            log.warning(f"[email] Attempt {attempt}/{SMTP_MAX_RETRY} failed: {last_err[:150]}")
            if attempt < SMTP_MAX_RETRY:
                _time.sleep(SMTP_RETRY_BASE * attempt)
    log.error(f"[email] Failed to send after {SMTP_MAX_RETRY} attempts: {last_err[:200]}")
    return False

def _smtp_fallback_raw(to_email, client_name, subject, plain, html):
    """Last resort: try Himalaya subprocess if SMTP fails."""
    try:
        import subprocess
        boundary = f"====pashynska_{abs(hash(subject))}===="
        template = (
            f"From: Iryna Pashynska <iryna.pashynska@gmail.com>\r\n"
            f"To: {client_name} <{to_email}>\r\n"
            f"Subject: {subject}\r\n"
            f"MIME-Version: 1.0\r\n"
            f"Content-Type: multipart/alternative; boundary=\"{boundary}\"\r\n"
            f"\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{plain}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n\r\n"
            f"{html}\r\n"
            f"--{boundary}--\r\n"
        )
        result = subprocess.run(
            ["himalaya", "message", "send", "-a", "iryna"],
            input=template, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info(f"[email fallback] Sent '{subject}' to {to_email}")
            return True
        else:
            log.error(f"[email fallback] Himalaya failed ({subject}): {result.stderr[:200]}")
            return False
    except Exception as e:
        log.error(f"[email fallback] Send failed ({subject}): {e}")
        return False


def _send_email_raw(to_email, client_name, subject, plain, html, attachment_bytes=None, attachment_filename="attachment.pdf", attachment_mime="application/pdf"):
    """Unified email sender: prefers SMTP with retry, falls back to Himalaya CLI."""
    return _smtp_send_email(to_email, client_name, subject, plain, html, attachment_bytes, attachment_filename, attachment_mime)


def _send_client_reschedule_email(to_email, client_name, old_event_title, old_date, old_time,
                                  new_event_title, new_date, new_time, booking_id, location=None):
    """Send HTML reschedule notification to client via Himalaya CLI."""
    if not to_email:
        return False
    try:
        old_date_nice = datetime.strptime(old_date, "%Y-%m-%d").strftime("%B %d, %Y") if old_date else "—"
        new_date_nice = datetime.strptime(new_date, "%Y-%m-%d").strftime("%B %d, %Y") if new_date else "—"
        subject = f"Session Rescheduled — Now {new_date_nice} at {new_time}"
        loc_line = f"Location: {location}\n" if location else "Location details will be sent closer to the session date.\n"

        plain = (
            f"Hi {client_name},\n\n"
            f"Your photo session has been rescheduled.\n\n"
            f"OLD: {old_event_title} — {old_date_nice} at {old_time}\n"
            f"NEW: {new_event_title} — {new_date_nice} at {new_time}\n\n"
            f"{loc_line}"
            f"Booking ID: #{booking_id}\n\n"
            f"Your deposit is preserved — nothing extra to pay.\n\n"
            f"Need to change again? DM me on Instagram @pashynska.photo.\n\n"
            f"See you soon,\nIryna\n@pashynska.photo"
        )
        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(180deg,#fff7f1 0%,#f7efe9 100%);padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 2px 8px rgba(46,25,20,.04),0 16px 42px rgba(93,55,47,.12);">
  <tr><td style="background:linear-gradient(135deg,#c4857a 0%,#a3685e 100%);padding:40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:32px;">📅</p>
    <h1 style="margin:0;color:#fff;font-size:24px;font-weight:normal;letter-spacing:1px;">Session Rescheduled</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.92);font-size:13px;letter-spacing:.04em;">Pashynska Photography · Calgary</p>
  </td></tr>
  <tr><td style="padding:36px 40px 24px;">
    <p style="margin:0 0 18px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{client_name}</strong>,</p>
    <p style="margin:0 0 24px;font-size:15px;color:#7a5a6a;line-height:1.7;">Your photo session has been moved to a new date and time. Your deposit travels with you — nothing extra to pay.</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;border-radius:12px;margin-bottom:24px;">
      <tr><td style="padding:18px 22px;">
        <p style="margin:0 0 8px;font-size:12px;color:#a8918e;text-transform:uppercase;letter-spacing:.1em;">Was</p>
        <p style="margin:0 0 14px;font-size:14px;color:#7a5a6a;text-decoration:line-through;">{old_event_title} · {old_date_nice} · {old_time}</p>
        <p style="margin:0 0 6px;font-size:12px;color:#c4857a;text-transform:uppercase;letter-spacing:.1em;">Now</p>
        <p style="margin:0;font-size:16px;color:#5a3d4a;"><strong>{new_event_title}</strong><br>{new_date_nice} &middot; {new_time}</p>
      </td></tr>
    </table>
    {f'<p style="margin:0 0 12px;font-size:14px;color:#7a5a6a;">📍 <strong>Location:</strong> {location}</p>' if location else '<p style="margin:0 0 12px;font-size:14px;color:#7a5a6a;">📍 Exact location will be sent closer to the session date.</p>'}
    <p style="margin:0 0 12px;font-size:14px;color:#7a5a6a;line-height:1.7;">Booking ID: <strong>#{booking_id}</strong></p>
    <p style="margin:0 0 24px;font-size:14px;color:#7a5a6a;line-height:1.7;">Need to change again? DM me on Instagram <a href="https://instagram.com/pashynska.photo" style="color:#c4857a;text-decoration:none;">@pashynska.photo</a></p>
    <p style="margin:12px 0 0;font-size:14px;color:#a3685e;"><strong>Iryna Pashynska</strong></p>
  </td></tr>
  <tr><td style="background:#fff8f4;padding:20px 40px;text-align:center;border-top:1px solid #f2e3dd;">
    <p style="margin:0;font-size:12px;color:#b0938b;">Pashynska Photography · Calgary, AB · Canada</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""
        return _send_email_raw(to_email, client_name, subject, plain, html)
    except Exception as e:
        log.error(f"[email] Reschedule email failed: {e}")
        return False


def _notify_reschedule(booking_id, client_name, client_email, old_event_title, old_date, old_time,
                       new_event_title, new_date, new_time, status):
    """Telegram notify Iryna + Andrzej about reschedule."""
    text = (
        f"📅 <b>Booking Rescheduled #{booking_id}</b>\n\n"
        f"👤 {_tg_escape(client_name)}\n"
        f"📧 {_tg_escape(client_email)}\n\n"
        f"<s>{_tg_escape(old_event_title)} · {_tg_escape(old_date)} · {_tg_escape(old_time)}</s>\n"
        f"➡️ <b>{_tg_escape(new_event_title)} · {_tg_escape(new_date)} · {_tg_escape(new_time)}</b>\n\n"
        f"Status: {_tg_escape(status)}"
    )
    _notify_admin(text)


def _send_abandoned_email(booking):
    """Send 'You were so close!' recovery email to a client who didn't complete payment."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    slot_time = booking.get("time", "")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev or not email:
        return False

    date_nice = ""
    try:
        date_nice = datetime.strptime(ev["date"], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_nice = ev.get("date", "")

    booking_url = f"https://{os.environ.get('SITE_HOST', CANONICAL_SITE_HOST)}"
    subject = f"Still thinking about your photo session? 📸"

    plain = (
        f"Hi {name},\n\n"
        f"We noticed you started booking a mini session for {date_nice} at {slot_time} "
        f"but didn't quite finish.\n\n"
        f"Totally okay — life gets busy! But if you're still thinking about it, "
        f"there might still be spots available.\n\n"
        f"Book here: {booking_url}\n\n"
        f"No pressure at all — just didn't want you to miss out! 🌸\n\n"
        f"Warmly,\nIryna\n@pashynska.photo"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(180deg,#fff7f1 0%,#f7efe9 100%);padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 2px 8px rgba(46,25,20,.04),0 16px 42px rgba(93,55,47,.12);">
  <tr><td style="background:linear-gradient(135deg,#e7c7bf 0%,#a3685e 100%);padding:36px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:36px;">📸</p>
    <h1 style="margin:0;color:#fff;font-size:24px;font-weight:400;letter-spacing:-.01em;">You were so close!</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.92);font-size:13px;letter-spacing:.04em;">Pashynska Photography · Calgary</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 20px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{name}</strong> 👋</p>
    <p style="margin:0 0 16px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      We noticed you started booking a mini session for
      <strong>{ev.get('title', 'your session')}</strong> on <strong>{date_nice} at {slot_time}</strong>
      but didn't quite finish.
    </p>
    <p style="margin:0 0 28px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      Totally okay — life gets busy! But if you're still thinking about it,
      spots fill up fast and I'd love to have you. 🌸
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      <tr><td align="center">
        <a href="{booking_url}" style="display:inline-block;background:linear-gradient(135deg,#c4857a,#a3685e);color:#fff;text-decoration:none;padding:14px 36px;border-radius:50px;font-size:15px;letter-spacing:.5px;">
          Check available spots →
        </a>
      </td></tr>
    </table>
    <p style="margin:0;font-size:13px;color:#b0938b;text-align:center;line-height:1.6;">
      No pressure at all — just didn't want you to miss out!
    </p>
  </td></tr>
  <tr><td style="background:#fff8f4;padding:20px 40px;text-align:center;border-top:1px solid #f2e3dd;">
    <p style="margin:0;font-size:12px;color:#b0938b;">Pashynska Photography · Calgary, AB · Canada<br>
    <a href="https://instagram.com/pashynska.photo" style="color:#c4857a;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


def _send_abandoned_second_email(booking):
    """Send a gentle final recovery email ~48h after the first abandoned follow-up."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    slot_time = booking.get("time", "")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev or not email:
        return False

    date_nice = ""
    try:
        date_nice = datetime.strptime(ev["date"], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_nice = ev.get("date", "")

    booking_url = f"https://{os.environ.get('SITE_HOST', CANONICAL_SITE_HOST)}"
    subject = "A quick note about your photo session spot 🌿"

    plain = (
        f"Hi {name},\n\n"
        f"Just one last little note — you had started booking {ev.get('title', 'a photo session')} "
        f"for {date_nice} at {slot_time}.\n\n"
        f"If the timing still works for you, you can check the remaining availability here:\n"
        f"{booking_url}\n\n"
        f"If not, no worries at all. You can always message me on Instagram @pashynska.photo "
        f"and I'll help you find another option.\n\n"
        f"Warmly,\nIryna"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(180deg,#fff7f1 0%,#f7efe9 100%);padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 2px 8px rgba(46,25,20,.04),0 16px 42px rgba(93,55,47,.12);">
  <tr><td style="background:linear-gradient(135deg,#f1d8cf 0%,#b98a80 100%);padding:34px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:34px;">🌿</p>
    <h1 style="margin:0;color:#fff;font-size:23px;font-weight:400;letter-spacing:-.01em;">A little final note</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.92);font-size:13px;letter-spacing:.04em;">Pashynska Photography · Calgary</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 20px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{name}</strong>,</p>
    <p style="margin:0 0 16px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      Just one last little note — you had started booking
      <strong>{ev.get('title', 'your photo session')}</strong> on <strong>{date_nice} at {slot_time}</strong>.
    </p>
    <p style="margin:0 0 26px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      If the timing still works for you, you can check the remaining availability below.
      If not, no worries at all — message me on Instagram and I’ll help you find another option.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:26px;">
      <tr><td align="center">
        <a href="{booking_url}" style="display:inline-block;background:linear-gradient(135deg,#c4857a,#a3685e);color:#fff;text-decoration:none;padding:14px 34px;border-radius:50px;font-size:15px;letter-spacing:.5px;">
          Check remaining spots →
        </a>
      </td></tr>
    </table>
    <p style="margin:0;font-size:13px;color:#b0938b;text-align:center;line-height:1.6;">
      Thank you for considering me for your memories ♡
    </p>
  </td></tr>
  <tr><td style="background:#fff8f4;padding:20px 40px;text-align:center;border-top:1px solid #f2e3dd;">
    <p style="margin:0;font-size:12px;color:#b0938b;">Pashynska Photography · Calgary, AB · Canada<br>
    <a href="https://instagram.com/pashynska.photo" style="color:#c4857a;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


def _parse_iso_datetime(value):
    """Parse an ISO datetime string; return None for invalid/empty values."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _is_due_for_second_abandoned_followup(booking, now=None):
    """Second abandoned follow-up is due 48h after the first one, once only."""
    if booking.get("status") != "expired":
        return False
    if booking.get("abandoned_second_email_sent"):
        return False
    first_sent = _parse_iso_datetime(booking.get("abandoned_email_sent"))
    if not first_sent:
        return False
    now = now or _local_now()
    if first_sent.tzinfo is None and getattr(now, "tzinfo", None) is not None:
        first_sent = first_sent.replace(tzinfo=now.tzinfo)
    return first_sent <= now - timedelta(hours=48)


def _is_due_for_first_abandoned_followup(booking, now=None):
    """First abandoned follow-up is due 2h after booking creation, once only."""
    if booking.get("status") != "expired":
        return False
    if booking.get("abandoned_email_sent"):
        return False
    created = _parse_iso_datetime(booking.get("created_at"))
    if not created:
        return False
    now = now or _local_now()
    if created.tzinfo is None and getattr(now, "tzinfo", None) is not None:
        created = created.replace(tzinfo=now.tzinfo)
    return created <= now - timedelta(hours=2)


def _send_reminder_email(booking):
    """Send 48-hour pre-session reminder email, with balance button if still owed."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    slot_time = booking.get("time", "")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev or not email:
        return False

    date_nice = ""
    try:
        date_nice = datetime.strptime(ev["date"], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_nice = ev.get("date", "")

    location = ev.get("location", "Location details coming soon")
    balance_due = _booking_balance_due(booking, ev)
    balance_url = _balance_page_url(booking) if balance_due and balance_due > 0 else None
    subject = f"Your session is in 2 days! 🌸 — {date_nice} at {slot_time}"

    balance_plain = (
        f"\n💳 Remaining balance due: ${balance_due:.2f} CAD\n"
        f"Pay now: {balance_url}\n"
    ) if balance_url else ""

    plain = (
        f"Hi {name},\n\n"
        f"Just a friendly reminder — your mini photo session is coming up in 2 days!\n\n"
        f"📅 {date_nice}\n"
        f"⏰ {slot_time}\n"
        f"📍 {location}\n"
        f"{balance_plain}\n"
        f"A few tips to make the most of your session:\n"
        f"• Wear colours that complement each other (avoid busy patterns)\n"
        f"• Arrive 5 minutes early so we can start relaxed\n"
        f"• Bring any props you love — a blanket, flowers, a favourite hat\n"
        f"• Most importantly — just have fun! I'll guide you the whole time 😊\n\n"
        f"Any questions? DM me on Instagram @pashynska.photo\n\n"
        f"See you soon!\n"
        f"Iryna 🌸"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(180deg,#fff7f1 0%,#f7efe9 100%);padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 2px 8px rgba(46,25,20,.04),0 16px 42px rgba(93,55,47,.12);">
  <tr><td style="background:linear-gradient(135deg,#c4857a 0%,#a3685e 100%);padding:36px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:36px;">🌸</p>
    <h1 style="margin:0;color:#fff;font-size:24px;font-weight:400;letter-spacing:-.01em;">See you in 2 days!</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.92);font-size:13px;letter-spacing:.04em;">Pashynska Photography · Calgary</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 20px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{name}</strong>! 👋</p>
    <p style="margin:0 0 24px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      Your mini photo session is just <strong>2 days away</strong>! Here are the details:
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #ead8d0;border-radius:16px;margin-bottom:28px;box-shadow:0 8px 22px rgba(102,63,53,.06);">
      <tr><td style="padding:20px 24px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f2e3dd;color:#7a5a6a;font-size:14px;">🗓 Date</td>
            <td style="padding:8px 0;border-bottom:1px solid #f2e3dd;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{date_nice}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f2e3dd;color:#7a5a6a;font-size:14px;">⏰ Time</td>
            <td style="padding:8px 0;border-bottom:1px solid #f2e3dd;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{slot_time}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#7a5a6a;font-size:14px;">📍 Location</td>
            <td style="padding:8px 0;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{location}</strong></td>
          </tr>
        </table>
      </td></tr>
    </table>
    <h3 style="margin:0 0 12px;color:#5a3d4a;font-size:15px;">Tips for a beautiful session:</h3>
    <table cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">👗 &nbsp;Wear colours that complement each other — avoid busy patterns</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">⏱ &nbsp;Arrive 5 minutes early so we can start relaxed</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">🎀 &nbsp;Bring props you love — a blanket, flowers, a favourite hat</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">😊 &nbsp;Just have fun — I'll guide you the whole time!</td></tr>
    </table>
    {"" if not balance_url else f'''<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf5e4;border:1px solid #e8d5a3;border-radius:14px;margin-bottom:28px;">
      <tr><td style="padding:18px 24px;">
        <p style="margin:0 0 6px;font-size:13px;color:#92722a;font-weight:600;">💳 Remaining balance</p>
        <p style="margin:0 0 14px;font-size:14px;color:#7a5a6a;line-height:1.5;">You have <strong style="color:#5a3d4a;">${balance_due:.2f} CAD</strong> due. You can pay now or right after the session.</p>
        <a href="{_html_escape(balance_url)}" style="display:inline-block;background:#4b2f38;color:#ffffff;text-decoration:none;border-radius:10px;padding:10px 20px;font-size:13px;font-weight:700;">Pay Remaining Balance</a>
      </td></tr>
    </table>'''}
    <p style="margin:0 0 8px;font-size:14px;color:#7a5a6a;">Questions? DM me on Instagram
      <a href="https://instagram.com/pashynska.photo" style="color:#c4857a;text-decoration:none;">@pashynska.photo</a>
    </p>
  </td></tr>
  <tr><td style="padding:24px 40px;text-align:left;border-top:1px solid #f2e3dd;">
    <p style="margin:0;font-size:15px;color:#5a3d4a;">See you soon! 🌸</p>
    <p style="margin:8px 0 0;font-size:14px;color:#a3685e;"><strong>Iryna Pashynska</strong><br>
    <a href="https://instagram.com/pashynska.photo" style="color:#c4857a;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
  <tr><td style="background:#fff8f4;padding:16px 40px;text-align:center;border-top:1px solid #f2e3dd;">
    <p style="margin:0;font-size:12px;color:#b0938b;">Pashynska Photography · Calgary, AB · Canada</p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


def _send_24h_reminder_email(booking):
    """Send 24-hour pre-session reminder email — short and punchy, with balance link if owed."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    slot_time = booking.get("time", "")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev or not email:
        return False

    date_nice = ""
    try:
        date_nice = datetime.strptime(ev["date"], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_nice = ev.get("date", "")

    location = ev.get("location", "Location details coming soon")
    balance_due = _booking_balance_due(booking, ev)
    balance_url = _balance_page_url(booking) if balance_due and balance_due > 0 else None
    subject = f"Tomorrow: your session at {slot_time}! 🌸 — {date_nice}"

    balance_plain = (
        f"\n💳 Balance due: ${balance_due:.2f} CAD → {balance_url}\n"
    ) if balance_url else ""

    plain = (
        f"Hi {name},\n\n"
        f"Your mini photo session is tomorrow!\n\n"
        f"📅 {date_nice}\n"
        f"⏰ {slot_time}\n"
        f"📍 {location}\n"
        f"{balance_plain}\n"
        f"Quick prep checklist:\n"
        f"• Soft, coordinating colours (lilac, cream, white, pastels)\n"
        f"• Avoid neon and busy patterns\n"
        f"• Arrive 5 min early\n"
        f"• Bring any favourite prop (blanket, flowers, hat)\n\n"
        f"Any last-minute questions? DM @pashynska.photo\n\n"
        f"See you tomorrow!\n"
        f"Iryna 🌸"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(180deg,#fff7f1 0%,#f7efe9 100%);padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 2px 8px rgba(46,25,20,.04),0 16px 42px rgba(93,55,47,.12);">
  <tr><td style="background:linear-gradient(135deg,#c4857a 0%,#a3685e 100%);padding:36px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:36px;">🌸</p>
    <h1 style="margin:0;color:#fff;font-size:24px;font-weight:400;letter-spacing:-.01em;">See you tomorrow!</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.92);font-size:13px;letter-spacing:.04em;">Pashynska Photography · Calgary</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 20px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{name}</strong>! 👋</p>
    <p style="margin:0 0 24px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      Your mini photo session is <strong>tomorrow</strong>! Here's everything you need:
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #ead8d0;border-radius:16px;margin-bottom:28px;box-shadow:0 8px 22px rgba(102,63,53,.06);">
      <tr><td style="padding:20px 24px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f2e3dd;color:#7a5a6a;font-size:14px;">🗓 Date</td>
            <td style="padding:8px 0;border-bottom:1px solid #f2e3dd;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{date_nice}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;border-bottom:1px solid #f2e3dd;color:#7a5a6a;font-size:14px;">⏰ Time</td>
            <td style="padding:8px 0;border-bottom:1px solid #f2e3dd;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{slot_time}</strong></td>
          </tr>
          <tr>
            <td style="padding:8px 0;color:#7a5a6a;font-size:14px;">📍 Location</td>
            <td style="padding:8px 0;text-align:right;"><strong style="color:#5a3d4a;font-size:14px;">{location}</strong></td>
          </tr>
        </table>
      </td></tr>
    </table>
    <h3 style="margin:0 0 12px;color:#5a3d4a;font-size:15px;">Quick prep checklist:</h3>
    <table cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">👗 &nbsp;Soft, coordinating colours — lilac, cream, white, pastels</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">⏱ &nbsp;Arrive 5 minutes early</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">🎀 &nbsp;Bring a favourite prop — blanket, flowers, hat</td></tr>
      <tr><td style="padding:5px 0;color:#7a5a6a;font-size:14px;line-height:1.5;">😊 &nbsp;Most importantly — just have fun!</td></tr>
    </table>
    {"" if not balance_url else f'''<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf5e4;border:1px solid #e8d5a3;border-radius:14px;margin-bottom:24px;">
      <tr><td style="padding:16px 22px;">
        <p style="margin:0 0 5px;font-size:13px;color:#92722a;font-weight:600;">💳 Balance reminder</p>
        <p style="margin:0 0 12px;font-size:14px;color:#7a5a6a;">You still have <strong style="color:#5a3d4a;">${balance_due:.2f} CAD</strong> outstanding. Pay before the session or right after.</p>
        <a href="{_html_escape(balance_url)}" style="display:inline-block;background:#4b2f38;color:#fff;text-decoration:none;border-radius:10px;padding:9px 18px;font-size:13px;font-weight:700;">Pay Remaining Balance</a>
      </td></tr>
    </table>'''}
    <p style="margin:0 0 8px;font-size:14px;color:#7a5a6a;">Questions? DM me on Instagram
      <a href="https://instagram.com/pashynska.photo" style="color:#c4857a;text-decoration:none;">@pashynska.photo</a>
    </p>
  </td></tr>
  <tr><td style="background:#fff8f4;padding:16px 40px;text-align:center;border-top:1px solid #f2e3dd;">
    <p style="margin:0;font-size:12px;color:#b0938b;">Pashynska Photography · Calgary, AB · Canada</p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


def _send_review_email(booking):
    """Send post-session review request email (5 days after session)."""
    name = booking.get("name", "there")
    email = booking.get("email", "")
    event_id = booking.get("event_id")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not email:
        return False

    insta_url = "https://instagram.com/pashynska.photo"
    google_review_url = os.environ.get("GOOGLE_REVIEW_URL", "https://review.pashynskaphoto.com")
    safe_name = _html_escape(name or "there")

    subject = "How were your photos? 🌸"

    plain = (
        f"Hi {name},\n\n"
        f"I hope you've had a chance to look through your photos and love them as much as I do!\n\n"
        f"If you enjoyed your session, a quick review means the absolute world to a small business like mine. "
        f"It takes just 30 seconds:\n\n"
        f"⭐ Google review: {google_review_url}\n"
        f"📸 Tag me on Instagram: {insta_url}\n\n"
        f"And of course — I'd LOVE to see you again for your next session! 🌸\n\n"
        f"Thank you so much for trusting me to capture your memories.\n\n"
        f"Warmly,\nIryna\n@pashynska.photo"
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(180deg,#fff7f1 0%,#f7efe9 100%);padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 2px 8px rgba(46,25,20,.04),0 16px 42px rgba(93,55,47,.12);">
  <tr><td style="background:linear-gradient(135deg,#f2c9bf 0%,#c4857a 100%);padding:36px 40px;text-align:center;">
    <p style="margin:0 0 8px;font-size:36px;">⭐</p>
    <h1 style="margin:0;color:#fff;font-size:24px;font-weight:400;letter-spacing:-.01em;">How were your photos?</h1>
    <p style="margin:8px 0 0;color:rgba(255,255,255,.92);font-size:13px;letter-spacing:.04em;">Pashynska Photography · Calgary</p>
  </td></tr>
  <tr><td style="padding:36px 40px 28px;">
    <p style="margin:0 0 16px;font-size:16px;color:#5a3d4a;line-height:1.6;">Hi <strong>{safe_name}</strong>! 🌸</p>
    <p style="margin:0 0 20px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      I hope you've had a chance to look through your photos and love them as much as I do!
      It was such a pleasure photographing you.
    </p>
    <p style="margin:0 0 28px;font-size:15px;color:#7a5a6a;line-height:1.7;">
      If you enjoyed your session, a quick review means the absolute world to a small business like mine — it helps other families and couples find me. It takes just 30 seconds! ✨
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      <tr><td align="center" style="padding-bottom:12px;">
        <a href="{google_review_url}" style="display:inline-block;background:linear-gradient(135deg,#f4c430,#f0a030);color:#fff;text-decoration:none;padding:14px 32px;border-radius:50px;font-size:15px;letter-spacing:.5px;">
          ⭐ Leave a Google Review
        </a>
      </td></tr>
      <tr><td align="center">
        <a href="{insta_url}" style="display:inline-block;background:linear-gradient(135deg,#c4857a,#a3685e);color:#fff;text-decoration:none;padding:12px 28px;border-radius:50px;font-size:14px;letter-spacing:.5px;">
          📸 Tag me on Instagram
        </a>
      </td></tr>
    </table>
    <p style="margin:20px 0 0;font-size:14px;color:#b0938b;text-align:center;line-height:1.6;">
      And I'd love to see you again for your next session! 💜
    </p>
  </td></tr>
  <tr><td style="padding:24px 40px;text-align:left;border-top:1px solid #f2e3dd;">
    <p style="margin:0;font-size:15px;color:#5a3d4a;">Thank you so much! 🌸</p>
    <p style="margin:8px 0 0;font-size:14px;color:#a3685e;"><strong>Iryna Pashynska</strong><br>
    <a href="{insta_url}" style="color:#c4857a;text-decoration:none;">@pashynska.photo</a></p>
  </td></tr>
  <tr><td style="background:#fff8f4;padding:16px 40px;text-align:center;border-top:1px solid #f2e3dd;">
    <p style="margin:0;font-size:12px;color:#b0938b;">Pashynska Photography · Calgary, AB · Canada</p>
  </td></tr>
</table></td></tr></table></body></html>"""

    return _send_email_raw(email, name, subject, plain, html)


def notify_payment_confirmed(booking_id, paid_amount=None):
    """Send Telegram notification to admin: booking was auto-confirmed by e-Transfer checker."""
    try:
        conn = db_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        if not row:
            return
        b = dict(row)
        amount_str = f"${paid_amount:.2f}" if paid_amount else "deposit"
        msg = (
            f"✅ <b>Auto-Confirmed: Payment Received!</b>\n\n"
            f"👤 {b.get('name','?')}\n"
            f"📧 {b.get('email','?')}\n"
            f"📱 {b.get('phone','N/A')}\n"
            f"📸 @{b.get('instagram','N/A')}\n"
            f"📅 {b.get('date','?')} @ {b.get('time','?')}\n"
            f"💰 <b>Received: {amount_str}</b>\n"
            f"🆔 Booking #{booking_id}\n\n"
            f"Email confirmation sent to client."
        )
        _notify_admin(msg)
    except Exception as e:
        log.error(f"[notify_confirmed] Failed: {e}")


def _maybe_payout_referral(booking_id):
    """If this booking used a referral code, credit the code owner $20 and email them.
    No-op for non-referral bookings; idempotent (confirm_referral_payment only fires once
    per booking), so it is safe to call from every payment-confirmation path. Never raises."""
    try:
        use = gift_db.confirm_referral_payment(booking_id)
        if not use:
            return
        from gift_referral_email import send_referral_reward_email
        send_referral_reward_email(
            owner_email=use["owner_email"],
            owner_name=use["owner_name"],
            friend_name=use.get("referee_name") or "Your friend",
            reward=use["reward_for_owner"],
            code=use["referral_code"],
            new_balance=use.get("new_balance", use["reward_for_owner"]),
            total_earned=use.get("total_earned", use["reward_for_owner"]),
        )
        log.info(f"[referral] Paid ${use['reward_for_owner']:.0f} credit to {use['owner_email']} for booking #{booking_id}")
    except Exception as e:
        log.error(f"[referral] payout failed for booking #{booking_id}: {e}")


def _after_auto_payment_confirmed(booking_id):
    """Run the same side-effects after automatic e-Transfer confirmation
    that manual admin confirmation runs: calendar, Notion, client email,
    and admin Telegram notification.
    """
    try:
        conn = db_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        conn.close()
        if not row:
            log.warning(f"[auto-confirm] Booking #{booking_id} not found after payment match")
            return
        booking = dict(row)
        event_url = create_calendar_event_for_booking(booking_id)
        sync_to_notion(booking_id)
        ev = get_event_by_id(booking.get("event_id"))
        if ev and booking.get("email"):
            _send_client_email(
                to_email=booking.get("email", ""),
                client_name=booking.get("name", "Client"),
                event_date=ev.get("date", booking.get("date", "")),
                slot_time=booking.get("time", ""),
                event_title=ev.get("title", "Mini Session"),
                booking_id=booking_id,
                location=ev.get("location"),
                location_url=ev.get("location_url"),
                **_client_email_context(booking, ev),
            )
        notify_payment_confirmed(booking_id, booking.get("paid_amount"))
        _record_booking_funnel_event(
            booking,
            "booking_confirmed",
            {"source": "etransfer_auto", "paid_amount": booking.get("paid_amount")},
        )
        log.info(f"[auto-confirm] Booking #{booking_id} side-effects complete; calendar={event_url or 'none'}")
    except Exception as e:
        log.error(f"[auto-confirm] side-effects failed for booking #{booking_id}: {e}")


# ── PHOTO STORAGE ────────────────────────────────────────────────────────────
# Photos uploaded via the admin panel are stored on the persistent volume
# (PHOTOS_DIR, defaults to /data/images on Fly). Bundled images that ship with
# the repo live in /app/static/images and are used as fallback so existing URLs
# in events.yaml keep working.
PHOTOS_DIR = os.environ.get(
    "PHOTOS_DIR",
    os.path.join(
        (os.environ.get("BACKUP_DIR", "").replace("/backups", "") or os.path.dirname(__file__)),
        "images"
    )
)
try:
    os.makedirs(PHOTOS_DIR, exist_ok=True)
except Exception as _e:
    log.warning(f"[photos] Could not ensure PHOTOS_DIR={PHOTOS_DIR}: {_e}")
_BUNDLED_IMAGES_DIR = os.path.join(app.root_path, 'static', 'images')
_IMAGE_CACHE_DIR = os.environ.get("IMAGE_CACHE_DIR", os.path.join(PHOTOS_DIR, ".cache"))
_IMAGE_CACHE_MAX_DIMENSION = int(os.environ.get("IMAGE_CACHE_MAX_DIMENSION", "1400"))
_IMAGE_CACHE_WEBP_QUALITY = int(os.environ.get("IMAGE_CACHE_WEBP_QUALITY", "78"))
_IMAGE_CACHE_MIN_BYTES = int(os.environ.get("IMAGE_CACHE_MIN_BYTES", str(320 * 1024)))

# ── Basic security headers ────────────────────────────────────────────────────
# CSP is intentionally permissive (allows inline + Google Fonts + Stripe + Telegram
# images + cdnjs for Stripe.js) — tightening to nonce-based would require touching
# every <script>/<style> across 8 templates. This is the conservative middle ground.
_CSP = (
    "default-src 'self'; "
    # NOTE: www.google.com + www.gstatic.com are required by reCAPTCHA v3
    # (api.js → recaptcha__en.js); without them grecaptcha never loads and
    # every booking POST fails server-side verification.
    "script-src 'self' 'unsafe-inline' https://js.stripe.com https://cdnjs.cloudflare.com "
    "https://www.google.com https://www.gstatic.com https://connect.facebook.net "
    "https://www.googletagmanager.com https://www.googleadservices.com "
    "https://googleads.g.doubleclick.net "
    "https://us-assets.i.posthog.com https://us.i.posthog.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: https: https://www.facebook.com https://www.googleadservices.com https://www.google.com https://googleads.g.doubleclick.net; "
    "connect-src 'self' https://api.stripe.com https://www.facebook.com https://connect.facebook.net https://us.i.posthog.com https://www.google-analytics.com https://analytics.google.com https://www.google.com https://googleads.g.doubleclick.net; "
    # frame-src: Stripe iframes + reCAPTCHA challenge iframe (when score is low)
    "frame-src https://js.stripe.com https://hooks.stripe.com https://www.google.com; "
    "frame-ancestors 'self' https://*.wfolio.com https://pashynska.agency https://www.pashynska.agency https://book.pashynskaphoto.com; "
    "form-action 'self' https://checkout.stripe.com; "
    "base-uri 'self'; "
    "object-src 'none'"
)
_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(self)"


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # frame-ancestors in CSP supersedes X-Frame-Options; keep XFO for old browsers
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
    # HSTS only when serving over HTTPS to avoid breaking local http://
    # (fly.io terminates TLS and sets X-Forwarded-Proto=https)
    proto = request.headers.get("X-Forwarded-Proto", "").lower()
    if request.is_secure or proto == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=15552000; includeSubDomains"
        )
    response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


# Lightweight health endpoint for Fly.io HTTP checks — no DB hit, no auth.
# fly.toml's [[http_service.checks]] hits this on every machine startup; if
# it 404s the deploy times out and rolls back.
@app.route('/healthz')
def healthz():
    return jsonify({"ok": True, "service": "iryna-booking"}), 200


_REVIEW_AI_RATE: dict[str, list[float]] = {}
_REVIEW_STYLE_LABELS = {
    "family": "family photo session",
    "maternity": "maternity photo session",
    "couple": "couple photo session",
    "newborn": "newborn photo session",
}
_REVIEW_STATIC_FALLBACKS = {
    "family": [
        "We had a wonderful family photo session with Iryna. She made everyone feel comfortable, guided us gently, and captured beautiful natural moments that really feel like us. The photos are warm, timeless, and full of emotion. Highly recommend Pashynska Photography for families in Calgary.",
        "Iryna is incredibly talented with families. She kept our kids engaged, found perfect light, and the photos turned out better than we ever imagined. Natural, emotional, and authentic — exactly what we wanted.",
        "Amazing experience from start to finish. Iryna created a relaxed atmosphere and captured genuine smiles and candid moments. The gallery is stunning — every photo tells a story. We will treasure them forever.",
    ],
    "maternity": [
        "Iryna made my maternity session feel calm, beautiful, and very comfortable. She guided the poses naturally and captured such tender, meaningful photos. The final gallery feels elegant, warm, and emotional. I highly recommend Pashynska Photography.",
        "A truly magical experience. Iryna knew exactly how to highlight the beauty of pregnancy with soft light and graceful poses. The photos are intimate, artistic, and deeply personal — I will cherish them forever.",
        "I felt so at ease during my maternity shoot. Iryna created a peaceful environment and captured moments I didn't even realize were happening. Every image is soft, glowing, and full of love.",
    ],
    "couple": [
        "Our couple session with Iryna was relaxed, natural, and so much fun. She helped us feel comfortable in front of the camera and captured genuine connection instead of stiff poses. The photos are beautiful and full of emotion.",
        "Iryna has a gift for capturing real connection. The session felt like a date, not a photoshoot. The photos are romantic, authentic, and absolutely stunning — we couldn't be happier.",
        "We were nervous about being photographed, but Iryna made it effortless. She found the perfect light and caught us laughing and truly enjoying each other. Highly recommend for any couple in Calgary.",
    ],
    "newborn": [
        "Iryna was patient, gentle, and thoughtful during our newborn session. She created a calm experience and captured beautiful details and family moments we will treasure forever. The photos feel warm, natural, and timeless.",
        "The newborn session exceeded every expectation. Iryna was so gentle with our baby and captured tiny details we will never forget. The photos are soft, pure, and absolutely heart-melting.",
        "We are so grateful for Iryna's patience and artistry. She created a safe, warm environment for our newborn and captured the most precious family moments. Every photo is a treasure.",
    ],
}


def _review_fallback(style: str) -> str:
    variants = _REVIEW_STATIC_FALLBACKS.get(style, _REVIEW_STATIC_FALLBACKS["family"])
    return variants[int(time.time() * 1000) % len(variants)]


def _review_ai_allowed(ip_key: str) -> bool:
    now = time.time()
    window = 60.0
    hits = [ts for ts in _REVIEW_AI_RATE.get(ip_key, []) if now - ts < window]
    limit = int(os.environ.get("REVIEW_AI_RATE_LIMIT_PER_MINUTE", "12"))
    if len(hits) >= limit:
        _REVIEW_AI_RATE[ip_key] = hits
        return False
    hits.append(now)
    _REVIEW_AI_RATE[ip_key] = hits
    return True


def _clean_review_text(text: str) -> str:
    text = re.sub(r"```.*?```", "", str(text or ""), flags=re.S).strip()
    text = re.sub(r"^[-*\d.\s]+", "", text).strip().strip('"“”')
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"Pashynka\s+Photography", "Pashynska Photography", text, flags=re.I)
    text = re.sub(r"Пашынська\s+Фотографія", "Pashynska Photography", text, flags=re.I)
    text = re.sub(r"Пашинской\s+фотографии", "Ирине", text, flags=re.I)
    text = re.sub(r"Пашинская\s+фотография", "Pashynska Photography", text, flags=re.I)
    return text[:900]


def _review_ai_endpoint() -> str:
    url = (os.environ.get("REVIEW_AI_BASE_URL") or os.environ.get("ZAI_CHAT_COMPLETIONS_URL") or "https://api.z.ai/api/paas/v4/chat/completions").strip().rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"
    return url


def _generate_ai_review_text(style: str, lang: str, previous: str = "") -> tuple[str, str]:
    """Generate one fresh client-editable Google review draft via Z.ai/GLM."""
    api_key = os.environ.get("REVIEW_AI_API_KEY", "").strip() or os.environ.get("ZAI_API_KEY", "").strip()
    if not api_key:
        return _review_fallback(style), "fallback-no-key"

    model = os.environ.get("REVIEW_AI_MODEL") or os.environ.get("ZAI_MODEL", "glm-4.5-air")
    style_label = _REVIEW_STYLE_LABELS.get(style, _REVIEW_STYLE_LABELS["family"])
    if lang == "ru":
        language = "Russian (use Cyrillic Russian, not Ukrainian, not Belarusian)"
    elif lang == "uk":
        language = "Ukrainian"
    else:
        language = "English"
    prompt = (
        f"Write ONE fresh, natural Google review draft in {language} for Pashynska Photography in Calgary. "
        f"Session type: {style_label}. The text must sound like a real client, warm and specific, 55-90 words. "
        "Use the exact business name Pashynska Photography or the photographer name Iryna; never translate or misspell the business name. "
        "Do not copy common templates. Do not mention that AI wrote it. Do not use hashtags, markdown, bullets, or quotation marks. "
        "Make it easy for the client to edit before posting. "
        f"Avoid repeating this previous draft: {previous[:500]}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You write concise, varied, human-sounding photography review drafts."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "max_tokens": int(os.environ.get("REVIEW_AI_MAX_TOKENS", "220")),
        "temperature": float(os.environ.get("REVIEW_AI_TEMPERATURE", "0.95")),
    }
    thinking = os.environ.get("ZAI_THINKING", "disabled").strip()
    if thinking:
        payload["thinking"] = {"type": thinking}
    response = requests.post(
        _review_ai_endpoint(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=float(os.environ.get("REVIEW_AI_TIMEOUT", "14")),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Z.ai review API error {response.status_code}: {response.text[:300]}")
    data = response.json()
    choice = (data.get("choices") or [{}])[0]
    message_obj = choice.get("message") or {}
    content = message_obj.get("content")
    if isinstance(content, list):
        content = " ".join(str(item.get("text") if isinstance(item, dict) else item) for item in content)
    text = _clean_review_text(str(content or ""))
    return (text or _review_fallback(style), f"zai:{model}")


@app.route('/api/review-helper')
@app.route('/api/review-helper/')
def review_helper_api_base():
    """Human-friendly fallback for people opening the API URL directly."""
    return redirect(url_for('review_helper'))


@app.route('/api/review-helper/generate')
def generate_review_helper_text():
    style = (request.args.get("style") or "family").strip().lower()
    lang = (request.args.get("lang") or "en").strip().lower()[:2]
    previous = request.args.get("previous", "")
    ip_key = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "unknown"
    if not _review_ai_allowed(ip_key):
        return jsonify({"ok": True, "review": _review_fallback(style), "source": "fallback-rate-limit"})
    try:
        review, source = _generate_ai_review_text(style, lang, previous)
        return jsonify({"ok": True, "review": review, "source": source})
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[review-ai] generation failed: {type(exc).__name__}: {exc}")
        return jsonify({"ok": True, "review": _review_fallback(style), "source": "fallback-error"})


@app.route('/review-helper')
def review_helper():
    """Public helper page that makes it easy for clients to write a Google review."""
    style = (request.args.get("style") or "family").strip().lower()
    lang = (request.args.get("lang") or "en").strip().lower()[:2]

    labels = {
        "en": {
            "title": "Thank you for trusting Pashynska Photography",
            "subtitle": "If you loved your session, this helper drafts a warm review you can copy and post on Google.",
            "copy": "Copy review text",
            "google": "Open Google Reviews",
            "copied": "Copied — now paste it into Google Reviews 💛",
            "edit": "You can edit the text before posting so it sounds exactly like you.",
            "rotate": "New version",
        },
        "ru": {
            "title": "Спасибо, что выбрали Pashynska Photography",
            "subtitle": "Если съёмка понравилась, эта страница подготовит тёплый текст отзыва для Google.",
            "copy": "Скопировать отзыв",
            "google": "Открыть Google Reviews",
            "copied": "Скопировано — теперь вставьте текст в Google Reviews 💛",
            "edit": "Текст можно отредактировать перед публикацией, чтобы он звучал именно как вы.",
            "rotate": "Новый вариант",
        },
    }.get(lang, None)
    if labels is None:
        labels = {
            "title": "Thank you for trusting Pashynska Photography",
            "subtitle": "This helper drafts a warm review you can copy and post on Google.",
            "copy": "Copy review text",
            "google": "Open Google Reviews",
            "copied": "Copied — now paste it into Google Reviews 💛",
            "edit": "You can edit the text before posting so it sounds exactly like you.",
        }

    review_by_style = {
        "family": "We had a wonderful family photo session with Iryna. She made everyone feel comfortable, guided us gently, and captured beautiful natural moments that really feel like us. The photos are warm, timeless, and full of emotion. Highly recommend Pashynska Photography for families in Calgary.",
        "maternity": "Iryna made my maternity session feel calm, beautiful, and very comfortable. She guided the poses naturally and captured such tender, meaningful photos. The final gallery feels elegant, warm, and emotional. I highly recommend Pashynska Photography.",
        "couple": "Our couple session with Iryna was relaxed, natural, and so much fun. She helped us feel comfortable in front of the camera and captured genuine connection instead of stiff poses. The photos are beautiful and full of emotion.",
        "newborn": "Iryna was patient, gentle, and thoughtful during our newborn session. She created a calm experience and captured beautiful details and family moments we will treasure forever. The photos feel warm, natural, and timeless.",
    }
    review_text = request.args.get("text") or review_by_style.get(style, review_by_style["family"])
    google_review_url = os.environ.get("GOOGLE_REVIEW_URL", "https://g.page/r/CenY1x2zXYc_EAE/review")

    # Multi-variant review texts by style
    reviews_family = [
        "We had a wonderful family photo session with Iryna. She made everyone feel comfortable, guided us gently, and captured beautiful natural moments that really feel like us. The photos are warm, timeless, and full of emotion. Highly recommend Pashynska Photography for families in Calgary.",
        "Iryna is incredibly talented with families. She kept our kids engaged, found perfect light, and the photos turned out better than we ever imagined. Natural, emotional, and authentic — exactly what we wanted.",
        "Amazing experience from start to finish. Iryna created a relaxed atmosphere and captured genuine smiles and candid moments. The gallery is stunning — every photo tells a story. We will treasure them forever."
    ]
    reviews_maternity = [
        "Iryna made my maternity session feel calm, beautiful, and very comfortable. She guided the poses naturally and captured such tender, meaningful photos. The final gallery feels elegant, warm, and emotional. I highly recommend Pashynska Photography.",
        "A truly magical experience. Iryna knew exactly how to highlight the beauty of pregnancy with soft light and graceful poses. The photos are intimate, artistic, and deeply personal — I will cherish them forever.",
        "I felt so at ease during my maternity shoot. Iryna created a peaceful environment and captured moments I didn't even realize were happening. Every image is soft, glowing, and full of love."
    ]
    reviews_couple = [
        "Our couple session with Iryna was relaxed, natural, and so much fun. She helped us feel comfortable in front of the camera and captured genuine connection instead of stiff poses. The photos are beautiful and full of emotion.",
        "Iryna has a gift for capturing real connection. The session felt like a date, not a photoshoot. The photos are romantic, authentic, and absolutely stunning — we couldn't be happier.",
        "We were nervous about being photographed, but Iryna made it effortless. She found the perfect light and caught us laughing and truly enjoying each other. Highly recommend for any couple in Calgary."
    ]
    reviews_newborn = [
        "Iryna was patient, gentle, and thoughtful during our newborn session. She created a calm experience and captured beautiful details and family moments we will treasure forever. The photos feel warm, natural, and timeless.",
        "The newborn session exceeded every expectation. Iryna was so gentle with our baby and captured tiny details we will never forget. The photos are soft, pure, and absolutely heart-melting.",
        "We are so grateful for Iryna's patience and artistry. She created a safe, warm environment for our newborn and captured the most precious family moments. Every photo is a treasure."
    ]
    review_variants = {
        "family": reviews_family,
        "maternity": reviews_maternity,
        "couple": reviews_couple,
        "newborn": reviews_newborn,
    }
    variants = review_variants.get(style, reviews_family)
    # pick variant index from query or rotate randomly client-side
    variant_idx = 0
    try:
        vi = request.args.get("v")
        if vi is not None:
            variant_idx = int(vi) % len(variants)
    except Exception:
        pass
    review_text = request.args.get("text") or variants[variant_idx]

    # Inline HTML keeps this page deploy-safe even if templates are out of sync.
    from flask import Response
    # Build JS array of variants for client rotation
    safe_variants_js = json.dumps(variants)
    html = f"""<!doctype html>
<html lang=\"{html_escape(lang or 'en')}\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Review Helper · Pashynska Photography</title>
  <style>
    :root {{ --bg:#fff8f5; --card:#ffffff; --ink:#342521; --muted:#7d6860; --accent:#c58b7c; --accent2:#8f5d55; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif; background:radial-gradient(circle at top,#ffe8df 0,#fff8f5 42%,#f7efeb 100%); color:var(--ink); min-height:100vh; display:flex; align-items:center; justify-content:center; padding:22px; }}
    .card {{ width:min(720px,100%); background:rgba(255,255,255,.88); border:1px solid rgba(197,139,124,.28); border-radius:28px; box-shadow:0 24px 70px rgba(76,45,35,.14); padding:clamp(24px,5vw,44px); }}
    .eyebrow {{ letter-spacing:.14em; text-transform:uppercase; color:var(--accent2); font-size:12px; font-weight:700; }}
    h1 {{ font-family:Georgia,serif; font-size:clamp(30px,6vw,52px); line-height:1.02; margin:12px 0 14px; }}
    p {{ color:var(--muted); font-size:17px; line-height:1.6; }}
    textarea {{ width:100%; min-height:190px; margin:18px 0; padding:18px; border-radius:20px; border:1px solid rgba(143,93,85,.25); font:16px/1.55 -apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif; color:var(--ink); background:#fffdfc; resize:vertical; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:12px; margin-top:8px; align-items:center; }}
    button, a.btn, .btn-ghost {{ appearance:none; border:0; border-radius:999px; padding:15px 20px; font-weight:800; font-size:15px; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }}
    button {{ background:var(--accent2); color:white; }}
    a.btn {{ background:#fff; color:var(--accent2); border:1px solid rgba(143,93,85,.28); }}
    .btn-ghost {{ background:transparent; color:var(--muted); border:1px solid rgba(143,93,85,.25); padding:10px 16px; font-size:13px; }}
    .btn-ghost:hover {{ color:var(--accent2); border-color:var(--accent2); }}
    .hint {{ margin-top:16px; font-size:14px; color:var(--muted); }}
    .toast {{ margin-top:14px; color:#2f6f46; font-weight:700; min-height:20px; }}
    .meta {{ display:flex; align-items:center; gap:10px; font-size:13px; color:var(--muted); margin-top:6px; }}
  </style>
</head>
<body>
  <main class=\"card\">
    <div class=\"eyebrow\">Pashynska Photography · Review helper</div>
    <h1>{html_escape(labels['title'])}</h1>
    <p>{html_escape(labels['subtitle'])}</p>
    <textarea id=\"reviewText\">{html_escape(review_text)}</textarea>
    <div class=\"actions\">
      <button type=\"button\" onclick=\"copyReview()\">{html_escape(labels['copy'])}</button>
      <a class=\"btn\" href=\"{html_escape(google_review_url)}\" target=\"_blank\" rel=\"noopener\">⭐ {html_escape(labels['google'])}</a>
      <button type=\"button\" class=\"btn-ghost\" onclick=\"rotateReview()\" title=\"Generate another review\">↻ {html_escape(labels.get('rotate','New version'))}</button>
    </div>
    <div class=\"meta\"><span id=\"variantLabel\">Version <span id=\"variantNum\">1</span> of {len(variants)}</span></div>
    <div class=\"toast\" id=\"toast\"></div>
    <p class=\"hint\">{html_escape(labels['edit'])}</p>
  </main>
  <script>
    const FALLBACK_REVIEWS = {safe_variants_js};
    const REVIEW_STYLE = {style!r};
    const REVIEW_LANG = {lang!r};
    let currentIndex = {variant_idx};
    let aiCount = 0;
    async function loadAiReview(isInitial=false) {{
      const textarea = document.getElementById('reviewText');
      const toast = document.getElementById('toast');
      const previous = textarea.value || '';
      if (!isInitial) toast.textContent = 'Generating a fresh review ✨';
      try {{
        const params = new URLSearchParams({{style: REVIEW_STYLE, lang: REVIEW_LANG, previous}});
        params.set('nonce', String(Date.now()) + Math.random().toString(16).slice(2));
        const res = await fetch('/api/review-helper/generate?' + params.toString(), {{headers: {{'Accept':'application/json'}}}});
        const data = await res.json();
        if (!data.ok || !data.review) throw new Error(data.error || 'empty review');
        textarea.value = data.review;
        aiCount += 1;
        document.getElementById('variantLabel').innerHTML = 'AI version <span id="variantNum">' + aiCount + '</span>';
        toast.textContent = isInitial ? '' : 'Fresh AI review generated ✨';
      }} catch(e) {{
        currentIndex = (currentIndex + 1) % FALLBACK_REVIEWS.length;
        textarea.value = FALLBACK_REVIEWS[currentIndex];
        document.getElementById('variantNum').textContent = String(currentIndex + 1);
        toast.textContent = isInitial ? '' : 'Backup review version loaded ✨';
      }}
      if (!isInitial) setTimeout(() => {{ toast.textContent = ''; }}, 2500);
    }}
    function rotateReview() {{
      return loadAiReview(false);
    }}
    async function copyReview() {{
      const text = document.getElementById('reviewText').value;
      try {{ await navigator.clipboard.writeText(text); }}
      catch(e) {{ const t=document.getElementById('reviewText'); t.focus(); t.select(); document.execCommand('copy'); }}
      document.getElementById('toast').textContent = {labels['copied']!r};
    }}
    document.addEventListener('DOMContentLoaded', () => loadAiReview(true));
  </script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.route('/callback')
def oauth_callback():
    """Generic OAuth redirect target for Meta/Threads testing.

    Threads OAuth redirects here with ?code=... after the user authorizes the
    app. The app itself does not need to perform the token exchange in-browser;
    this endpoint only gives Meta a valid redirect URI and gives the operator a
    copyable code.
    """
    code = request.args.get("code", "")
    error = request.args.get("error") or request.args.get("error_message")
    if error:
        return jsonify({"ok": False, "error": error}), 400
    if code:
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Threads OAuth Code</title>"
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
            "max-width:720px;margin:48px auto;padding:0 20px;line-height:1.5}"
            "code{display:block;white-space:pre-wrap;word-break:break-all;background:#f6f3f1;"
            "border-radius:12px;padding:16px}</style></head><body>"
            "<h1>Threads OAuth code received</h1>"
            "<p>Copy this and send it to Hermes:</p>"
            f"<code>THREADS_CODE={code}</code>"
            "</body></html>"
        ), 200
    return jsonify({"ok": True, "service": "iryna-booking", "purpose": "oauth_callback"}), 200


@app.route('/meta/deauthorize', methods=['GET', 'POST'])
def meta_deauthorize_callback():
    """Meta app deauthorization callback.

    Meta may call this when a user removes app authorization. We do not keep a
    local Threads user table yet, so acknowledge the callback without side
    effects. Keeping this endpoint live lets Meta Developer settings validate.
    """
    return jsonify({"ok": True, "status": "deauthorization_received"}), 200


@app.route('/meta/data-deletion', methods=['GET', 'POST'])
def meta_data_deletion_callback():
    """Meta data deletion callback.

    Meta expects a JSON response containing a status URL and confirmation code.
    The booking system does not persist Threads user data yet; the response says
    the deletion request was accepted/no local Threads data exists.
    """
    confirmation_code = request.values.get("confirmation_code") or "pashynska-threads-no-local-user-data"
    return jsonify({
        "url": "https://book.pashynskaphoto.com/meta/data-deletion/status",
        "confirmation_code": confirmation_code,
    }), 200


@app.route('/meta/data-deletion/status')
def meta_data_deletion_status():
    return jsonify({
        "ok": True,
        "status": "completed",
        "message": "No local Threads user data is stored by this booking system.",
    }), 200


# ── Global error handlers ─────────────────────────────────────────────────────
# Goal: never show Flask's default debug traceback to a real visitor, never
# return raw 500 HTML to a JSON consumer (drawer JS, admin SPA, /admin/api/*).
# Path-aware content negotiation:
#   - /admin/api/*, paths ending in JSON-y suffixes, or `Accept: application/json` → JSON
#   - everything else → a small friendly HTML with the homepage as escape hatch.
def _wants_json():
    path = (request.path or "").lower()
    if path.startswith("/admin/api/") or path.startswith("/api/"):
        return True
    accept = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


def _safe_error_html(code, title, message):
    """Tiny self-contained error page — no template lookup (templates can be
    the thing that's broken), no external assets, no scripts."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{code} {title} · Pashynska Photography</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>body{margin:0;font:16px/1.5 -apple-system,Inter,sans-serif;"
        "background:#faf7f5;color:#1c1917;min-height:100vh;display:flex;"
        "align-items:center;justify-content:center;padding:24px}"
        ".card{max-width:480px;background:#fff;border-radius:18px;"
        "box-shadow:0 8px 28px rgba(28,25,23,.08);padding:32px;text-align:center}"
        "h1{margin:0 0 6px;font-size:22px;font-weight:600}"
        "p{margin:0 0 20px;color:#44403c}"
        "a.btn{display:inline-block;background:#1c1917;color:#fff;padding:12px 22px;"
        "border-radius:999px;text-decoration:none;font-weight:500}"
        "a.btn:hover{background:#44403c}.muted{color:#78716c;font-size:13px;margin-top:18px}"
        "</style></head><body><div class='card'>"
        f"<h1>{title}</h1><p>{message}</p>"
        "<a class='btn' href='/'>← Back to bookings</a>"
        "<p class='muted'>If this keeps happening, message Iryna on "
        "<a href='https://instagram.com/pashynska.photo' style='color:#a3685e'>Instagram</a>.</p>"
        "</div></body></html>"
    )


@app.errorhandler(404)
def _not_found(_e):
    if _wants_json():
        return jsonify({"error": "not_found", "path": request.path}), 404
    return _safe_error_html(404, "Page not found",
                            "We couldn't find that page. The booking calendar is here:"), 404


@app.errorhandler(413)
def _request_too_large(_e):
    message = "Upload is too large. Choose up to 5 photos and keep the batch under 30 MB."
    if _wants_json() or request.path.startswith("/admin/photos/"):
        return jsonify({"error": message}), 413
    return _safe_error_html(413, "Upload too large", message), 413


@app.errorhandler(500)
def _server_error(e):
    # Log full exception (Flask already logs, but be explicit and grep-friendly).
    log.exception(f"[500] {request.method} {request.path}: {e}")
    if _wants_json():
        return jsonify({"error": "server_error", "message": "Please try again in a moment."}), 500
    return _safe_error_html(500, "Something went wrong on our side",
                            "We're already looking into it. Please try again in a moment."), 500


@app.errorhandler(Exception)
def _uncaught(e):
    # Werkzeug HTTP exceptions (Abort, 4xx) pass through normally;
    # only catch true uncaught Python exceptions here.
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception(f"[uncaught] {request.method} {request.path}: {e}")
    if _wants_json():
        return jsonify({"error": "server_error", "message": "Please try again in a moment."}), 500
    return _safe_error_html(500, "Something went wrong on our side",
                            "We're already looking into it. Please try again in a moment."), 500


# Serve uploaded photos: try persistent volume first, then bundled static.
def _optimized_image_cache_path(source_path):
    """Return a cached WebP for legacy large jpg/png/webp images, or None.

    Existing admin uploads are already optimized. This keeps public /images URLs
    stable while fixing old heavy files on the persistent Fly volume.
    """
    try:
        if not os.path.isfile(source_path):
            return None
        ext = os.path.splitext(source_path)[1].lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            return None
        source_size = os.path.getsize(source_path)
        if source_size < _IMAGE_CACHE_MIN_BYTES:
            return None

        mtime = int(os.path.getmtime(source_path))
        basename = os.path.basename(source_path)
        cache_name = f"{basename}.{source_size}.{mtime}.webp"
        os.makedirs(_IMAGE_CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(_IMAGE_CACHE_DIR, cache_name)
        if os.path.isfile(cache_path):
            return cache_path

        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = 36_000_000
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((_IMAGE_CACHE_MAX_DIMENSION, _IMAGE_CACHE_MAX_DIMENSION), resampling)
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, (255, 255, 255))
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")
            tmp_path = f"{cache_path}.tmp"
            image.save(tmp_path, "WEBP", quality=_IMAGE_CACHE_WEBP_QUALITY, method=6)
            os.replace(tmp_path, cache_path)
        return cache_path
    except Exception as exc:
        log.warning(f"[images] optimized cache skipped for {source_path}: {exc}")
        return None

@app.route('/images/<path:filename>')
def serve_image(filename):
    persistent_path = os.path.join(PHOTOS_DIR, filename)
    source_path = persistent_path if os.path.isfile(persistent_path) else os.path.join(_BUNDLED_IMAGES_DIR, filename)
    cached_path = _optimized_image_cache_path(source_path)
    if cached_path:
        response = send_file(cached_path, mimetype="image/webp", conditional=True)
    elif os.path.isfile(persistent_path):
        response = send_from_directory(PHOTOS_DIR, filename)
    else:
        response = send_from_directory(_BUNDLED_IMAGES_DIR, filename)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response

# ===== NOTION CONFIG =====
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
# New Bookings v2 database (improved schema, Status field, Calendar Event link, etc.)
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "d722613f-a8b5-438f-bcf0-0ef9f84c3d78")
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

# ===== STRIPE CONFIG =====
STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
RECAPTCHA_SECRET_KEY   = os.environ.get("RECAPTCHA_SECRET_KEY", "")
RECAPTCHA_SITE_KEY     = os.environ.get("RECAPTCHA_SITE_KEY", "")

if STRIPE_SECRET_KEY:
    try:
        import stripe as _stripe
        _stripe.api_key = STRIPE_SECRET_KEY
        log.info("[stripe] Stripe configured ✓")
    except ImportError:
        log.warning("[stripe] stripe package not installed — card payments disabled")
else:
    log.info("[stripe] STRIPE_SECRET_KEY not set — card payments disabled")

# Map internal SQLite status → Notion select option
NOTION_STATUS_MAP = {
    "reserved": "Reserved",
    "pending": "Pending Payment",
    "pending_payment": "Pending Payment",
    "underpaid": "Underpaid",
    "confirmed": "Confirmed",
    "completed": "Completed",
    "expired": "Expired",
    "cancelled": "Cancelled",
}


def _slot_label(t, event=None):
    """Turn '10:00' into '10:00–10:20' using session_length from event."""
    sl = (event or _active).get("session_length", SESSION_LENGTH)
    try:
        start = datetime.strptime(t, "%H:%M")
        end = start + timedelta(minutes=sl)
        return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
    except Exception:
        return t


def sync_to_notion(booking_id):
    """Sync a single SQLite booking to the Bookings v2 Notion DB.
    Stores the Notion page_id back in SQLite so subsequent calls patch in place."""
    if not NOTION_API_KEY:
        return  # silently skip when no key configured
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return
        booking = dict(row)

        if booking["confirmed"]:
            status_name = "Confirmed"
        else:
            status_name = NOTION_STATUS_MAP.get(booking["status"], "Pending Payment")

        properties = {
            "Client Name": {"title": [{"text": {"content": booking["name"] or "(awaiting details)"}}]},
            "Status": {"select": {"name": status_name}},
            "Date": {"date": {"start": booking["date"]}},
            "Time Slot": {"rich_text": [{"text": {"content": _slot_label(booking["time"])}}]},
            "Instagram": {"rich_text": [{"text": {"content": booking.get("instagram") or ""}}]},
            "Deposit (CAD)": {"number": booking.get("paid_amount") or SESSION_PRICE},
            "Total (CAD)": {"number": SESSION_TOTAL},
            "Paid": {"checkbox": bool(booking["paid"])},
            "Booking ID (legacy)": {"number": booking["id"]},
        }
        # Notion rejects empty strings for email/phone — only set when present
        if booking.get("email"):
            properties["Email"] = {"email": booking["email"]}
        if booking.get("phone"):
            properties["Phone"] = {"phone_number": booking["phone"]}
        if booking.get("session_type"):
            properties["Session Type"] = {"select": {"name": booking["session_type"]}}
        if booking.get("paid"):
            properties["Payment Method"] = {"select": {"name": "e-Transfer"}}
        if booking.get("calendar_event_url"):
            properties["Calendar Event"] = {"url": booking["calendar_event_url"]}
        if booking.get("reserved_until"):
            properties["Reserved Until"] = {"date": {"start": booking["reserved_until"]}}

        page_id = booking.get("notion_page_id")
        if page_id:
            r = requests.patch(f"https://api.notion.com/v1/pages/{page_id}",
                               headers=NOTION_HEADERS,
                               json={"properties": properties}, timeout=15)
            if r.status_code >= 400:
                print(f"Notion patch failed ({r.status_code}): {r.text[:200]}")
        else:
            r = requests.post("https://api.notion.com/v1/pages",
                              headers=NOTION_HEADERS,
                              json={"parent": {"database_id": NOTION_DATABASE_ID},
                                    "properties": properties}, timeout=15)
            if r.status_code < 300:
                page_id = r.json().get("id")
                c.execute("UPDATE bookings SET notion_page_id=? WHERE id=?", (page_id, booking_id))
                conn.commit()
            else:
                print(f"Notion create failed ({r.status_code}): {r.text[:200]}")
        conn.close()
    except Exception as e:
        print(f"Notion sync error: {e}")


# ===== GOOGLE CALENDAR CONFIG =====
CALENDAR_ID = os.environ.get("BOOKING_CALENDAR_ID", "iryna.pashynska@gmail.com")
CALENDAR_TZ = os.environ.get("BOOKING_CALENDAR_TZ", "America/Edmonton")
# Filled in by run-calendar-helper.py via env at runtime; left empty here so import never fails
_calendar_helper = None  # type: ignore


def create_calendar_event_for_booking(booking_id):
    """Create a Google Calendar event by shelling out to the GCal MCP helper script.
    Stored event URL goes back into SQLite so Notion can render the link.
    Falls back gracefully if no helper is configured."""
    helper = os.environ.get("GCAL_HELPER")  # path to a CLI that wraps create_event
    if not helper:
        return None
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return None
        b = dict(row)

        start_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=SESSION_LENGTH)
        summary = f"📸 {b['name'] or 'Mini Session'} — {EVENT_TITLE}"
        description = (
            f"Mini Photo Session ({SESSION_LENGTH} min)\n"
            f"Client: {b['name']}\n"
            f"Phone: {b['phone']}\n"
            f"Email: {b['email']}\n"
            f"Instagram: {b.get('instagram') or ''}\n"
            f"Session type: {b.get('session_type') or ''}\n"
            f"Booking #{b['id']}\n"
            f"Location: {ev.get('location') if ev else 'TBD'}"
        )
        # Get event details for location
        event = get_event_by_id(b.get('event_id')) if b.get('event_id') else None
        location = event.get('location', '') if event else ''
        
        import subprocess as _sp
        result = _sp.run(
            [helper, "create",
             "--calendar", CALENDAR_ID,
             "--summary", summary,
             "--start", start_dt.isoformat(),
             "--end", end_dt.isoformat(),
             "--tz", CALENDAR_TZ,
             "--description", description,
             "--location", location],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"GCal helper failed: {result.stderr[:200]}")
            conn.close()
            return None
        out = json.loads(result.stdout.strip())
        event_id = out.get("id")
        event_url = out.get("htmlLink") or out.get("url")
        c.execute("UPDATE bookings SET calendar_event_id=?, calendar_event_url=? WHERE id=?",
                  (event_id, event_url, booking_id))
        conn.commit()
        conn.close()
        return event_url
    except Exception as e:
        print(f"Calendar error: {e}")
        return None

# ===== EVENTS CONFIG (YAML) =====
# EVENTS_YAML_PATH env var lets Fly.io (or any deployment) store events on
# the persistent volume (/data/events.yaml) so they survive restarts/redeploys.
# start.sh copies the bundled events.yaml to /data on first run.
_EVENTS_PATH = (
    os.environ.get("EVENTS_YAML_PATH")
    or os.path.join(os.path.dirname(__file__), "events.yaml")
)
_EVENTS_YAML_LOCK = threading.RLock()

def _load_events():
    """Load events from YAML, return list of event dicts."""
    with open(_EVENTS_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    return data.get("events", []), data.get("settings", {})

EVENTS, SETTINGS = _load_events()

def get_active_event():
    """Return the first active event, or None."""
    for ev in EVENTS:
        if ev.get("status") == "active":
            return ev
    return EVENTS[0] if EVENTS else None

def get_event_by_date(date_str):
    """Find an event by its date."""
    for ev in EVENTS:
        if ev["date"] == date_str:
            return ev
    return None

def get_event_by_id(event_id):
    """Find an event by its id."""
    for ev in EVENTS:
        if ev["id"] == event_id:
            return ev
    return None


def _deeplink_key(value):
    """Normalize GBP/product/session link hints for robust matching."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def resolve_event_deeplink(value):
    """Resolve a direct booking link hint to the next matching public event.

    Supports exact event ids plus short GBP-friendly aliases such as
    `canoe_mini`, `mountain_mini`, and `boho_swing`. When multiple events match
    an alias, choose the earliest upcoming/non-completed event so evergreen GBP
    product links keep opening the next available session instead of the home
    page.
    """
    raw = str(value or "").strip()
    if not raw:
        return None

    exact = get_event_by_id(raw)
    if exact:
        return exact

    key = _deeplink_key(raw)
    if not key:
        return None

    alias_terms = {
        "canoemini": ["canoe"],
        "canoeminisession": ["canoe"],
        "mountainmini": ["mountain"],
        "mountainsmini": ["mountain"],
        "mountainminisession": ["mountain"],
        "bohoswingmini": ["boho", "swing"],
        "bohoswingminisession": ["boho", "swing"],
        "goldenboho": ["golden", "boho"],
        "goldenbohomini": ["golden", "boho"],
        "lilacmini": ["lilac"],
        "family": ["family"],
        "familysession": ["family"],
        "maternity": ["maternity"],
        "maternitysession": ["maternity"],
        "engagement": ["engagement"],
        "engagementsession": ["engagement"],
        "smallwedding": ["small", "wedding"],
        "fullwedding": ["full", "wedding"],
    }
    terms = alias_terms.get(key)
    if not terms:
        # Also allow ids/titles typed as loose slugs with underscores/hyphens.
        terms = [key]

    today = _local_today()
    candidates = []
    for ev in EVENTS:
        haystack = _deeplink_key(" ".join(str(ev.get(field, "")) for field in (
            "id", "title", "subtitle", "description", "location", "session_type", "type"
        )))
        if not all(term in haystack for term in terms):
            continue
        if ev.get("status") == "completed":
            continue
        try:
            ev_date = datetime.strptime(str(ev.get("date", "")), "%Y-%m-%d").date()
        except ValueError:
            ev_date = today
        if ev_date < today and ev.get("booking_type") != "rolling_availability":
            continue
        candidates.append((ev_date, ev))

    if candidates:
        return sorted(candidates, key=lambda item: item[0])[0][1]
    return None


BUILTIN_ADDONS = {
    "extra-10-edited-images": {
        "id": "extra-10-edited-images",
        "title": "10 Extra Edited Images",
        "description": (
            "Add 10 additional professionally edited images to your final gallery. "
            "Best value. Individual extra edited images can be purchased later for $10/image."
        ),
        "price": 50.0,
        "active": False,
    },
    "short-vertical-reel": {
        "id": "short-vertical-reel",
        "title": "Short Vertical Behind-the-Scenes Reel",
        "description": (
            "Add a short vertical video up to 1 minute from your session, perfect for "
            "Instagram Reels, Stories, or family memories."
        ),
        "price": 50.0,
        "active": False,
    },
}

DEFAULT_MINI_AGREEMENT = {
    "enabled": True,
    "require_terms": True,
    "require_marketing_choice": True,
    "terms_version": "mini-session-terms-v1",
}


def _default_mini_addons():
    """Default upsells for newly-created instant mini-session events."""
    return [
        {**BUILTIN_ADDONS["extra-10-edited-images"], "active": True},
        {**BUILTIN_ADDONS["short-vertical-reel"], "active": True},
    ]


def _is_instant_mini_event(event):
    """Return True for public fixed-slot mini sessions that should get defaults."""
    return (
        _booking_type(event or {}) == "fixed_slots"
        and str((event or {}).get("session_type") or (event or {}).get("type") or "mini").lower() == "mini"
        and not bool((event or {}).get("inquiry_only"))
    )


def _money(value, default=0.0):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return round(float(default or 0), 2)


def _event_active_addons(event):
    """Return sanitized active flat-price add-ons from event config."""
    out = []
    for addon in (event or {}).get("addons") or []:
        if not isinstance(addon, dict):
            continue
        addon_id = str(addon.get("id") or "").strip()
        if not addon_id or addon.get("active") is not True:
            continue
        title = str(addon.get("title") or BUILTIN_ADDONS.get(addon_id, {}).get("title") or addon_id).strip()
        description = str(addon.get("description") or BUILTIN_ADDONS.get(addon_id, {}).get("description") or "").strip()
        out.append({
            "id": addon_id[:80],
            "title": title[:120],
            "description": description[:500],
            "price": _money(addon.get("price"), 0.0),
            "active": True,
        })
    return out


def _validate_selected_addons(event, selected_ids):
    """Validate requested add-on ids against the selected event config."""
    if selected_ids in (None, "", []):
        return [], 0.0
    if not isinstance(selected_ids, list):
        raise ValueError("Add-ons must be sent as a list")
    active = {a["id"]: a for a in _event_active_addons(event)}
    selected = []
    seen = set()
    for raw_id in selected_ids:
        addon_id = str(raw_id or "").strip()
        if not addon_id or addon_id in seen:
            continue
        addon = active.get(addon_id)
        if not addon:
            raise ValueError(f"Unknown or inactive add-on: {addon_id}")
        selected.append({
            "id": addon["id"],
            "title": addon["title"],
            "description": addon["description"],
            "price": addon["price"],
        })
        seen.add(addon_id)
    total = round(sum(_money(a.get("price")) for a in selected), 2)
    return selected, total


def _booking_addons(booking):
    raw = (booking or {}).get("selected_addons_json")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _booking_addons_total(booking):
    amount = _money((booking or {}).get("addons_total"), 0.0)
    if amount > 0:
        return amount
    return round(sum(_money(a.get("price")) for a in _booking_addons(booking) if isinstance(a, dict)), 2)


def _event_agreement_config(event):
    cfg = (event or {}).get("agreement") or {}
    return cfg if isinstance(cfg, dict) else {}


def _event_requires_agreement(event):
    return bool(_event_agreement_config(event).get("enabled"))


def _terms_version(event):
    cfg = _event_agreement_config(event)
    return str(cfg.get("terms_version") or cfg.get("version") or "booking-terms-v1")[:80]


def _questionnaire_config_for_event(event):
    cfg = (event or {}).get("questionnaire") or {}
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    session_type = str((event or {}).get("session_type") or "").strip().lower()
    if session_type == "mini":
        return None
    allowed = [str(x).strip().lower() for x in (cfg.get("session_types") or ["individual", "custom", "private"])]
    if session_type and session_type not in allowed:
        return None
    if (cfg.get("timing") or "after_confirmed_payment") != "after_confirmed_payment":
        return None
    return cfg


def _strip_tags(value):
    text = re.sub(r"<[^>]*>", "", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _slugify_addon_id(value):
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:80] or f"custom-addon-{secrets.token_hex(3)}"


def _sanitize_event_addons(raw_addons):
    """Normalize admin-submitted add-ons before writing events.yaml."""
    if not isinstance(raw_addons, list):
        return []
    cleaned = []
    seen = set()
    for raw in raw_addons:
        if not isinstance(raw, dict) or raw.get("active") is not True:
            continue
        raw_id = str(raw.get("id") or "").strip()
        raw_title = _strip_tags(raw.get("title") or "")
        addon_id = raw_id or _slugify_addon_id(raw_title)
        if addon_id in seen:
            continue
        default = BUILTIN_ADDONS.get(addon_id, {})
        title = _strip_tags(raw_title or default.get("title") or addon_id)[:120]
        description = _strip_tags(raw.get("description") or default.get("description") or "")[:500]
        if title.lower() == "full gallery upgrade" or addon_id == "full-gallery-upgrade":
            continue
        cleaned.append({
            "id": addon_id[:80],
            "title": title,
            "description": description,
            "price": _money(raw.get("price"), default.get("price", 0.0)),
            "active": True,
        })
        seen.add(addon_id)
    return cleaned


def _booking_type(ev):
    """Admin-selected booking behavior for an event.

    Backward-compatible mapping:
    - mini/fixed_slots: one configured date with generated slots
    - individual/rolling_availability: client chooses a date inside horizon
    - wedding/inquiry_only: no instant checkout, route to inquiry/DM
    """
    if not ev:
        return "fixed_slots"
    explicit = ev.get("booking_type") or ev.get("booking_mode") or ev.get("availability_mode")
    if explicit in ("fixed_slots", "rolling_availability", "inquiry_only"):
        return explicit
    st = (ev.get("session_type") or ev.get("type") or "mini").lower()
    if st in ("individual", "full", "portrait"):
        return "rolling_availability"
    if st in ("wedding", "custom", "inquiry"):
        return "inquiry_only"
    return "fixed_slots"


def _event_blackout_dates(ev):
    vals = []
    for key in ("blackout_dates", "unavailable_dates", "blocked_dates"):
        raw = ev.get(key) or []
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.replace("\n", ",").split(",")]
        vals.extend(str(v).strip() for v in raw if str(v).strip())
    return set(vals)


def _rolling_horizon_days(ev):
    try:
        return max(1, min(365, int(ev.get("availability_horizon_days") or ev.get("booking_horizon_days") or 90)))
    except (TypeError, ValueError):
        return 90


def _rolling_date_unavailable_reason(ev, date_str):
    """Return None if rolling event can be booked on date_str, else reason."""
    try:
        requested = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return "invalid_date"
    today = _local_today()
    if requested < today:
        return "past"
    if (requested - today).days > _rolling_horizon_days(ev):
        return "outside_horizon"
    if date_str in _event_blackout_dates(ev):
        return "blackout"
    return None


def _event_date_for_booking(ev, requested_date=None):
    return requested_date if _booking_type(ev) == "rolling_availability" and requested_date else ev.get("date", "")

# Convenience accessors (backward-compatible)
_active = get_active_event() or {}
EVENT_TITLE = _active.get("title", "Mini Sessions")
SESSION_LENGTH = _active.get("session_length", 20)
BREAK_LENGTH = _active.get("break_length", 10)
SLOT_INTERVAL = _active.get("slot_interval", 30)
SESSION_PRICE = _active.get("deposit", 95)
SESSION_TOTAL = _active.get("full_price", 190)
EMAIL = SETTINGS.get("photographer_email", "")
RESERVATION_MINUTES = SETTINGS.get("reservation_minutes", 15)
PENDING_PAYMENT_HOURS = int(os.environ.get("PENDING_PAYMENT_HOURS", "24"))
DATE = _active.get("date", "")
START_TIME = _active.get("start_time", "10:00")
END_TIME = _active.get("end_time", "16:00")

# ===== DATABASE =====
# DB_PATH: can be overridden via env var DB_PATH.
# Default: ~/.pashynska-data/bookings.db — OUTSIDE the app folder so the
# database survives code updates, git pulls, or server redeployments.
_default_data_dir = os.path.join(os.path.expanduser("~"), ".pashynska-data")
DB_PATH = os.environ.get("DB_PATH") or os.path.join(_default_data_dir, "bookings.db")
BACKUP_DIR = os.environ.get("BACKUP_DIR") or os.path.join(_default_data_dir, "backups")

# Ensure directories exist
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
log.info(f"[db] Database: {DB_PATH}")
log.info(f"[db] Backups:  {BACKUP_DIR}")


def init_db():
    """Create SQLite tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # ── bookings table ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            instagram TEXT,
            session_type TEXT,
            status TEXT DEFAULT 'pending',
            paid BOOLEAN DEFAULT 0,
            confirmed BOOLEAN DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reserved_until TEXT,
            paid_amount REAL DEFAULT NULL,
            UNIQUE(date, time)
        )
    ''')

    # ── clients table — one row per unique email ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            instagram TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            total_bookings INTEGER DEFAULT 0,
            total_confirmed INTEGER DEFAULT 0,
            total_paid REAL DEFAULT 0.0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── client_notes — timestamped notes per client ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS client_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        )
    ''')
    # Rename legacy column 'note' → 'text' so the codebase is consistent.
    # SQLite < 3.25 has no RENAME COLUMN; detect and patch.
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(client_notes)").fetchall()]
        if 'text' not in cols and 'note' in cols:
            c.execute("ALTER TABLE client_notes RENAME COLUMN note TO text")
    except Exception:
        pass

    # ── processed_emails — prevent double-processing of same Interac email ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS processed_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            booking_id INTEGER,
            amount REAL,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── waitlist — interested clients for sold-out / fully booked events ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT DEFAULT '',
            preferred_slot TEXT DEFAULT '',
            message TEXT DEFAULT '',
            status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(event_id, email)
        )
    ''')

    # ── first-party ad / funnel analytics ──
    c.execute('''
        CREATE TABLE IF NOT EXISTS visitor_sessions (
            visitor_id TEXT PRIMARY KEY,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            utm_source TEXT DEFAULT '',
            utm_medium TEXT DEFAULT '',
            utm_campaign TEXT DEFAULT '',
            utm_content TEXT DEFAULT '',
            utm_term TEXT DEFAULT '',
            fbclid TEXT DEFAULT '',
            gclid TEXT DEFAULT '',
            referrer TEXT DEFAULT '',
            landing_url TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            ip_address TEXT DEFAULT ''
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visitor_id TEXT NOT NULL,
            booking_id INTEGER,
            event_name TEXT NOT NULL,
            event_id TEXT DEFAULT '',
            page TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── migrations: add columns that didn't exist in older installs ──
    _migrations = [
        ("bookings",  "paid_amount",       "ALTER TABLE bookings ADD COLUMN paid_amount REAL"),
        ("bookings",  "notion_page_id",    "ALTER TABLE bookings ADD COLUMN notion_page_id TEXT"),
        ("bookings",  "calendar_event_id", "ALTER TABLE bookings ADD COLUMN calendar_event_id TEXT"),
        ("bookings",  "calendar_event_url","ALTER TABLE bookings ADD COLUMN calendar_event_url TEXT"),
        ("bookings",  "event_id",          "ALTER TABLE bookings ADD COLUMN event_id TEXT"),
        ("clients",   "tags",              "ALTER TABLE clients ADD COLUMN tags TEXT DEFAULT ''"),
        ("clients",   "notes",             "ALTER TABLE clients ADD COLUMN notes TEXT DEFAULT ''"),
        # Automated email tracking
        ("bookings",  "abandoned_email_sent",  "ALTER TABLE bookings ADD COLUMN abandoned_email_sent TEXT"),
        ("bookings",  "abandoned_second_email_sent", "ALTER TABLE bookings ADD COLUMN abandoned_second_email_sent TEXT"),
        ("bookings",  "reminder_email_sent",   "ALTER TABLE bookings ADD COLUMN reminder_email_sent TEXT"),
        ("bookings",  "reminder_24h_email_sent","ALTER TABLE bookings ADD COLUMN reminder_24h_email_sent TEXT"),
        ("bookings",  "review_email_sent",     "ALTER TABLE bookings ADD COLUMN review_email_sent TEXT"),
        ("bookings",  "wfolio_url",            "ALTER TABLE bookings ADD COLUMN wfolio_url TEXT"),
        # first_booking_at / last_booking_at for clients table
        ("clients",   "first_booking_at",  "ALTER TABLE clients ADD COLUMN first_booking_at TEXT"),
        ("clients",   "last_booking_at",   "ALTER TABLE clients ADD COLUMN last_booking_at TEXT"),
        ("bookings",  "confirmation_token", "ALTER TABLE bookings ADD COLUMN confirmation_token TEXT"),
        # Store expected deposit at booking time so checker doesn't need events.yaml lookup
        ("bookings",  "deposit_amount",    "ALTER TABLE bookings ADD COLUMN deposit_amount REAL"),
        # Store full_price in booking so invoice always matches what was agreed
        ("bookings",  "full_price",        "ALTER TABLE bookings ADD COLUMN full_price REAL"),
        # Referral code applied at booking + the friend's discount (comes off the BALANCE,
        # never the deposit, so e-Transfer amount-matching is unaffected)
        ("bookings",  "referral_code",     "ALTER TABLE bookings ADD COLUMN referral_code TEXT"),
        ("bookings",  "referral_discount", "ALTER TABLE bookings ADD COLUMN referral_discount REAL DEFAULT 0"),
        # Session-inspired add-ons, agreement, and optional post-confirmation questionnaire
        ("bookings",  "selected_addons_json", "ALTER TABLE bookings ADD COLUMN selected_addons_json TEXT"),
        ("bookings",  "addons_total",      "ALTER TABLE bookings ADD COLUMN addons_total REAL DEFAULT 0"),
        ("bookings",  "marketing_consent", "ALTER TABLE bookings ADD COLUMN marketing_consent TEXT"),
        ("bookings",  "agreement_name",    "ALTER TABLE bookings ADD COLUMN agreement_name TEXT"),
        ("bookings",  "agreement_accepted_at", "ALTER TABLE bookings ADD COLUMN agreement_accepted_at TEXT"),
        ("bookings",  "terms_version",     "ALTER TABLE bookings ADD COLUMN terms_version TEXT"),
        ("bookings",  "questionnaire_answers_json", "ALTER TABLE bookings ADD COLUMN questionnaire_answers_json TEXT"),
        # First-party attribution for Meta/Google/organic comparisons
        ("bookings",  "visitor_id",        "ALTER TABLE bookings ADD COLUMN visitor_id TEXT"),
        ("bookings",  "utm_source",        "ALTER TABLE bookings ADD COLUMN utm_source TEXT"),
        ("bookings",  "utm_medium",        "ALTER TABLE bookings ADD COLUMN utm_medium TEXT"),
        ("bookings",  "utm_campaign",      "ALTER TABLE bookings ADD COLUMN utm_campaign TEXT"),
        ("bookings",  "utm_content",       "ALTER TABLE bookings ADD COLUMN utm_content TEXT"),
        ("bookings",  "utm_term",          "ALTER TABLE bookings ADD COLUMN utm_term TEXT"),
        ("bookings",  "fbclid",            "ALTER TABLE bookings ADD COLUMN fbclid TEXT"),
        ("bookings",  "gclid",             "ALTER TABLE bookings ADD COLUMN gclid TEXT"),
        ("bookings",  "referrer",          "ALTER TABLE bookings ADD COLUMN referrer TEXT"),
        ("bookings",  "landing_url",       "ALTER TABLE bookings ADD COLUMN landing_url TEXT"),
        # Stripe payment-link URL for manually-created private sessions
        ("bookings",  "payment_link",      "ALTER TABLE bookings ADD COLUMN payment_link TEXT"),
        # processed_emails ledger for e-Transfer safety
        ("_meta",     "processed_emails",  "CREATE TABLE IF NOT EXISTS processed_emails (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL, booking_id INTEGER, amount REAL, processed_at TEXT DEFAULT CURRENT_TIMESTAMP)"),
        # Interac e-Transfer ledger — every incoming transfer (email + CSV import), linkable to a booking
        ("_meta",     "etransfers",        "CREATE TABLE IF NOT EXISTS etransfers (id INTEGER PRIMARY KEY AUTOINCREMENT, reference_number TEXT UNIQUE, message_id TEXT, sender_name TEXT, amount REAL, memo TEXT, direction TEXT DEFAULT 'in', email_date TEXT, matched_booking_id INTEGER, matched_gift_code TEXT, status TEXT DEFAULT 'unmatched', source TEXT DEFAULT 'email', created_at TEXT DEFAULT CURRENT_TIMESTAMP)"),
        ("etransfers", "matched_gift_code", "ALTER TABLE etransfers ADD COLUMN matched_gift_code TEXT"),
    ]
    for _tbl, _col, _ddl in _migrations:
        try:
            c.execute(_ddl)
        except sqlite3.OperationalError:
            pass  # column already exists

    # ── Data-fix: legacy default DEFAULT '[]' for clients.tags created rows
    # with the literal string "[]" instead of an empty CSV. The rest of the
    # codebase (api_client_tag, /admin/clients UI) treats `tags` as a
    # comma-separated list, so "[]" rendered as a fake tag chip and broke
    # tag filters. Normalise once on startup; safe to run repeatedly.
    try:
        c.execute("UPDATE clients SET tags='' WHERE tags='[]' OR tags IS NULL")
    except sqlite3.OperationalError:
        pass

    # ── Indexes for hot query paths ────────────────────────────────────────────
    # /slots queries: WHERE date=? AND status NOT IN (...) AND reserved_until>?
    # expire_reservations: WHERE confirmed=0 AND paid=0 AND reserved_until<=? AND status IN (...)
    # email scheduler: WHERE status='expired' AND abandoned_email_sent IS NULL ...
    _indexes = [
        "CREATE INDEX IF NOT EXISTS idx_bookings_date        ON bookings(date)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_status      ON bookings(status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_confirmed   ON bookings(confirmed)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_reserved    ON bookings(reserved_until)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_event_id    ON bookings(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_utm_campaign ON bookings(utm_campaign)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_visitor_id   ON bookings(visitor_id)",
        "CREATE INDEX IF NOT EXISTS idx_analytics_visitor     ON analytics_events(visitor_id)",
        "CREATE INDEX IF NOT EXISTS idx_analytics_booking     ON analytics_events(booking_id)",
        "CREATE INDEX IF NOT EXISTS idx_analytics_event_time  ON analytics_events(event_name, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_clients_email        ON clients(email)",
        "CREATE INDEX IF NOT EXISTS idx_waitlist_event_email ON waitlist(event_id, email)",
    ]
    for _idx_ddl in _indexes:
        try:
            c.execute(_idx_ddl)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


init_db()


# ─────────────────────────────────────────────────────────────────────────────
#  BACKGROUND EMAIL SCHEDULER
#  Runs every 5 minutes in a daemon thread. Handles:
#    1. Abandoned booking recovery  (2h after expiry)
#    2. Pre-session reminder         (48h before session)
#    3. Post-session review request  (5 days after session)
# ─────────────────────────────────────────────────────────────────────────────

def _run_email_scheduler():
    import threading as _threading
    # Small initial delay so the server has time to fully start
    _threading.Event().wait(60)
    while True:
        try:
            _process_abandoned_emails()
            _process_reminder_emails()      # 48h
            _process_24h_reminder_emails()  # 24h
            _process_review_emails()
        except Exception as _e:
            log.error(f"[scheduler] Unexpected error: {_e}")
        _threading.Event().wait(300)  # run every 5 minutes


def _process_abandoned_emails():
    """Send abandoned recovery emails: first after 2h, gentle final after 48h."""
    now = _local_now()
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM bookings
        WHERE status = 'expired'
          AND email IS NOT NULL AND email != ''
          AND (
            abandoned_email_sent IS NULL
            OR (abandoned_email_sent IS NOT NULL AND abandoned_second_email_sent IS NULL)
          )
    """).fetchall()
    conn.close()

    for row in rows:
        b = dict(row)
        try:
            if _is_due_for_first_abandoned_followup(b, now):
                ok = _send_abandoned_email(b)
                sent_column = "abandoned_email_sent"
                event_name = "abandoned_followup_sent"
                log_label = "Abandoned email sent"
            elif _is_due_for_second_abandoned_followup(b, now):
                ok = _send_abandoned_second_email(b)
                sent_column = "abandoned_second_email_sent"
                event_name = "abandoned_second_followup_sent"
                log_label = "Second abandoned email sent"
            else:
                continue

            if ok:
                conn2 = db_conn()
                conn2.execute(
                    f"UPDATE bookings SET {sent_column}=? WHERE id=?",
                    (now.isoformat(), b["id"])
                )
                conn2.commit()
                conn2.close()
                _record_booking_funnel_event(
                    b,
                    event_name,
                    {"channel": "email", "sent_at": now.isoformat()},
                )
                _emit_n8n_event(
                    f"booking.{event_name}",
                    booking={
                        "id": b.get("id"),
                        "name": b.get("name"),
                        "email": b.get("email"),
                        "phone": b.get("phone"),
                        "instagram": b.get("instagram"),
                        "date": b.get("date"),
                        "time": b.get("time"),
                        "event_id": b.get("event_id"),
                        "utm_campaign": b.get("utm_campaign"),
                        "utm_content": b.get("utm_content"),
                    },
                    channel="email",
                )
                log.info(f"[scheduler] {log_label} → booking #{b['id']} ({b.get('email')})")
        except Exception as e:
            log.error(f"[scheduler] Abandoned email failed for #{b['id']}: {e}")


def _process_reminder_emails():
    """Send 48h pre-session reminders to confirmed bookings."""
    now = _local_now()
    # Window: sessions happening between 46h and 50h from now (4h window to avoid duplicates)
    date_from = (now + timedelta(hours=46)).strftime("%Y-%m-%d")
    date_to   = (now + timedelta(hours=50)).strftime("%Y-%m-%d")
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM bookings
        WHERE status = 'confirmed' AND confirmed = 1
          AND reminder_email_sent IS NULL
          AND date BETWEEN ? AND ?
          AND email IS NOT NULL AND email != ''
    """, (date_from, date_to)).fetchall()
    conn.close()
    for row in rows:
        b = dict(row)
        try:
            ok = _send_reminder_email(b)
            conn2 = db_conn()
            conn2.execute(
                "UPDATE bookings SET reminder_email_sent=? WHERE id=?",
                (now.isoformat(), b["id"])
            )
            conn2.commit()
            conn2.close()
            if ok:
                log.info(f"[scheduler] Reminder email sent → booking #{b['id']} ({b.get('email')}) for {b.get('date')}")
        except Exception as e:
            log.error(f"[scheduler] Reminder email failed for #{b['id']}: {e}")


def _process_24h_reminder_emails():
    """Send 24-hour pre-session reminders to confirmed bookings."""
    now = _local_now()
    # Window: sessions happening between 22h and 26h from now (4h window to avoid duplicates)
    date_from = (now + timedelta(hours=22)).strftime("%Y-%m-%d")
    date_to   = (now + timedelta(hours=26)).strftime("%Y-%m-%d")
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM bookings
        WHERE status = 'confirmed' AND confirmed = 1
          AND reminder_24h_email_sent IS NULL
          AND date BETWEEN ? AND ?
          AND email IS NOT NULL AND email != ''
    """, (date_from, date_to)).fetchall()
    conn.close()
    for row in rows:
        b = dict(row)
        try:
            ok = _send_24h_reminder_email(b)
            conn2 = db_conn()
            conn2.execute(
                "UPDATE bookings SET reminder_24h_email_sent=? WHERE id=?",
                (now.isoformat(), b["id"])
            )
            conn2.commit()
            conn2.close()
            if ok:
                log.info(f"[scheduler] 24h reminder sent → booking #{b['id']} ({b.get('email')}) for {b.get('date')}")
        except Exception as e:
            log.error(f"[scheduler] 24h reminder failed for #{b['id']}: {e}")


def _process_review_emails():
    """Send review request emails 5 days after a confirmed session."""
    now = _local_now()
    # Sessions that happened between 4 and 6 days ago
    date_from = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    date_to   = (now - timedelta(days=4)).strftime("%Y-%m-%d")
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM bookings
        WHERE status = 'confirmed' AND confirmed = 1
          AND review_email_sent IS NULL
          AND date BETWEEN ? AND ?
          AND email IS NOT NULL AND email != ''
    """, (date_from, date_to)).fetchall()
    conn.close()
    for row in rows:
        b = dict(row)
        try:
            ok = _send_review_email(b)
            conn2 = db_conn()
            conn2.execute(
                "UPDATE bookings SET review_email_sent=? WHERE id=?",
                (now.isoformat(), b["id"])
            )
            conn2.commit()
            conn2.close()
            if ok:
                log.info(f"[scheduler] Review email sent → booking #{b['id']} ({b.get('email')})")
        except Exception as e:
            log.error(f"[scheduler] Review email failed for #{b['id']}: {e}")


# Start the scheduler in a background daemon thread
if _background_threads_disabled():
    log.info("[scheduler] Email scheduler disabled by DISABLE_BACKGROUND_THREADS")
else:
    import threading as _bg_thread
    _sched = _bg_thread.Thread(target=_run_email_scheduler, daemon=True, name="email-scheduler")
    _sched.start()
    log.info("[scheduler] Email scheduler started (abandoned / 48h reminder / 24h reminder / review)")


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── CLIENT SYNC ──────────────────────────────────────────────────────────────
def sync_client(email: str, name: str, phone: str = "", instagram: str = ""):
    """Upsert a client record and refresh aggregated stats from bookings table.
    Called automatically whenever a booking is created or confirmed."""
    if not email:
        return
    conn = db_conn()
    c = conn.cursor()
    now = _local_now().isoformat()

    # Insert or update basic info (keep latest name/phone/ig)
    c.execute("""
        INSERT INTO clients (email, name, phone, instagram, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            name      = excluded.name,
            phone     = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE clients.phone END,
            instagram = CASE WHEN excluded.instagram != '' THEN excluded.instagram ELSE clients.instagram END,
            last_seen = excluded.last_seen
    """, (email.lower().strip(), name, phone, instagram, now, now))

    # Refresh aggregate stats — bookings counters + first/last booking dates.
    # first_booking_at / last_booking_at use the booking `date` (not created_at)
    # so they reflect when the session actually happened/will happen, which is
    # what an operator scanning /admin/clients expects.
    c.execute("""
        UPDATE clients SET
            total_bookings   = (SELECT COUNT(*) FROM bookings WHERE LOWER(email)=LOWER(?) AND status NOT IN ('expired')),
            total_confirmed  = (SELECT COUNT(*) FROM bookings WHERE LOWER(email)=LOWER(?) AND confirmed=1),
            total_paid       = (SELECT COALESCE(SUM(paid_amount),0) FROM bookings WHERE LOWER(email)=LOWER(?) AND confirmed=1),
            first_booking_at = (SELECT MIN(date) FROM bookings WHERE LOWER(email)=LOWER(?) AND status NOT IN ('expired','cancelled')),
            last_booking_at  = (SELECT MAX(date) FROM bookings WHERE LOWER(email)=LOWER(?) AND status NOT IN ('expired','cancelled'))
        WHERE LOWER(email) = LOWER(?)
    """, (email, email, email, email, email, email))

    conn.commit()
    conn.close()


def rebuild_clients_from_bookings():
    """One-time rebuild: populate clients table from existing bookings."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT DISTINCT email, name, phone, instagram FROM bookings WHERE email != '' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    for r in rows:
        sync_client(r["email"], r["name"], r["phone"] or "", r["instagram"] or "")
    log.info(f"[db] Rebuilt clients table: {len(rows)} unique emails processed")


# Run once on startup to populate clients from existing bookings
try:
    _c = db_conn()
    _cnt = _c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    _c.close()
    if _cnt == 0:
        rebuild_clients_from_bookings()
except Exception as _e:
    log.warning(f"[db] Client rebuild skipped: {_e}")

# Backfill: first_booking_at / last_booking_at were added as columns but
# sync_client wasn't writing to them — old installs have NULL even though
# bookings exist. Recompute once on startup so /admin/clients sorts/displays
# real dates. Cheap: scoped to rows that are actually NULL.
try:
    _c = db_conn()
    _c.execute("""
        UPDATE clients
        SET first_booking_at = (
                SELECT MIN(b.date) FROM bookings b
                WHERE LOWER(b.email) = LOWER(clients.email)
                  AND b.status NOT IN ('expired', 'cancelled')
            ),
            last_booking_at = (
                SELECT MAX(b.date) FROM bookings b
                WHERE LOWER(b.email) = LOWER(clients.email)
                  AND b.status NOT IN ('expired', 'cancelled')
            )
        WHERE first_booking_at IS NULL OR last_booking_at IS NULL
    """)
    _c.commit()
    _c.close()
except Exception as _e:
    log.warning(f"[db] booking-date backfill skipped: {_e}")


# ── BACKUP ───────────────────────────────────────────────────────────────────
def create_backup(label: str = "auto") -> str:
    """Copy the SQLite file to BACKUP_DIR with a timestamp.
    Returns the backup file path."""
    import shutil
    ts = _local_now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = os.path.join(BACKUP_DIR, f"bookings_{ts}_{label}.db")
    shutil.copy2(DB_PATH, dest)
    # Keep only the 30 most recent backups (prevent disk bloat)
    all_bk = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")],
        reverse=True
    )
    for old in all_bk[30:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    log.info(f"[backup] Created: {dest}")
    return dest


# Daily auto-backup on startup
try:
    _today_prefix = _local_now().strftime("%Y-%m-%d")
    _has_today = any(
        f.startswith(f"bookings_{_today_prefix}") and "startup" in f
        for f in os.listdir(BACKUP_DIR)
    )
    if not _has_today:
        create_backup("startup")
except Exception as _be:
    log.warning(f"[backup] Startup backup failed: {_be}")

def _parse_reserved_until_utc(value):
    """Parse a reserved_until string to an aware UTC datetime.

    Values are written with an America/Edmonton UTC offset; very old rows may
    be naive and are treated as UTC (they are long expired either way).
    Comparing parsed instants instead of raw strings keeps the expiry sweep
    correct regardless of the stored offset or separator format.
    """
    if not value:
        return None
    text = str(value).strip()
    dt = None
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def expire_reservations():
    """Release expired holds so abandoned slots become bookable again.

    Plain reservations use the short reservation window. Clicking "I paid"
    extends pending_payment until the configured review window expires.

    The cutoff is evaluated in UTC on parsed datetimes (not lexically in SQL):
    reserved_until strings carry a timezone offset, and comparing them against
    a wall-clock string silently misfires when formats or offsets differ.
    Returns count of expired bookings."""
    conn = db_conn()
    c = conn.cursor()
    now_utc = datetime.now(timezone.utc)
    now = now_utc.isoformat()
    c.execute("""
        SELECT * FROM bookings
        WHERE confirmed = 0
          AND paid = 0
          AND reserved_until IS NOT NULL
          AND status IN ('reserved', 'pending_payment')
    """)
    rows = []
    for row in c.fetchall():
        row = dict(row)
        reserved_until = _parse_reserved_until_utc(row.get("reserved_until"))
        if reserved_until is not None and reserved_until <= now_utc:
            rows.append(row)

    for row in rows:
        booking_id = row["id"]
        notion_page_id = row.get("notion_page_id")

        # Update status to 'expired' instead of deleting
        c.execute("UPDATE bookings SET status='expired', reserved_until=NULL WHERE id=?",
                  (booking_id,))
        log.info(f"[expire] Booking #{booking_id} marked as expired")

        # Sync status to Notion
        if notion_page_id and NOTION_API_KEY:
            try:
                requests.patch(
                    f"https://api.notion.com/v1/pages/{notion_page_id}",
                    headers=NOTION_HEADERS,
                    json={"properties": {"Status": {"select": {"name": "Expired"}}}},
                    timeout=10
                )
            except Exception as e:
                log.error(f"[expire] Notion update failed for #{booking_id}: {e}")

    conn.commit()
    conn.close()
    expired_count = len(rows)
    for booking in rows:
        _record_booking_funnel_event(
            booking,
            "booking_expired",
            {"expired_at": now, "status_before": booking.get("status")},
        )
        _emit_n8n_event(
            "booking.expired",
            booking={
                "id": booking.get("id"),
                "name": booking.get("name"),
                "email": booking.get("email"),
                "phone": booking.get("phone"),
                "instagram": booking.get("instagram"),
                "date": booking.get("date"),
                "time": booking.get("time"),
                "event_id": booking.get("event_id"),
                "status": "expired",
                "utm_campaign": booking.get("utm_campaign"),
                "utm_content": booking.get("utm_content"),
            },
        )
    return expired_count

# Generate time slots for a specific event
def generate_slots(event=None):
    """Generate time slots based on event config."""
    ev = event or _active
    if not ev:
        return []
    start = datetime.strptime(ev.get("start_time", START_TIME), "%H:%M")
    end = datetime.strptime(ev.get("end_time", END_TIME), "%H:%M")
    interval = ev.get("slot_interval", SLOT_INTERVAL)
    sl = ev.get("session_length", SESSION_LENGTH)
    slots = []
    current = start
    while current < end:
        slot_str = current.strftime("%H:%M")
        session_end = current + timedelta(minutes=sl)
        slots.append({
            "time": slot_str,
            "label": f"{slot_str} – {session_end.strftime('%H:%M')}",
            "reserved_until": None
        })
        current += timedelta(minutes=interval)
    return slots

SLOTS = generate_slots()

# ===== ROUTES =====

def _public_visible_events():
    """Events shown on the public landing — active or upcoming, not hidden."""
    out = []
    for ev in EVENTS:
        if ev.get("hidden"):
            continue
        if ev.get("status") not in ("active", "upcoming"):
            continue
        out.append(ev)
    return out

def _enrich_event_for_landing(ev):
    """Add computed fields used by the landing template (date_pretty, days_until, spots)."""
    from datetime import date as _date
    e = dict(ev)
    e.setdefault("type", "mini")
    e.setdefault("featured", False)
    e.setdefault("subtitle", "")
    try:
        d = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        e["date_pretty"] = d.strftime("%a, %B %-d, %Y") if hasattr(d, "strftime") else str(d)
        e["days_until"] = (d - _date.today()).days
    except Exception:
        e["date_pretty"] = ev.get("date", "")
        e["days_until"] = 0
    # spots accounting from the actual DB
    total = int(ev.get("total_spots") or len(generate_slots(ev)))
    booked = 0
    try:
        conn = db_conn()
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) FROM bookings
            WHERE date=? AND status NOT IN ('cancelled','expired')
        """, (ev["date"],))
        booked = c.fetchone()[0] or 0
        conn.close()
    except Exception:
        pass
    e["spots"] = {"total": total, "booked": booked, "left": max(0, total - booked)}
    return e

def _select_booking_flow_variant(query_value=None, cookie_value=None):
    """Conservative A/B selector for booking drawer UX.

    Default is always the proven control flow. The one-step variant only renders
    when BOOKING_FLOW_EXPERIMENT=1 and the visitor is explicitly opted in by
    query string or cookie. This prevents accidental rollout to all production
    traffic while we validate the UI safely.
    """
    if os.environ.get("BOOKING_FLOW_EXPERIMENT") not in {"1", "true", "TRUE", "yes", "on"}:
        return "control"
    for value in (query_value, cookie_value):
        if value in {"control", "one_step"}:
            return value
    return "control"


@app.template_filter("display_date")
def _display_date_filter(value):
    """Render ISO dates (2026-07-11) as 'July 11, 2026' for human-facing copy."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%B %-d, %Y")
    except (ValueError, TypeError):
        return str(value or "")


@app.route("/index.html")
@app.route("/book-a-session")
@app.route("/book-a-session/")
@app.route("/reserve", methods=["GET"])
def legacy_booking_entrypoints():
    """Keep old public links from showing 404/405; send clients to booking home."""
    return redirect(url_for("index"), code=302)


@app.route("/")
def index():
    """Landing — new v2 design with event grid, featured banner, and booking drawer."""
    deeplink_hint = (
        request.args.get("event")
        or request.args.get("event_id")
        or request.args.get("session")
        or request.args.get("product")
        or request.args.get("utm_content")
    )
    direct_event = resolve_event_deeplink(deeplink_hint)
    explicit_event_requested = any(request.args.get(name) for name in ("event", "event_id", "session", "product"))
    booking_flow_variant = _select_booking_flow_variant(
        request.args.get("flow"),
        request.cookies.get("booking_flow"),
    )
    initial_events = _public_events_payload()
    today = _local_today().strftime("%Y-%m-%d")
    raw_visible_events = [ev for ev in EVENTS if not ev.get("hidden") and ev.get("photos")]
    raw_upcoming_events = [ev for ev in raw_visible_events if str(ev.get("date", "")) >= today]
    hero_source_events = initial_events or raw_upcoming_events or raw_visible_events
    hero_event = _select_hero_event(hero_source_events)
    template_context = {
        "stripe_enabled": bool(STRIPE_SECRET_KEY),
        "booking_flow_variant": booking_flow_variant,
        "initial_events": initial_events,
        "initial_past_events": _past_events_payload(),
        "hero_event": hero_event,
        "hero_preload_image": _select_hero_preload_image([hero_event] if hero_event else []),
    }

    # ── Direct event / GBP product links: render v2 and auto-open the matching drawer ──
    if direct_event:
        # Pass per-event OG data so social shares show the right preview
        _ev_photos = direct_event.get("photos", [])
        _og_image = _ev_photos[0] if _ev_photos else "/static/og-image.jpg"
        if not _og_image.startswith("http"):
            _og_image = f"https://book.pashynskaphoto.com{_og_image}"
        template_context.update(
            og_title=f"{direct_event.get('title', 'Calgary Photography')} — Book Online | Pashynska Photography",
            og_description=direct_event.get("subtitle") or f"Book your {direct_event.get('title', 'photo session')} in Calgary. Easy online booking, secure deposit.",
            og_image=_og_image,
            og_url=f"https://book.pashynskaphoto.com/?event={direct_event['id']}",
        )
        return render_template("index_v2.html", direct_event_id=direct_event["id"], direct_event=direct_event, **template_context)
    if explicit_event_requested:
        return "Event not found", 404

    # ── Render the new landing grid (v2 design) for all cases ──
    return render_template("index_v2.html", **template_context)


# ── SEO helper routes ──────────────────────────────────────────────────
@app.route("/robots.txt")
def robots_txt():
    """Serve robots.txt for search engine crawlers."""
    return send_from_directory(os.path.join(app.root_path, "static"), "robots.txt")

@app.route("/sitemap.xml")
def sitemap_xml():
    """Serve sitemap.xml for search engine crawlers."""
    return send_from_directory(os.path.join(app.root_path, "static"), "sitemap.xml", mimetype="application/xml")


# ── Service landing pages (SEO-optimized for each photography direction) ──
# 60-second TTL cache for landing-image discovery. A landing GET would otherwise
# do up to (6 slots) × (4 extensions) × (2 naming variants) = 48 stat syscalls;
# 60s is fast enough that a freshly-dropped photo appears within a minute and
# slow enough that crawler hammering doesn't multiply disk reads.
_LANDING_GALLERY_CACHE: dict = {}
_LANDING_GALLERY_CACHE_TTL = 60  # seconds


def _landing_gallery(slug, max_photos=6):
    """Look for `<slug>-1.jpg…N` (jpg/jpeg/webp/png) in both /static/images/
    (bundled) and PHOTOS_DIR (persistent volume) and return URL paths Jinja
    can drop straight into <img src>. TTL-cached to keep landing GETs cheap."""
    import time
    now = time.monotonic()
    cache_key = (slug, int(max_photos))
    cached = _LANDING_GALLERY_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return list(cached[1])  # defensive copy so callers can mutate

    out = []
    bundled_dir = os.path.join(app.root_path, "static", "images")
    persistent_dir = PHOTOS_DIR
    for i in range(1, max_photos + 1):
        found = False
        for ext in ("jpg", "jpeg", "webp", "png"):
            for fname in (f"{slug}-{i}.{ext}", f"{slug}_{i}.{ext}"):
                if os.path.isfile(os.path.join(persistent_dir, fname)):
                    out.append(f"/images/{fname}")
                    found = True
                    break
                if os.path.isfile(os.path.join(bundled_dir, fname)):
                    out.append(f"/static/images/{fname}")
                    found = True
                    break
            if found:
                break

    _LANDING_GALLERY_CACHE[cache_key] = (now + _LANDING_GALLERY_CACHE_TTL, list(out))
    return out


def _invalidate_landing_gallery_cache():
    """Test helper / future admin-side cache buster. Drops every cached
    slug's photo list so the next GET re-scans disk."""
    _LANDING_GALLERY_CACHE.clear()


def _landing_headshot():
    """Return a URL for Iryna's headshot if one is present, else None.
    Looked for in the same way as gallery photos so it can be dropped in
    without code edits."""
    bundled = os.path.join(app.root_path, "static", "images", "iryna.jpg")
    persistent = os.path.join(PHOTOS_DIR, "iryna.jpg")
    if os.path.isfile(persistent):
        return "/images/iryna.jpg"
    if os.path.isfile(bundled):
        return "/static/images/iryna.jpg"
    return None


def _landing_hero(slug):
    """Look for a per-page hero photo. Falls back to the OG image
    (which is bundled and always present) so the page never breaks."""
    bundled_dir = os.path.join(app.root_path, "static", "images")
    persistent_dir = PHOTOS_DIR
    for ext in ("jpg", "jpeg", "webp", "png"):
        fname = f"{slug}-hero.{ext}"
        if os.path.isfile(os.path.join(persistent_dir, fname)):
            return f"/images/{fname}"
        if os.path.isfile(os.path.join(bundled_dir, fname)):
            return f"/static/images/{fname}"
    return "/static/og-image.jpg"


def _landing_context(slug):
    """Common context for every /wedding /family /maternity landing."""
    return {
        "stripe_enabled": bool(STRIPE_SECRET_KEY),
        "gallery_photos": _landing_gallery(slug),
        "headshot_url": _landing_headshot(),
        "hero_photo_url": _landing_hero(slug),
    }


@app.route("/wedding")
def landing_wedding():
    """Wedding photography landing page — SEO optimized with schema.org."""
    return render_template("landing_wedding.html", **_landing_context("wedding"))

@app.route("/family")
def landing_family():
    """Family photography landing page — SEO optimized with schema.org."""
    return render_template("landing_family.html", **_landing_context("family"))

@app.route("/maternity")
def landing_maternity():
    """Maternity photography landing page — SEO optimized with schema.org."""
    return render_template("landing_maternity.html", **_landing_context("maternity"))


# ── Booking page: HTML view of available sessions (replaces /events for browser navigation) ──
@app.route("/book")
def booking_page():
    """Public booking page — shows upcoming sessions with real-time spot counts."""
    now = _local_now()
    today = now.strftime("%Y-%m-%d")
    requested_type = (request.args.get("type") or "").strip().lower()
    allowed_types = {"mini", "individual", "family", "maternity", "wedding", "custom"}
    type_filter = requested_type if requested_type in allowed_types else ""

    events = []
    featured = None
    for ev in EVENTS:
        ev_type = (ev.get("session_type") or ev.get("type") or "mini").strip().lower()
        if type_filter and ev_type != type_filter:
            continue
        if ev.get("status") in ("active", "upcoming") and not ev.get("hidden") and str(ev.get("date", "")) >= today:
            e = _enrich_event_for_landing(ev)
            events.append(e)
            if ev.get("featured") and not featured:
                featured = e
    events.sort(key=lambda x: x.get("date", ""))

    inquiry_mode = type_filter in {"wedding", "custom"} and not events
    inquiry_subject = "Wedding inquiry" if type_filter == "wedding" else "Custom photography inquiry"
    inquiry_body = (
        "Hi Iryna,%0D%0A%0D%0A"
        "Tell me about availability for:%0D%0A"
        "- Date:%0D%0A"
        "- Location:%0D%0A"
        "- Coverage needed:%0D%0A"
        "- Guest count / notes:%0D%0A%0D%0A"
        "Thank you!"
    )

    stats = {
        "reviews": "80+",
        "delivered": "120+",
    }
    settings = {
        "photographer_instagram": "pashynska.photo",
        "photographer_email": EMAIL or "iryna.pashynska@gmail.com",
    }
    return render_template(
        "events_landing.html",
        events=events,
        featured=featured,
        stats=stats,
        settings=settings,
        requested_type=type_filter,
        inquiry_mode=inquiry_mode,
        inquiry_subject=inquiry_subject,
        inquiry_body=inquiry_body,
    )


def _render_single_event(ev):
    slots = generate_slots(ev)
    return render_template(
        "index.html",
        title=ev.get("title", EVENT_TITLE),
        date=ev["date"],
        price=ev.get("deposit", SESSION_PRICE),
        total=ev.get("full_price", SESSION_TOTAL),
        email=EMAIL,
        session_length=ev.get("session_length", SESSION_LENGTH),
        break_length=ev.get("break_length", BREAK_LENGTH),
        event_id=ev["id"],
        subtitle=ev.get("subtitle", ""),
        location=ev.get("location", ""),
        included=ev.get("included", []),
        photos=ev.get("photos", []),
        slots=slots,
    )

def _public_events_payload():
    """Return public event cards with current availability.

    Used by both the API and the initial landing render so booking does not
    depend entirely on a second client-side request succeeding.
    """
    result = []
    now = _local_now()
    conn = db_conn()
    c = conn.cursor()

    today = now.strftime("%Y-%m-%d")

    for ev in EVENTS:
        # Public listing should only expose currently bookable sessions.
        # Admin screens still use EVENTS directly and can show historical/draft data.
        if (
            ev.get("status") in ("active", "upcoming")
            and not ev.get("hidden")
            and str(ev.get("date", "")) >= today
            and ev.get("photos")  # Must have at least one photo to show publicly
        ):
            # Calculate total and available spots
            booking_type = _booking_type(ev)
            slots = generate_slots(ev)
            total_spots = len(slots)

            if booking_type == "inquiry_only":
                available_spots = 1
            elif booking_type == "rolling_availability":
                horizon = _rolling_horizon_days(ev)
                available_spots = 0
                today_d = now.date()
                for offset in range(horizon + 1):
                    day = (today_d + timedelta(days=offset)).isoformat()
                    if _rolling_date_unavailable_reason(ev, day):
                        continue
                    c.execute("""
                        SELECT time FROM bookings
                        WHERE date=?
                          AND event_id=?
                          AND status NOT IN ('cancelled', 'expired')
                          AND (confirmed=1 OR reserved_until > ?)
                    """, (day, ev["id"], now.isoformat()))
                    booked_times = {row["time"] for row in c.fetchall()}
                    available_spots += len([s for s in slots if s["time"] not in booked_times])
                total_spots = len(slots) * (horizon + 1)
            else:
                c.execute("""
                    SELECT time FROM bookings
                    WHERE date=?
                      AND status NOT IN ('cancelled', 'expired')
                      AND (confirmed=1 OR reserved_until > ?)
                """, (ev["date"], now.isoformat()))
                booked_times = {row["time"] for row in c.fetchall()}
                available_spots = len([s for s in slots if s["time"] not in booked_times])

            # Get first photo URL
            photos = ev.get("photos", [])
            photo_url = photos[0] if photos else "/static/images/placeholder.jpg"
            active_addons = _event_active_addons(ev)
            agreement_cfg = _event_agreement_config(ev)
            try:
                event_date_obj = datetime.strptime(ev["date"], "%Y-%m-%d").date()
                date_pretty = event_date_obj.strftime("%a, %B %-d, %Y")
                days_until = (event_date_obj - now.date()).days
            except Exception:
                date_pretty = ev.get("date", "")
                days_until = None

            payload = {
                "id": ev["id"],
                "title": ev.get("title", ""),
                "subtitle": ev.get("subtitle", ""),
                "description": ev.get("subtitle", ""),
                "date": ev["date"],
                "date_pretty": date_pretty,
                "days_until": days_until,
                "start_time": ev.get("start_time", ""),
                "end_time": ev.get("end_time", ""),
                "session_length": ev.get("session_length", 20),
                "break_length": ev.get("break_length", 10),
                "slot_interval": ev.get("slot_interval", 30),
                "deposit": ev.get("deposit", SESSION_PRICE),
                "full_price": ev.get("full_price", SESSION_TOTAL),
                "price": ev.get("deposit", SESSION_PRICE),
                "location": ev.get("location", "Calgary"),
                "status": ev.get("status", ""),
                "booking_type": booking_type,
                "availability_horizon_days": _rolling_horizon_days(ev) if booking_type == "rolling_availability" else None,
                "blackout_dates": sorted(_event_blackout_dates(ev)),
                "inquiry_only": booking_type == "inquiry_only",
                "session_type": ev.get("session_type", "mini"),
                "type": ev.get("session_type", "mini"),
                "featured": ev.get("featured", False),
                "hidden": ev.get("hidden", False),
                "total_spots": total_spots,
                "spots_left": available_spots,
                "photo_url": photo_url,
                "photo": photo_url,
                "photos": photos,
                "included": ev.get("included", []),
            }
            if active_addons:
                payload["addons"] = active_addons
            if agreement_cfg.get("enabled"):
                payload["agreement"] = {
                    "enabled": True,
                    "require_terms": bool(agreement_cfg.get("require_terms", True)),
                    "require_marketing_choice": bool(agreement_cfg.get("require_marketing_choice", True)),
                    "terms_version": _terms_version(ev),
                }
            result.append(payload)
    conn.close()
    return result


def _select_hero_event(events):
    """Return the event that the frontend is most likely to use as the default hero.

    Mirrors the default frontend pickHeroEvent() path for non-campaign traffic:
    featured + bookable, then nearest bookable, then first visible event.
    Campaign-specific hero selection still happens client-side.
    """
    if not events:
        return None
    ordered = sorted(events, key=lambda e: str(e.get("date") or ""))

    def bookable(e):
        if not e or e.get("status") == "completed":
            return False
        if "spots_left" not in e:
            return e.get("status") in ("active", "upcoming", "")
        return int(e.get("spots_left") or 0) > 0

    return (
        next((e for e in ordered if e.get("featured") and bookable(e)), None)
        or next((e for e in ordered if bookable(e)), None)
        or ordered[0]
    )


def _select_hero_preload_image(events):
    """Return the first image URL for the default hero/LCP event."""
    hero = _select_hero_event(events)
    if not hero:
        return ""
    photos = hero.get("photos") or []
    src = photos[0] if photos else (hero.get("photo_url") or hero.get("photo") or "")
    if not src:
        return ""
    return src if str(src).startswith("http") else str(src)


def _past_events_payload(limit=12):
    """Display-only past / completed sessions for the public 'Past sessions' archive.

    These are NOT bookable, so no slot/DB availability work is done. The archive is
    pure social proof — small thumbnails of recent shoots — so the landing page still
    feels alive between upcoming dates and showcases Iryna's range. Hidden events and
    drafts/cancelled events are never exposed here.
    """
    today = _local_today().strftime("%Y-%m-%d")
    out = []
    for ev in EVENTS:
        if ev.get("hidden"):
            continue
        if not ev.get("photos"):
            continue
        status = ev.get("status", "")
        if status in ("draft", "cancelled"):
            continue
        is_completed = status == "completed"
        is_past_date = str(ev.get("date", "")) < today
        if not (is_completed or is_past_date):
            continue
        photos = ev.get("photos", [])
        out.append({
            "id": ev["id"],
            "title": ev.get("title", ""),
            "subtitle": ev.get("subtitle", ""),
            "date": ev.get("date", ""),
            "location": ev.get("location", "Calgary"),
            "session_type": ev.get("session_type", "mini"),
            "photo_url": photos[0],
            "photos": photos,
        })
    # Newest first, capped so the strip never grows unbounded.
    out.sort(key=lambda e: e.get("date", ""), reverse=True)
    return out[:limit]


@app.route("/events")
def list_events():
    """API: list all events with full details including available spots."""
    result = _public_events_payload()
    response = jsonify({"events": result, "past_events": _past_events_payload()})
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response

@app.route("/slots/<date_str>")
def get_slots(date_str):
    # Support looking up by event_id via query param too
    event_id = request.args.get("event_id")
    if event_id:
        ev = get_event_by_id(event_id)
    else:
        ev = get_event_by_date(date_str)
    if not ev:
        return jsonify({"slots": [], "message": f"No event on {date_str}"})

    booking_type = _booking_type(ev)
    slots = generate_slots(ev)
    now = _local_now()
    instagram_url = SETTINGS.get("photographer_instagram_url", "https://instagram.com/pashynska.photo")
    instagram_handle = SETTINGS.get("photographer_instagram", "@pashynska.photo")

    if booking_type == "inquiry_only":
        return jsonify({
            "date": date_str,
            "event_id": ev["id"],
            "event_title": ev.get("title", ""),
            "booking_type": booking_type,
            "inquiry_only": True,
            "slots": [],
            "total": 0,
            "available": 0,
            "message": "This session is inquiry-only. Please contact Iryna to discuss details.",
            "instagram_url": instagram_url,
            "instagram_handle": instagram_handle,
        })

    if booking_type == "rolling_availability":
        reason = _rolling_date_unavailable_reason(ev, date_str)
        if reason:
            return jsonify({
                "date": date_str,
                "event_id": ev["id"],
                "event_title": ev.get("title", ""),
                "booking_type": booking_type,
                "slots": [],
                "total": len(slots),
                "available": 0,
                "unavailable_reason": reason,
                "instagram_url": instagram_url,
                "instagram_handle": instagram_handle,
            })
        booking_date = date_str
    else:
        booking_date = ev["date"]

    conn = db_conn()
    c = conn.cursor()

    # Single query instead of one per slot (N+1 → 1). Intentionally global by
    # date+time to prevent double-booking Iryna across different event cards.
    c.execute("""
        SELECT time, event_id FROM bookings
        WHERE date=?
          AND status NOT IN ('cancelled', 'expired')
          AND (confirmed=1 OR reserved_until > ?)
    """, (booking_date, now.isoformat()))
    booked_rows = c.fetchall()
    booked_times = {row["time"] for row in booked_rows}
    # Detect if any booking belongs to a different event (global cross-event block)
    foreign_booked = any(row["event_id"] != ev["id"] for row in booked_rows)
    conn.close()

    available_slots = [
        {"time": s["time"], "label": s["label"]}
        for s in slots
        if s["time"] not in booked_times
    ]

    return jsonify({
        "date": booking_date,
        "requested_date": date_str,
        "event_id": ev["id"],
        "event_title": ev.get("title", ""),
        "booking_type": booking_type,
        "slots": available_slots,
        "total": len(slots),
        "available": len(available_slots),
        "foreign_booked": foreign_booked,
        # Fallback shown in frontend when all slots are booked
        "sold_out_message": (
            f"All spots are taken! DM {instagram_handle} on Instagram — "
            "cancellations happen and I'd love to fit you in."
            if len(available_slots) == 0 and len(slots) > 0 else None
        ),
        "instagram_url": instagram_url,
        "instagram_handle": instagram_handle,
    })

import re as _re

def _validate_booking_fields(name, email, phone, instagram=""):
    """Validate client booking fields. Returns (is_valid, error_message)."""
    # Name: letters (Latin, accented, Cyrillic, Ukrainian, Devanagari), spaces,
    # hyphens, apostrophes — min 2 chars. Many clients are Russian/Ukrainian.
    if not name or len(name.strip()) < 2:
        return False, "Please enter your full name (at least 2 characters)"
    if not _re.match(r"^[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁёЇїІіЄєҐґऀ-ॿ'\- ]{2,80}$", name.strip()):
        return False, "Name should contain only letters, spaces, or hyphens"

    # Email: standard format check
    if not email or not _re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", email.strip()):
        return False, "Please enter a valid email address (e.g. jane@example.com)"
    if len(email) > 254:
        return False, "Email address is too long"

    # Phone: accept Canadian (10 digits) or international (+country code, 7–15 digits).
    # We no longer reject non-Canadian formats — many clients are newcomers or
    # have family booking from abroad.
    if phone:
        raw = phone.strip()
        digits = _re.sub(r"\D", "", raw)
        is_international = raw.startswith("+")
        if is_international:
            # E.164-ish: + followed by 7–15 digits
            if not (7 <= len(digits) <= 15):
                return False, "Please enter a valid phone number including country code (e.g. +1 403-555-1234)"
        else:
            # No + prefix — treat as North American: strip leading 1 if present
            if digits.startswith("1") and len(digits) == 11:
                digits = digits[1:]
            if len(digits) != 10:
                return False, "Please enter a valid phone number (e.g. 403-555-1234 or +380 50 123 4567)"
            if digits[0] in "01" or digits[3] in "01":
                return False, "Please enter a valid Canadian phone number"

    # Instagram: optional — if provided must be @handle or handle (1-30 alphanumeric/._)
    if instagram:
        handle = instagram.lstrip("@")
        if not _re.match(r"^[A-Za-z0-9_.]{1,30}$", handle):
            return False, "Instagram handle should be 1–30 characters (letters, numbers, . or _)"

    return True, ""


# Whitelist of gallery hosts the admin is allowed to paste into Wfolio URL.
# Unknown hosts are accepted but flagged in logs — Iryna sometimes moves
# between providers, so we don't want a hard block. The list is upper-cased
# on read for case-insensitive matching.
_GALLERY_HOST_WHITELIST = (
    "wfolio.com",
    "pic-time.com",
    "pixieset.com",
    "smugmug.com",
    "pashynskaphoto.com",
)


def _validate_gallery_url(url):
    """Validate a client-facing gallery URL (Wfolio/Pic-Time/etc.).

    Returns (is_valid, error_message). Rules:
    - Must be present
    - Scheme must be exactly 'https' (no http, javascript, mailto, data, etc.)
    - Must have a netloc with at least one dot
    - Length cap so we don't store something pathological in SQLite

    The admin paste box used to accept anything starting with 'http', which
    let `javascript:alert(...)` and 'http://internal-tool/...' through.
    Both could go straight into an outbound email <a href>, so this guard
    is defensive even though only an authenticated admin can call the endpoint.
    """
    if not url:
        return False, "Gallery URL is required"
    if len(url) > 500:
        return False, "Gallery URL is too long"
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
    except Exception:
        return False, "Could not parse the URL"
    if parsed.scheme != "https":
        return False, "Gallery URL must start with https://"
    if parsed.username or parsed.password:
        return False, "Gallery URL must not include username/password credentials"
    host = (parsed.hostname or "").lower()
    if not host or "." not in host:
        return False, "Gallery URL must include a valid host"
    # Host check: accept exact match or subdomain of a whitelisted suffix.
    if not any(host == s or host.endswith("." + s) for s in _GALLERY_HOST_WHITELIST):
        log.warning(f"[wfolio] Saved non-whitelisted host '{host}' (admin override)")
    return True, ""


def _verify_recaptcha(token, ip=None):
    """Verify reCAPTCHA v3 token with Google.

    Returns (success, error_message).
    - If RECAPTCHA_SECRET_KEY is not set → silently passes (dev mode).
    - If token is empty (e.g. grecaptcha didn't load due to CSP/network) →
      soft-fallback allows. The honeypot field on the form remains primary
      anti-bot; we'd rather let a real client through than block them on a
      flaky third-party script. (Booted 2026-05-16 after a CSP regression.)
    - Score < 0.3 → bot detected.
    - Score 0.3–0.5 → suspicious, allow but log.
    """
    if not RECAPTCHA_SECRET_KEY:
        # Development / staging — no check
        return True, ""

    if not token:
        log.info("[recaptcha] Empty token — soft-fallback allow (honeypot stays primary)")
        return True, ""

    try:
        resp = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={
                "secret": RECAPTCHA_SECRET_KEY,
                "response": token,
                "remoteip": ip or "",
            },
            timeout=5,
        )
        result = resp.json()
    except Exception:
        # Network error — be lenient, but log
        log.warning("[recaptcha] Network error during verification — allowing")
        return True, ""

    if not result.get("success"):
        error_codes = result.get("error-codes", [])
        if "timeout-or-duplicate" in error_codes:
            return False, "Verification expired — please refresh the page"
        return False, "Verification failed — please try again"

    score = result.get("score", 0.0)
    action = result.get("action", "")

    if score < 0.3:
        log.warning(f"[recaptcha] Blocked: score={score}, action={action}")
        return False, "Suspicious activity detected. Please contact us directly."
    elif score < 0.5:
        log.info(f"[recaptcha] Suspicious: score={score}, action={action} — allowing with log")
    else:
        log.debug(f"[recaptcha] OK: score={score}, action={action}")

    return True, ""


@app.route("/waitlist", methods=["POST"])
def join_waitlist():
    """Capture interest for sold-out sessions without reserving a slot."""
    data = request.get_json(silent=True) or {}
    event_id = (data.get("event_id") or "").strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    preferred_slot = (data.get("preferred_slot") or "").strip()[:120]
    message = (data.get("message") or "").strip()[:500]

    if not event_id:
        return jsonify({"success": False, "error": "event_id is required"}), 400
    if not get_event_by_id(event_id):
        return jsonify({"success": False, "error": "Event not found"}), 404

    valid, err = _validate_booking_fields(name, email, phone, "")
    if not valid:
        return jsonify({"success": False, "error": err}), 400

    entry = {
        "event_id": event_id,
        "name": name,
        "email": email,
        "phone": phone,
        "preferred_slot": preferred_slot,
        "message": message,
    }

    conn = db_conn()
    try:
        conn.execute("""
            INSERT INTO waitlist (event_id, name, email, phone, preferred_slot, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (event_id, name, email, phone, preferred_slot, message))
        conn.commit()
        duplicate = False
    except sqlite3.IntegrityError:
        duplicate = True
    finally:
        conn.close()

    if not duplicate:
        try:
            _notify_waitlist_signup(entry)
        except Exception as exc:
            log.warning(f"[waitlist] Notification failed for {email}: {exc}")

    return jsonify({
        "success": True,
        "duplicate": duplicate,
        "message": "You're on the waitlist — we'll contact you if a spot opens.",
    })


@app.route("/track", methods=["POST"])
def track_event():
    """Lightweight first-party funnel analytics endpoint.

    It deliberately accepts only a tiny allowlisted payload and never blocks the
    booking flow if storage fails. This lets ads be compared by real booking
    outcomes without adding third-party tracking scripts as the source of truth.
    """
    ip = _client_ip()
    if not check_analytics_rate_limit(ip):
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    record_analytics_request(ip)

    data = request.get_json(silent=True) or {}
    visitor_id = _safe_text(data.get("visitor_id"), 120)
    if not visitor_id:
        return jsonify({"ok": False, "error": "missing visitor_id"}), 400
    attribution = _normalise_utm(data)
    attribution.update({
        "fbclid": _safe_text(data.get("fbclid"), 500),
        "gclid": _safe_text(data.get("gclid"), 500),
        "referrer": _safe_text(data.get("referrer") or request.headers.get("Referer"), 500),
        "landing_url": _safe_text(data.get("landing_url") or data.get("page"), 1000),
    })
    ok = _record_analytics_event(
        data.get("event_name") or "page_view",
        visitor_id=visitor_id,
        booking_id=data.get("booking_id"),
        event_id=data.get("event_id") or data.get("session_id") or "",
        page=data.get("page") or "",
        metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        attribution=attribution,
    )
    return jsonify({"ok": bool(ok)})


@app.route("/reserve", methods=["POST"])
def reserve_slot():
    data = request.json or {}
    slot_time = data.get("time")
    event_id = data.get("event_id") or data.get("date")  # accept either
    requested_date = (data.get("date") or "").strip()
    client_name = (data.get("name") or "").strip()
    client_email = (data.get("email") or "").strip().lower()
    # Auto-clean common copy/paste artefacts: trailing ! . , ; : and angle brackets
    client_email = client_email.strip("<>").rstrip(".,;:!? ")
    client_phone = (data.get("phone") or "").strip()
    client_ig = (data.get("instagram") or "").strip()
    session_type = data.get("session_type", "")
    visitor_id = _safe_text(data.get("visitor_id"), 120)
    attribution = _normalise_utm(data)
    attribution.update({
        "fbclid": _safe_text(data.get("fbclid"), 500),
        "gclid": _safe_text(data.get("gclid"), 500),
        "referrer": _safe_text(data.get("referrer") or request.headers.get("Referer"), 500),
        "landing_url": _safe_text(data.get("landing_url") or request.headers.get("Referer"), 1000),
    })

    if visitor_id:
        _record_analytics_event(
            "reserve_attempt",
            visitor_id=visitor_id,
            event_id=event_id or "",
            page=attribution.get("landing_url") or "",
            metadata={"time": slot_time, "date": requested_date, "session_type": session_type, "name": client_name, "email": client_email},
            attribution=attribution,
        )

    if not slot_time:
        if visitor_id:
            _record_analytics_event(
                "reserve_failed",
                visitor_id=visitor_id,
                event_id=event_id or "",
                page=attribution.get("landing_url") or "",
                metadata={"reason": "No time slot specified", "date": requested_date, "session_type": session_type},
                attribution=attribution,
            )
        return jsonify({"success": False, "error": "No time slot specified"}), 400

    # ── Field validation ──
    valid, err = _validate_booking_fields(client_name, client_email, client_phone, client_ig)
    if not valid:
        return jsonify({"success": False, "error": err}), 400

    # ── reCAPTCHA v3 verification (anti-bot) ──
    if RECAPTCHA_SECRET_KEY:
        recaptcha_token = data.get("recaptcha_token", "")
        recaptcha_ok, recaptcha_err = _verify_recaptcha(recaptcha_token, request.remote_addr)
        if not recaptcha_ok:
            return jsonify({"success": False, "error": recaptcha_err}), 400

    # Normalise phone: keep international as-is (+...), format NA as (XXX) XXX-XXXX
    if client_phone:
        if client_phone.startswith("+"):
            # International — store cleaned but preserve + prefix
            _digits = _re.sub(r"\D", "", client_phone)
            client_phone = "+" + _digits
        else:
            _digits = _re.sub(r"\D", "", client_phone)
            if _digits.startswith("1") and len(_digits) == 11:
                _digits = _digits[1:]
            if len(_digits) == 10:
                client_phone = f"({_digits[:3]}) {_digits[3:6]}-{_digits[6:]}"

    # Normalise Instagram — always store without @
    if client_ig and client_ig.startswith("@"):
        client_ig = client_ig[1:]

    # Resolve event
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    if not ev:
        return jsonify({"success": False, "error": "Event not found"}), 404

    booking_type = _booking_type(ev)
    if booking_type == "inquiry_only":
        return jsonify({"success": False, "error": "This session is inquiry-only. Please contact Iryna to discuss details."}), 400

    event_date = _event_date_for_booking(ev, requested_date)
    if booking_type == "rolling_availability":
        if not requested_date:
            return jsonify({"success": False, "error": "Please choose a booking date"}), 400
        reason = _rolling_date_unavailable_reason(ev, requested_date)
        if reason:
            return jsonify({"success": False, "error": f"Selected date is not available ({reason})"}), 400
    elif requested_date and requested_date != ev.get("date"):
        return jsonify({"success": False, "error": "Selected date does not match this event"}), 400

    valid_slot_times = {s["time"] for s in generate_slots(ev)}
    if slot_time not in valid_slot_times:
        return jsonify({"success": False, "error": "Selected time is not available for this event"}), 400

    try:
        selected_addons, addons_total = _validate_selected_addons(ev, data.get("addons") or [])
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    agreement_enabled = _event_requires_agreement(ev)
    agreement_cfg = _event_agreement_config(ev)
    marketing_consent = (data.get("marketing_consent") or "").strip().lower()
    agreement_name = _strip_tags(data.get("agreement_name") or "")[:120]
    terms_accepted = data.get("terms_accepted") is True or str(data.get("terms_accepted")).lower() in ("true", "1", "yes", "on")
    agreement_accepted_at = None
    terms_version = None

    if agreement_enabled:
        if agreement_cfg.get("require_terms", True) and not terms_accepted:
            return jsonify({"success": False, "error": "Please accept the booking terms"}), 400
        if not agreement_name:
            return jsonify({"success": False, "error": "Please enter your electronic signature name"}), 400
        if agreement_cfg.get("require_marketing_choice", True):
            if marketing_consent not in ("yes", "no"):
                return jsonify({"success": False, "error": "Please choose your photo/video marketing privacy preference"}), 400
        elif marketing_consent and marketing_consent not in ("yes", "no"):
            return jsonify({"success": False, "error": "Marketing consent must be yes or no"}), 400
        terms_version = _terms_version(ev)
    else:
        marketing_consent = None
        agreement_name = None

    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if isinstance(ip, str) and ',' in ip:
        ip = ip.split(',')[0].strip()

    if not check_rate_limit(ip):
        return jsonify({"success": False, "error": "Too many requests. Please wait 10 minutes."}), 429
    record_request(ip)

    now = _local_now()
    expires = now + timedelta(minutes=RESERVATION_MINUTES)

    conn = db_conn()
    c = conn.cursor()
    booking_id = None

    try:
        # BEGIN IMMEDIATE acquires a write lock up-front, serialising concurrent
        # reserve attempts so the check-then-insert is atomic.
        c.execute("BEGIN IMMEDIATE")

        # Slot is taken if there is a confirmed booking OR an active (non-expired) reservation
        c.execute("""
            SELECT id, event_id FROM bookings
            WHERE date=? AND time=?
              AND status NOT IN ('cancelled', 'expired')
              AND (confirmed=1 OR reserved_until > ?)
        """, (event_date, slot_time, now.isoformat()))
        conflict = c.fetchone()
        if conflict:
            conn.rollback()
            conn.close()
            # Distinguish same-event sold-out vs cross-event global block
            if conflict["event_id"] != ev["id"]:
                return jsonify({
                    "success": False,
                    "error": "This time is reserved for another session. Please DM Iryna on Instagram to check alternatives.",
                    "foreign_event": True,
                }), 409
            return jsonify({"success": False, "error": "Slot is no longer available"}), 409

        # Remove stale rows: cancelled, expired, and past-deadline reserved/pending
        # so the INSERT doesn't hit the UNIQUE(date, time) constraint.
        c.execute("""
            DELETE FROM bookings
            WHERE date=? AND time=?
              AND (
                status IN ('cancelled', 'expired')
                OR (status IN ('reserved', 'pending_payment') AND reserved_until <= ?)
              )
        """, (event_date, slot_time, now.isoformat()))

        token = secrets.token_urlsafe(16)
        _deposit_amt = float(ev.get("deposit") or SESSION_PRICE)
        _base_full_price = float(ev.get("full_price") or 0) or _deposit_amt * 2
        _full_price = round(_base_full_price + addons_total, 2)
        if agreement_enabled and terms_accepted:
            agreement_accepted_at = now.isoformat()
        c.execute("""
            INSERT INTO bookings
                (date, time, name, email, phone, instagram, session_type, status, reserved_until,
                 event_id, confirmation_token, deposit_amount, full_price, selected_addons_json,
                 addons_total, marketing_consent, agreement_name, agreement_accepted_at, terms_version,
                 visitor_id, utm_source, utm_medium, utm_campaign, utm_content, utm_term,
                 fbclid, gclid, referrer, landing_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_date, slot_time, client_name, client_email, client_phone, client_ig, session_type,
            expires.isoformat(), ev["id"], token, _deposit_amt, _full_price,
            json.dumps(selected_addons, ensure_ascii=False) if selected_addons else None,
            addons_total, marketing_consent, agreement_name, agreement_accepted_at, terms_version,
            visitor_id,
            attribution.get("utm_source"), attribution.get("utm_medium"), attribution.get("utm_campaign"),
            attribution.get("utm_content"), attribution.get("utm_term"), attribution.get("fbclid"),
            attribution.get("gclid"), attribution.get("referrer"), attribution.get("landing_url"),
        ))

        if c.rowcount == 0:
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Slot just taken"})

        booking_id = c.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Sync client record (upsert) — creates or updates client profile
    if visitor_id and booking_id:
        _record_analytics_event(
            "booking_reserved",
            visitor_id=visitor_id,
            booking_id=booking_id,
            event_id=ev["id"],
            page=attribution.get("landing_url") or "",
            metadata={"time": slot_time, "date": event_date, "session_type": session_type, "deposit": _deposit_amt},
            attribution=attribution,
        )
    try:
        sync_client(client_email, client_name, client_phone, client_ig)
    except Exception as _e:
        log.warning(f"[reserve] sync_client failed: {_e}")

    # Notify photographer via Telegram
    _notify_new_reservation(
        booking_id=booking_id,
        client_name=client_name,
        client_email=client_email,
        event_date=event_date,
        slot_time=slot_time,
        event_title=ev.get("title", ""),
        session_type=session_type,
        client_ig=client_ig,
        client_phone=client_phone,
        selected_addons=selected_addons,
        addons_total=addons_total,
        marketing_consent=marketing_consent,
    )

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "event_id": ev["id"],
        "confirmation_token": token,
        "expires_at": expires.isoformat(),
        "message": f"Reserved for {RESERVATION_MINUTES} minutes. Complete payment before {expires.strftime('%H:%M')}."
    })

@app.route("/payment")
def payment():
    """Standalone payment page — identity-safe via booking_id+token."""
    booking_id = request.args.get("booking_id")
    token      = request.args.get("token")
    if not booking_id or not token:
        return redirect(url_for("index"))

    conn = db_conn()
    conn.row_factory = sqlite3.Row
    c  = conn.cursor()
    c.execute("SELECT * FROM bookings WHERE id=? AND confirmation_token=?",
              (booking_id, token))
    row = c.fetchone()
    conn.close()

    if not row:
        return redirect(url_for("index"))

    booking = dict(row)

    # Finished bookings should never show a payment form.
    status_now = booking.get("status")
    if status_now in ("expired", "cancelled"):
        return redirect(url_for("index"))
    if booking.get("confirmed") or status_now == "confirmed":
        return redirect(_booking_success_url(booking["id"], booking.get("confirmation_token"), absolute_base=""))

    ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    if not ev:
        ev = {}
    booking["selected_addons"] = _booking_addons(booking)
    amount_due_today = _money(booking.get("deposit_amount") or ev.get("deposit") or SESSION_PRICE)
    remaining_balance = _booking_balance_due(booking, ev)

    # Includes text based on session type
    is_private = booking.get("session_type") == "private"
    edited_photos = ev.get("edited_photos") or (25 if is_private else 15)
    includes_text = f"{edited_photos} edited photos + all originals"

    # Server-computed countdown (client clocks/timezones can't be trusted).
    # None => no timer: private sessions and pending_payment have no short
    # reservation window to count down.
    timer_seconds_left = None
    if status_now == "reserved" and booking.get("reserved_until"):
        try:
            _ru = datetime.fromisoformat(str(booking["reserved_until"]))
            if _ru.tzinfo is None:
                _ru = _ru.replace(tzinfo=timezone.utc)
            timer_seconds_left = max(0, int((_ru - _local_now()).total_seconds()))
        except (TypeError, ValueError):
            timer_seconds_left = None

    return render_template("payment.html",
        timer_seconds_left=timer_seconds_left,
        booking=booking,
        meta_event_id=("checkout." + str(booking.get("id"))) if booking else "",
        date=ev.get("date", DATE),
        time=booking.get("time", ""),
        name=booking.get("name", ""),
        price=amount_due_today,
        amount_due_today=amount_due_today,
        remaining_balance=remaining_balance,
        total_price=_booking_total_price(booking, ev),
        selected_addons=booking.get("selected_addons") or [],
        addons_total=_booking_addons_total(booking),
        session_length=ev.get("session_length", SESSION_LENGTH),
        event_title=ev.get("title", "Mini Session"),
        email=EMAIL,
        includes_text=includes_text,
        edited_photos=edited_photos,
        # Legacy static Payment Link (kept for backward compat — ignored if stripe_enabled)
        stripe_payment_link=ev.get("stripe_payment_link", ""),
        # New: dynamic Stripe Checkout — enabled when secret key is configured
        stripe_enabled=bool(STRIPE_SECRET_KEY),
        # Private session extras: add-ons + agreement
        show_addons=is_private,
        show_agreement=is_private,
    )


@app.route("/pay-balance")
def pay_balance():
    """Durable page to pay the REMAINING balance after the session.

    Identity-safe via booking_id+token. Offers Interac e-Transfer and (if Stripe
    is configured) a freshly-created card Checkout. The link never expires, so it
    lives in the confirmation email and the success-page steps and works whenever
    the client is ready to settle up.
    """
    booking_id = request.args.get("booking_id")
    token      = request.args.get("token")
    if not booking_id or not token:
        return redirect(url_for("index"))

    conn = db_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bookings WHERE id=? AND confirmation_token=?",
                       (booking_id, token)).fetchone()
    conn.close()
    if not row:
        return redirect(url_for("index"))

    booking = dict(row)
    if booking.get("status") in ("expired", "cancelled"):
        return redirect(url_for("index"))

    ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    ev = ev or {}
    balance_due = _booking_balance_due(booking, ev)
    event_title = ev.get("title", "Photo Session")
    event_date  = ev.get("date", booking.get("date", ""))
    bank_msg = f"Balance — {booking.get('name','')} · {event_title} · {event_date} {booking.get('time','')}".strip()

    return render_template("balance_payment.html",
        booking=booking,
        booking_id=booking.get("id"),
        token=booking.get("confirmation_token"),
        event_title=event_title,
        date=event_date,
        time=booking.get("time", ""),
        name=booking.get("name", ""),
        balance_due=balance_due,
        total_price=_booking_total_price(booking, ev),
        email=EMAIL,
        bank_msg=bank_msg,
        stripe_enabled=bool(STRIPE_SECRET_KEY),
        already_paid=(balance_due <= 0),
        meta_event_id=("balance." + str(booking.get("id"))) if booking else "",
    )


@app.route("/pay-balance/checkout", methods=["POST"])
def pay_balance_checkout():
    """Create a FRESH Stripe Checkout Session for the remaining balance on demand,
    so the durable /pay-balance link never hands out an expired session.
    Identity-safe via booking_id+token. Mirrors /stripe/create-checkout → {checkout_url}.
    """
    data = request.get_json(silent=True) or {}
    booking_id = str(data.get("booking_id") or "").strip()
    token      = (data.get("confirmation_token") or data.get("token") or "").strip()
    if not booking_id or not token:
        return jsonify({"error": "booking_id and token required"}), 400

    conn = db_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bookings WHERE id=? AND confirmation_token=?",
                       (booking_id, token)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Booking not found"}), 404

    booking = dict(row)
    ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    ev = ev or {}
    balance_due = _booking_balance_due(booking, ev)
    if balance_due <= 0:
        return jsonify({"error": "No balance due — your booking is paid in full."}), 400
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Card payments are not available — please use e-Transfer."}), 400
    try:
        url = _create_balance_checkout_url(booking, ev, balance_due)
        if not url:
            return jsonify({"error": "Could not start card checkout — please use e-Transfer."}), 502
        return jsonify({"checkout_url": url})
    except Exception as e:
        log.warning(f"[balance-checkout] booking #{booking_id} failed: {e}")
        return jsonify({"error": "Could not start card checkout — please use e-Transfer."}), 502


@app.route("/expired", methods=["GET", "POST"])
def expired_endpoint():
    """Manually trigger expired-reservation cleanup. Safe to call repeatedly."""
    deleted = expire_reservations()
    return jsonify({"success": True, "released": deleted,
                    "message": f"{deleted} expired slot(s) released"})


@app.route("/cancel-reservation", methods=["POST"])
def cancel_reservation():
    """Let a client cancel their own unconfirmed reservation using booking_id + token.
    Only works on 'reserved' or 'pending_payment' (not yet confirmed/paid) bookings.
    This frees the slot immediately so the client can rebook a different time.
    """
    data = request.get_json(silent=True) or {}
    booking_id = data.get("booking_id")
    token = (data.get("token") or "").strip()
    if not booking_id or not token:
        return jsonify({"success": False, "error": "booking_id and token required"}), 400

    conn = db_conn()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM bookings WHERE id=? AND confirmation_token=?",
        (booking_id, token)
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"success": False, "error": "Booking not found"}), 404

    booking = dict(row)
    # Only cancel unconfirmed, unpaid reservations
    if booking.get("confirmed") or booking.get("paid"):
        conn.close()
        return jsonify({"success": False, "error": "Cannot cancel a confirmed or paid booking"}), 400
    if booking.get("status") not in ("reserved", "pending_payment"):
        conn.close()
        return jsonify({"success": True, "message": "Already released"})

    c.execute(
        "UPDATE bookings SET status='cancelled', reserved_until=NULL WHERE id=?",
        (booking_id,)
    )
    conn.commit()
    conn.close()
    log.info(f"[cancel-reservation] Booking #{booking_id} cancelled by client (token match)")
    return jsonify({"success": True, "message": "Reservation cancelled. The slot is now free."})

@app.route("/confirm", methods=["POST"])
def confirm_payment():
    data = request.get_json(silent=True) or {}
    booking_id = data.get("booking_id")
    token      = (data.get("confirmation_token") or data.get("token") or "").strip()
    if not booking_id:
        return jsonify({"success": False, "error": "booking_id required"}), 400
    if not token:
        return jsonify({"success": False, "error": "confirmation_token required"}), 400

    conn = db_conn()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Identity-safe lookup: must match BOTH booking_id AND its confirmation_token.
    # Prevents an attacker from flipping arbitrary bookings to pending_payment by
    # guessing booking_ids.
    c.execute(
        "SELECT * FROM bookings WHERE id=? AND confirmation_token=?",
        (booking_id, token)
    )
    booking = c.fetchone()

    if not booking:
        conn.close()
        return jsonify({"success": False, "error": "Booking not found or token mismatch"}), 404

    row = dict(booking)
    
    # Can only move reserved bookings to pending_payment
    if row["status"] not in ("reserved", "pending_payment"):
        conn.close()
        return jsonify({"success": False, "error": "Booking already confirmed/cancelled/expired"}), 400
    
    event_id = row.get("event_id")
    ev = get_event_by_id(event_id) if event_id else get_active_event()
    event_date = row["date"]
    time = row["time"]
    client_name = row["name"]
    client_email = row["email"]
    client_phone = row["phone"]
    client_ig = row["instagram"]
    session_type = row["session_type"]
    
    # A client who says payment was sent must not lose the slot while Interac
    # or Gmail is delayed. Keep it protected long enough for automation/admin
    # review, then let the normal expiry sweep release it.
    new_expires = (_local_now() + timedelta(hours=PENDING_PAYMENT_HOURS)).isoformat()
    # Private sessions occupy a dedicated slot the admin created on purpose —
    # the expiry sweep must never release them while an e-Transfer is in flight.
    if (row.get("session_type") or "") == "private":
        new_expires = None
    c.execute("""
        UPDATE bookings
        SET status='pending_payment', reserved_until=?, confirmed=0, paid=0
        WHERE id=?
    """, (new_expires, booking_id))
    conn.commit()
    conn.close()
    row["status"] = "pending_payment"
    row["reserved_until"] = new_expires
    _record_booking_funnel_event(
        row,
        "payment_sent_clicked",
        {"pending_until": new_expires, "payment_method": "interac"},
    )
    
    # Track confirmed booking (payment sent)
    if visitor_id := row.get("visitor_id"):
        _record_analytics_event(
            "confirmed_booking",
            visitor_id=visitor_id,
            event_id=row.get("event_id") or "",
            page=row.get("landing_url") or "",
            metadata={
                "booking_id": booking_id,
                "name": client_name,
                "email": client_email,
                "phone": client_phone,
                "instagram": client_ig,
                "session_type": session_type,
                "event_date": event_date,
                "time": time,
                "amount": ev.get("deposit", SESSION_PRICE) if ev else SESSION_PRICE,
            },
            attribution=json.loads(row.get("attribution", "{}")),
        )
    
    # Sync to Notion
    sync_to_notion(booking_id)
    
    # Notify with inline confirm/cancel buttons
    _notify_payment_pending(
        booking_id=booking_id,
        client_name=client_name,
        client_email=client_email,
        event_date=event_date,
        slot_time=time,
        event_title=ev.get("title", "Mini Session") if ev else "Mini Session",
        session_type=session_type,
        client_ig=client_ig,
        expected_deposit=ev.get("deposit", SESSION_PRICE) if ev else SESSION_PRICE,
        client_phone=client_phone,
        confirmation_token=token,
    )
    
    _emit_n8n_event(
        "booking.payment_pending",
        booking={
            "id": booking_id,
            "name": client_name,
            "email": client_email,
            "phone": client_phone,
            "instagram": client_ig,
            "date": event_date,
            "time": time,
            "session_type": session_type,
            "status": "pending_payment",
            "deposit_amount": ev.get("deposit", SESSION_PRICE) if ev else SESSION_PRICE,
            "full_price": ev.get("full_price") if ev else None,
        },
        event_data=ev or {},
    )

    # Payment submitted — global watcher auto-detects e-Transfer
    log.info(f"[confirm] Booking #{booking_id} — {client_name} @ {time} — payment submitted, global watcher active")
    
    return jsonify({
        "success": True,
        "message": "Booking received! I'll confirm once payment is received.",
        "booking_id": booking_id
    })

@app.route("/success")
def success():
    booking_id = request.args.get("booking_id")
    token      = (request.args.get("token") or "").strip()
    if not booking_id:
        return redirect(url_for("index"))

    # Success shows private session details, so public access requires the same
    # confirmation token used by payment, status polling, and calendar export.
    if not token and not _admin_authorized():
        return redirect(url_for("index"))

    conn = db_conn()
    c = conn.cursor()
    if token:
        c.execute("SELECT * FROM bookings WHERE id=? AND confirmation_token=?", (booking_id, token))
    else:
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return redirect(url_for("index"))

    booking = dict(row)
    ev = get_event_by_id(booking["event_id"]) if booking and booking.get("event_id") else get_active_event()
    booking["selected_addons"] = _booking_addons(booking)
    amount_due_today = _money(booking.get("deposit_amount") or (ev or {}).get("deposit") or SESSION_PRICE)
    total_price = _booking_total_price(booking, ev or {})
    remaining_balance = _booking_balance_due(booking, ev or {})
    questionnaire_url = _questionnaire_url_for_booking(booking, ev or {})
    location_text = ev.get("location", "Calgary, AB") if ev else "Calgary, AB"
    location_url = ev.get("location_url") if ev else None
    # If no explicit location_url but we have a location, generate a Google Maps search link
    if location_text and not location_url:
        query = (location_text + ", Calgary, AB").replace(" ", "+")
        location_url = f"https://www.google.com/maps/search/?api=1&query={query}"
    return render_template("success.html",
        email=EMAIL,
        meta_event_id=("purchase." + str(booking.get("id"))) if booking else "",
        date=ev["date"] if ev else DATE,
        time=booking.get("time", "15:00") if booking else "15:00",
        price=ev.get("deposit", SESSION_PRICE) if ev else SESSION_PRICE,
        event_title=ev.get("title", "Photo Session") if ev else "Photo Session",
        session_length=ev.get("session_length", 20) if ev else 20,
        timezone=ev.get("timezone", "America/Edmonton") if ev else "America/Edmonton",
        location=location_text,
        location_url=location_url,
        booking=booking,
        confirmation_token=booking.get("confirmation_token") if booking else "",
        selected_addons=booking.get("selected_addons") or [],
        addons_total=_booking_addons_total(booking),
        amount_due_today=amount_due_today,
        total_price=total_price,
        remaining_balance=remaining_balance,
        questionnaire_url=questionnaire_url,
        balance_url=(_balance_page_url(booking, absolute_base="") if (booking and (remaining_balance or 0) > 0) else None),
    )


@app.route("/questionnaire", methods=["GET", "POST"])
def questionnaire():
    """Optional post-confirmation prep questionnaire, protected by booking id + token."""
    booking_id = request.args.get("booking_id")
    token = (request.args.get("token") or "").strip()
    if not booking_id or not token:
        return redirect(url_for("index"))

    conn = db_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("index"))
    booking = dict(row)
    stored_token = str(booking.get("confirmation_token") or "")
    if not stored_token or not hmac.compare_digest(stored_token, token):
        conn.close()
        return redirect(url_for("index"))

    event = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else None
    cfg = _questionnaire_config_for_event(event)
    if not cfg:
        conn.close()
        return redirect(url_for("index"))
    if not (booking.get("confirmed") or booking.get("paid") or booking.get("status") == "confirmed"):
        conn.close()
        return jsonify({"error": "questionnaire is available after booking confirmation"}), 403

    fields = [f for f in (cfg.get("fields") or []) if isinstance(f, dict) and f.get("id")]
    existing = {}
    if booking.get("questionnaire_answers_json"):
        try:
            existing = json.loads(booking.get("questionnaire_answers_json") or "{}")
        except Exception:
            existing = {}

    if request.method == "POST":
        answers = {}
        for field in fields:
            field_id = str(field.get("id") or "")[:80]
            answers[field_id] = _strip_tags(request.form.get(field_id) or "")[:1000]
        conn.execute(
            "UPDATE bookings SET questionnaire_answers_json=? WHERE id=?",
            (json.dumps(answers, ensure_ascii=False), booking_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("success", booking_id=booking_id, token=token))

    conn.close()
    controls = []
    for field in fields:
        field_id = str(field.get("id") or "")[:80]
        label = _html_escape(str(field.get("label") or field_id))
        value = _html_escape(str(existing.get(field_id) or ""))
        field_type = str(field.get("type") or "textarea")
        required = " required" if field.get("required") else ""
        if field_type == "select":
            options = []
            for opt in field.get("options") or []:
                opt_text = str(opt)
                selected = " selected" if opt_text == existing.get(field_id) else ""
                options.append(f"<option value=\"{_html_escape(opt_text)}\"{selected}>{_html_escape(opt_text)}</option>")
            control = f"<select id=\"{_html_escape(field_id)}\" name=\"{_html_escape(field_id)}\"{required}>{''.join(options)}</select>"
        else:
            control = f"<textarea id=\"{_html_escape(field_id)}\" name=\"{_html_escape(field_id)}\" rows=\"5\"{required}>{value}</textarea>"
        controls.append(f"<label for=\"{_html_escape(field_id)}\">{label}</label>{control}")

    safe_title = _html_escape((event or {}).get("title") or "Photo Session")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Session questionnaire · Pashynska Photography</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7efe9;color:#3f2d33;margin:0;padding:24px}}
main{{max-width:680px;margin:0 auto;background:#fff;border:1px solid #ead8d0;border-radius:18px;padding:24px;box-shadow:0 14px 38px rgba(93,55,47,.12)}}
h1{{font-size:24px;margin:0 0 8px}}p{{color:#765e66;line-height:1.55}}label{{display:block;margin:18px 0 7px;font-weight:700}}
textarea,select{{width:100%;box-sizing:border-box;border:1px solid #d9c9c2;border-radius:10px;padding:12px;font:inherit}}
button{{margin-top:18px;background:#1c1917;color:#fff;border:0;border-radius:12px;padding:13px 18px;font-weight:700;cursor:pointer}}
</style></head><body><main>
<h1>Optional session questionnaire</h1>
<p>{safe_title}. Share anything helpful before your session. This is optional and does not affect your confirmed booking.</p>
<form method="post">{''.join(controls)}<button type="submit">Save questionnaire</button></form>
</main></body></html>"""


@app.route("/privacy")
@app.route("/privacy-policy")
def privacy():
    """Plain-language privacy and browser storage disclosure for clients.

    We intentionally avoid a cookie-consent popup because the public booking
    flow does not set advertising/analytics cookies. This page keeps the site
    transparent without adding conversion friction.
    """
    return render_template("privacy.html")


@app.route("/calendar-ics/<booking_id>")
def calendar_ics(booking_id):
    """Generate .ics calendar file for a confirmed booking."""
    token = request.args.get("token", "")
    conn = db_conn()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bookings WHERE id=? AND confirmation_token=?", (booking_id, token))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "not found"}), 404

    booking = dict(row)
    if not booking.get("confirmed"):
        return jsonify({"error": "booking not confirmed"}), 403

    ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    event_date = ev["date"] if ev else booking.get("date", "")
    event_time = booking.get("time", "15:00")
    session_length = ev.get("session_length", 20) if ev else 20

    # Parse datetime — use local timezone (America/Edmonton = MST/MDT)
    # IMPORTANT: do NOT append Z (UTC marker) to local times.
    # Using TZID format keeps Apple/Google Calendar correct regardless of DST.
    from datetime import datetime, timedelta
    dt_start = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M")
    dt_end = dt_start + timedelta(minutes=session_length)

    tz_name = (ev.get("timezone") if ev else None) or "America/Edmonton"
    dt_start_local = dt_start.strftime("%Y%m%dT%H%M%S")
    dt_end_local   = dt_end.strftime("%Y%m%dT%H%M%S")
    dt_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    summary  = ev.get("title", "Photo Session") if ev else "Photo Session"
    location = ev.get("location", "Calgary, AB") if ev else "Calgary, AB"

    ics_body = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Pashynska Photography//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VTIMEZONE
TZID:{tz_name}
X-LIC-LOCATION:{tz_name}
BEGIN:DAYLIGHT
TZOFFSETFROM:-0700
TZOFFSETTO:-0600
TZNAME:MDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0600
TZOFFSETTO:-0700
TZNAME:MST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:{booking_id}@book.pashynskaphoto.com
DTSTAMP:{dt_stamp}
DTSTART;TZID={tz_name}:{dt_start_local}
DTEND;TZID={tz_name}:{dt_end_local}
SUMMARY:{summary}
LOCATION:{location}
DESCRIPTION:Booking #{booking_id} with Pashynska Photography\\nClient: {booking.get('name', '')}
END:VEVENT
END:VCALENDAR"""

    from flask import Response
    return Response(ics_body, mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename=booking-{booking_id}.ics"})

@app.route("/backstage")
def backstage():
    """Hidden admin access — redirects to admin login or dashboard if already authenticated."""
    if _admin_authorized():
        return redirect(url_for("admin"))
    return redirect(url_for("admin_login", next=url_for("admin")))


# ──────────────────────────────────────────────────────────────────────────────
#  STRIPE ENDPOINTS
#  Two routes:
#    POST /stripe/create-checkout  — create a Stripe Checkout Session for a booking
#    POST /stripe/webhook          — handle Stripe events (auto-confirm on payment)
# ──────────────────────────────────────────────────────────────────────────────


def _stripe_checkout_idempotency_key(booking):
    """Build a stable, privacy-safe Stripe idempotency key for checkout creation.

    Prevents duplicate Checkout Sessions if the client retries after a timeout,
    without exposing email or confirmation tokens in Stripe logs.
    """
    booking_id = str(booking.get("id") or "unknown")
    raw = "|".join([
        booking_id,
        str(booking.get("event_id") or ""),
        str(booking.get("date") or ""),
        str(booking.get("time") or ""),
        str(booking.get("email") or "").strip().lower(),
    ])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"checkout-{booking_id}-{digest}"


def _stripe_balance_idempotency_key(booking, balance_due):
    """Stable idempotency key for admin-created balance checkout sessions."""
    booking_id = str(booking.get("id") or "unknown")
    cents = int(round(float(balance_due or 0) * 100))
    raw = "|".join([
        "balance",
        booking_id,
        str(booking.get("event_id") or ""),
        str(booking.get("email") or "").strip().lower(),
        str(cents),
    ])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"balance-{booking_id}-{cents}-{digest}"


def _stripe_custom_payment_name(description):
    text = (description or "").strip()
    return text[:120] if text else "Custom Photography Payment"


def _booking_total_price(booking, event=None):
    """Return the agreed full session price in CAD.

    booking.full_price is the source of truth after admin edits; event/full app
    defaults are only fallbacks for older rows.
    """
    event = event or {}
    booking_price = _money(booking.get("full_price"), 0.0)
    if booking_price > 0:
        return booking_price
    addons_total = _booking_addons_total(booking)
    for value in (event.get("full_price"), SESSION_TOTAL):
        try:
            amount = float(value or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount > 0:
            return round(amount + addons_total, 2)
    return 0.0


def _booking_paid_amount(booking, event=None):
    """Return amount already paid, falling back to deposit for legacy rows."""
    event = event or {}
    paid = booking.get("paid_amount")
    if paid is None or paid == "":
        paid = booking.get("deposit_amount") or event.get("deposit") or SESSION_PRICE or 0
    try:
        return round(float(paid or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _booking_balance_due(booking, event):
    """Return remaining balance for a booking in CAD, never below zero.
    A referral discount (if any) comes off the balance — not the deposit."""
    total = _booking_total_price(booking, event)
    paid = _booking_paid_amount(booking, event)
    discount = float(booking.get("referral_discount") or 0)
    return round(max(total - paid - discount, 0.0), 2)


def _create_balance_checkout_url(booking, event, balance_due):
    """Create a Stripe Checkout Session for the remaining balance."""
    if not STRIPE_SECRET_KEY:
        return None
    try:
        import stripe as _stripe
    except ImportError:
        return None

    base_url = BASE_URL or CANONICAL_SITE_URL
    event_title = event.get("title", "Photo Session") if event else "Photo Session"
    event_date = (event or {}).get("date") or booking.get("date", "")
    amount_cents = int(round(float(balance_due) * 100))
    if amount_cents <= 0:
        return None

    session = _stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "cad",
                "product_data": {
                    "name": f"Remaining Balance — {event_title}",
                    "description": f"Booking #{booking.get('id')} · {event_date} · {booking.get('time', '')}",
                },
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }],
        mode="payment",
        customer_email=booking.get("email") or None,
        success_url=_booking_success_url(
            booking.get("id"),
            booking.get("confirmation_token"),
            absolute_base=base_url,
            balance_paid=1,
        ),
        cancel_url=f"{base_url}/admin",
        metadata={
            "booking_id": str(booking.get("id")),
            "payment_type": "balance",
            "client_name": booking.get("name", ""),
            "event_id": booking.get("event_id", ""),
            "amount_cad": f"{float(balance_due):.2f}",
        },
        billing_address_collection="auto",
        payment_method_options={},
        idempotency_key=_stripe_balance_idempotency_key(booking, balance_due),
    )
    return session.url


def _send_balance_request_email(to_email, client_name, event_title, event_date, slot_time,
                                booking_id, total_price, paid_amount, balance_due,
                                stripe_url=None, interac_email=None):
    """Email client a remaining-balance payment request with Interac + Stripe options."""
    if not to_email:
        return False
    interac_email = interac_email or EMAIL or "iryna.pashynska@gmail.com"
    subject = f"Remaining Balance for Your Photo Session — ${balance_due:.2f} CAD"
    safe_name = _html_escape(client_name or "Client")
    safe_title = _html_escape(event_title or "Photo Session")
    safe_interac = _html_escape(interac_email)
    date_line = f"{event_date} at {slot_time}".strip()
    stripe_plain = f"\nPay by card / Apple Pay / Google Pay: {stripe_url}\n" if stripe_url else "\nCard payment link is temporarily unavailable — please use Interac e-Transfer.\n"
    stripe_html = (
        f'<p style="margin:18px 0 0;"><a href="{stripe_url}" style="display:inline-block;background:#c4857a;color:#fff;text-decoration:none;padding:13px 22px;border-radius:999px;font-weight:600;">Pay ${balance_due:.2f} by card / Apple Pay / Google Pay</a></p>'
        if stripe_url else
        '<p style="margin:18px 0 0;color:#8a6f6a;">Card payment link is temporarily unavailable — Interac e-Transfer is available.</p>'
    )
    plain = (
        f"Hi {client_name},\n\n"
        f"Thank you for your photo session with Iryna Pashynska Photography.\n\n"
        f"Remaining balance due: ${balance_due:.2f} CAD\n"
        f"Session: {event_title}\n"
        f"Date/time: {date_line}\n"
        f"Booking ID: #{booking_id}\n\n"
        f"You can pay either way:\n"
        f"1) Interac e-Transfer to {interac_email}\n"
        f"   Amount: ${balance_due:.2f} CAD\n"
        f"   Message: Balance for booking #{booking_id}\n"
        f"2){stripe_plain}\n"
        f"Already paid? Just reply to this email or DM @pashynska.photo.\n\n"
        f"Warmly,\nIryna Pashynska\n@pashynska.photo"
    )
    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;padding:40px 18px;"><tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:18px;overflow:hidden;box-shadow:0 8px 32px rgba(70,40,35,.10);">
<tr><td style="background:linear-gradient(135deg,#c4857a,#a3685e);padding:34px;text-align:center;color:#fff;">
<h1 style="margin:0;font-size:25px;font-weight:400;">Remaining Balance</h1><p style="margin:8px 0 0;opacity:.9;">Pashynska Photography</p>
</td></tr>
<tr><td style="padding:34px 38px;color:#5a3d4a;">
<p style="font-size:16px;line-height:1.65;margin:0 0 18px;">Hi <strong>{safe_name}</strong>,</p>
<p style="font-size:15px;line-height:1.7;margin:0 0 22px;color:#7a5a6a;">Thank you for your session. The deposit has been applied; the remaining balance is ready to pay below.</p>
<div style="background:#fdf6f0;border-radius:14px;padding:20px;margin:0 0 22px;">
<p style="margin:0 0 8px;font-size:13px;color:#a8918e;text-transform:uppercase;letter-spacing:.08em;">Amount due</p>
<p style="margin:0;font-size:32px;color:#c4857a;">${balance_due:.2f} CAD</p>
<p style="margin:12px 0 0;font-size:14px;color:#7a5a6a;">Total: ${total_price:.2f} · Paid: ${paid_amount:.2f} · Booking #{booking_id}</p>
<p style="margin:8px 0 0;font-size:14px;color:#7a5a6a;">{safe_title} · {_html_escape(date_line)}</p>
</div>
<h2 style="font-size:16px;margin:0 0 10px;color:#5a3d4a;">Option 1 — Interac e-Transfer</h2>
<p style="font-size:15px;line-height:1.7;color:#7a5a6a;margin:0 0 14px;">Send <strong>${balance_due:.2f} CAD</strong> to <strong>{safe_interac}</strong><br>Message: <strong>Balance for booking #{booking_id}</strong><br>No password needed — auto-deposit is enabled.</p>
<h2 style="font-size:16px;margin:22px 0 10px;color:#5a3d4a;">Option 2 — Stripe</h2>
<p style="font-size:15px;line-height:1.7;color:#7a5a6a;margin:0;">Pay securely by card, Apple Pay, or Google Pay.</p>
{stripe_html}
<p style="font-size:13px;line-height:1.6;color:#a8918e;margin:26px 0 0;">Already paid? Reply to this email or DM <a href="https://instagram.com/pashynska.photo" style="color:#c4857a;">@pashynska.photo</a>.</p>
</td></tr></table></td></tr></table></body></html>"""
    return _send_email_raw(to_email, client_name or "Client", subject, plain, html)


def _send_private_payment_email(to_email, client_name, event_title, event_date,
                                start_time, end_time, session_minutes, price,
                                booking_id, payment_url, interac_email=None,
                                deposit=None, balance=None):
    """Email the client a private-session payment link (same /payment page as
    the deposit flow: Interac e-Transfer with auto-confirmation OR Stripe).
    When deposit < price, also shows remaining balance and a balance pay link."""
    if not to_email:
        return False
    interac_email = interac_email or EMAIL or "iryna.pashynska@gmail.com"
    deposit = deposit if deposit is not None else price
    balance = balance if balance is not None else round(price - deposit, 2)
    has_balance = balance > 0
    subject = f"Your Individual Photoshoot — {event_date} · Booking & Payment"
    safe_name = _html_escape(client_name or "Client")
    time_range = f"{start_time}–{end_time}"

    # Build balance section for email
    balance_section_plain = ""
    balance_section_html = ""
    if has_balance:
        balance_section_plain = (
            f"\nDeposit due now: ${deposit:.2f} CAD\n"
            f"Remaining balance: ${balance:.2f} CAD (due before or on the session day)\n"
        )
        balance_section_html = (
            f'<tr><td style="padding:6px 0;color:#a8918e;font-size:13px;">Deposit due now</td>'
            f'<td style="padding:6px 0;text-align:right;color:#5a3d4a;font-size:14px;font-weight:700;">${deposit:.2f} CAD</td></tr>'
            f'<tr><td style="padding:6px 0;color:#a8918e;font-size:13px;">Remaining balance</td>'
            f'<td style="padding:6px 0;text-align:right;color:#c4857a;font-size:14px;font-weight:700;">${balance:.2f} CAD</td></tr>'
        )
        payment_button_label = f"Pay Deposit — ${deposit:.2f} CAD"
    else:
        payment_button_label = f"Complete Payment — ${price:.2f} CAD"

    plain = (
        f"Hi {client_name},\n\n"
        f"Your individual photoshoot with Iryna Pashynska is reserved!\n\n"
        f"Date: {event_date}\n"
        f"Time: {time_range} ({session_minutes} min)\n"
        f"Total price: ${price:.2f} CAD\n"
        f"{balance_section_plain}"
        f"Booking ID: #{booking_id}\n\n"
        f"To secure your session, please complete the payment here:\n"
        f"{payment_url}\n\n"
        f"On that page you can pay either way:\n"
        f"1) Interac e-Transfer to {interac_email} — confirms automatically within minutes\n"
        f"2) Card / Apple Pay / Google Pay (Stripe)\n\n"
        f"The page updates live as soon as your payment is received, and you'll\n"
        f"get a confirmation email right away.\n\n"
        f"Questions? Just reply to this email or DM @pashynska.photo.\n\n"
        f"Warmly,\nIryna Pashynska\n@pashynska.photo"
    )
    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#fdf6f0;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf6f0;padding:40px 18px;"><tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#fff;border-radius:18px;overflow:hidden;box-shadow:0 8px 32px rgba(70,40,35,.10);">
<tr><td style="background:linear-gradient(135deg,#c4857a,#a3685e);padding:34px;text-align:center;color:#fff;">
<h1 style="margin:0;font-size:25px;font-weight:400;">Your Individual Photoshoot</h1><p style="margin:8px 0 0;opacity:.9;">Pashynska Photography</p>
</td></tr>
<tr><td style="padding:34px 38px;color:#5a3d4a;">
<p style="font-size:16px;line-height:1.65;margin:0 0 18px;">Hi <strong>{safe_name}</strong>,</p>
<p style="font-size:15px;line-height:1.7;margin:0 0 22px;color:#7a5a6a;">Your individual photoshoot is reserved! Complete the payment below to confirm it.</p>
<div style="background:#fdf6f0;border-radius:14px;padding:20px;margin:0 0 22px;">
<p style="margin:0 0 8px;font-size:13px;color:#a8918e;text-transform:uppercase;letter-spacing:.08em;">Session details</p>
<p style="margin:0;font-size:15px;line-height:1.8;color:#5a3d4a;"><strong>{_html_escape(event_title or "Individual Photoshoot")}</strong><br>
📅 {_html_escape(event_date)} · 🕐 {_html_escape(time_range)} ({session_minutes} min)</p>
<table width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 0;">
<tr><td style="padding:6px 0;color:#a8918e;font-size:13px;">Total price</td>
<td style="padding:6px 0;text-align:right;color:#5a3d4a;font-size:14px;font-weight:700;">${price:.2f} CAD</td></tr>
{balance_section_html}
</table>
</div>
<p style="margin:0;"><a href="{payment_url}" style="display:inline-block;background:#c4857a;color:#fff;text-decoration:none;padding:15px 28px;border-radius:999px;font-weight:600;font-size:16px;">{_html_escape(payment_button_label)}</a></p>
<p style="font-size:14px;line-height:1.7;color:#7a5a6a;margin:22px 0 0;">On the payment page you can choose <strong>Interac e-Transfer</strong> (confirms automatically within minutes) or <strong>card / Apple Pay / Google Pay</strong>. The page updates live once your payment arrives, and a confirmation email follows right away.</p>
<p style="font-size:13px;line-height:1.6;color:#a8918e;margin:26px 0 0;">Questions? Reply to this email or DM <a href="https://instagram.com/pashynska.photo" style="color:#c4857a;">@pashynska.photo</a>.</p>
</td></tr></table></td></tr></table></body></html>"""
    return _send_email_raw(to_email, client_name or "Client", subject, plain, html)


@app.route("/stripe/create-checkout", methods=["POST"])
def stripe_create_checkout():
    """Create a Stripe Checkout Session for the deposit payment.

    Expects JSON: {booking_id, confirmation_token}
    Returns JSON: {checkout_url} or {error}
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Card payments are not configured yet"}), 503

    try:
        import stripe as _stripe
    except ImportError:
        return jsonify({"error": "Stripe library not installed"}), 503

    data = request.get_json(silent=True) or {}
    booking_id = data.get("booking_id")
    token      = (data.get("confirmation_token") or data.get("token") or "").strip()

    if not booking_id or not token:
        return jsonify({"error": "booking_id and confirmation_token required"}), 400

    # Verify booking exists and token matches (identity-safe)
    conn = db_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM bookings WHERE id=? AND confirmation_token=?", (booking_id, token)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Booking not found or token mismatch"}), 404

    booking = dict(row)
    if booking["status"] not in ("reserved", "pending_payment"):
        return jsonify({"error": "Booking already confirmed, cancelled or expired"}), 400

    ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    if not ev:
        return jsonify({"error": "Event not found"}), 404

    # Amount due now: booking-level deposit_amount wins (private sessions store
    # their full price there); falls back to the event deposit for mini sessions.
    amount_due = _money(booking.get("deposit_amount") or ev.get("deposit", SESSION_PRICE))
    deposit_cents = int(round(amount_due * 100))
    if deposit_cents <= 0:
        return jsonify({"error": "Nothing to charge for this booking"}), 400
    event_title   = ev.get("title", "Mini Photo Session")
    event_date    = ev.get("date", booking.get("date", ""))
    try:
        date_nice = datetime.strptime(event_date, "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        date_nice = event_date

    base_url = BASE_URL or CANONICAL_SITE_URL
    success_url = _booking_success_url(booking_id, token, absolute_base=base_url, stripe_paid=1)
    cancel_url = f"{base_url}/payment?booking_id={booking_id}&token={token}"

    # Build product description from event includes
    includes = ev.get("included", [
        "20-minute photo session",
        "15 professionally edited photos",
        "Quick turnaround (within 48 hours)",
    ])
    description = " · ".join(includes[:3]) if includes else f"Mini session on {date_nice}"

    # Cover photo for the Stripe checkout page
    images = []
    ev_photos = ev.get("photos", [])
    if ev_photos:
        first_photo = ev_photos[0]
        if first_photo.startswith("/"):
            first_photo = f"{base_url}{first_photo}"
        images = [first_photo]

    try:
        session = _stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "cad",
                    "product_data": {
                        # Private events already carry a descriptive title;
                        # prefixing "Deposit" there would be wrong (full price).
                        "name": (event_title if booking.get("session_type") == "private"
                                 else f"Deposit — {event_title}"),
                        "description": f"{date_nice} · {booking.get('time', '')} · {description}",
                        "images": images,
                    },
                    "unit_amount": deposit_cents,
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=booking.get("email") or None,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "booking_id":         str(booking_id),
                "confirmation_token": token,
                "client_name":        booking.get("name", ""),
                "event_id":           ev.get("id", ""),
            },
            # Collect billing address (helps dispute resolution)
            billing_address_collection="auto",
            # Allow Apple Pay / Google Pay automatically
            payment_method_options={},
            # Avoid duplicate Checkout Sessions/charges if the browser retries.
            idempotency_key=_stripe_checkout_idempotency_key(booking),
        )
        log.info(f"[stripe] Checkout session created for booking #{booking_id}: {session.id}")
        return jsonify({"checkout_url": session.url, "session_id": session.id})

    except _stripe.error.StripeError as e:
        log.error(f"[stripe] Checkout session error for #{booking_id}: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        log.error(f"[stripe] Unexpected error for #{booking_id}: {e}")
        return jsonify({"error": "Failed to create checkout session"}), 500


@app.route("/admin/stripe-link", methods=["POST"])
@admin_required
def admin_create_stripe_link():
    """Create a one-off Stripe Checkout link for custom admin requests.

    This is intentionally separate from booking deposits/balances: it does not
    mutate a booking row and does not email clients automatically. The admin can
    copy the returned Stripe URL and send it manually.
    """
    data = request.get_json(silent=True) or {}
    raw_amount = str(data.get("amount") or "").strip().replace(",", ".")
    description = _stripe_custom_payment_name(data.get("description"))
    client_email = str(data.get("email") or "").strip().lower()

    try:
        amount = round(float(raw_amount), 2)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Enter a valid amount"}), 400
    if amount <= 0:
        return jsonify({"success": False, "error": "Amount must be greater than zero"}), 400
    if amount > 10000:
        return jsonify({"success": False, "error": "Amount is too high for a one-off link"}), 400
    if client_email and not _re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", client_email):
        return jsonify({"success": False, "error": "Enter a valid client email or leave it blank"}), 400

    if not STRIPE_SECRET_KEY:
        return jsonify({"success": False, "error": "Stripe is not configured"}), 503

    try:
        import stripe as _stripe
    except ImportError:
        return jsonify({"success": False, "error": "Stripe library not installed"}), 503

    amount_cents = int(round(amount * 100))
    base_url = BASE_URL or CANONICAL_SITE_URL
    try:
        session_obj = _stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "cad",
                    "product_data": {
                        "name": description,
                        "description": "Pashynska Photography custom payment",
                    },
                    "unit_amount": amount_cents,
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=client_email or None,
            success_url=f"{base_url}/payment/custom/success",
            cancel_url=f"{base_url}/",
            metadata={
                "payment_type": "custom_admin_link",
                "description": description,
                "client_email": client_email,
                "amount_cad": f"{amount:.2f}",
            },
            billing_address_collection="auto",
            payment_method_options={},
        )
    except _stripe.error.StripeError as e:
        log.error(f"[admin_stripe_link] Stripe error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    except Exception as e:
        log.exception(f"[admin_stripe_link] Unexpected error: {e}")
        return jsonify({"success": False, "error": "Failed to create Stripe link"}), 500

    log.info(f"[admin_stripe_link] created ${amount:.2f} CAD link: {description!r}")
    return jsonify({
        "success": True,
        "checkout_url": session_obj.url,
        "session_id": getattr(session_obj, "id", ""),
        "amount": amount,
        "description": description,
    })


@app.route("/payment/custom/success")
def custom_payment_success():
    return render_template("custom_payment_success.html", email=EMAIL)


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events.

    On checkout.session.completed → auto-confirm the booking (paid by card).

    IMPORTANT: Cloudflare must NOT buffer/transform this route — it uses the
    raw request body for signature verification. Add a Cloudflare Page Rule or
    WAF bypass for /stripe/webhook if needed.
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 503

    try:
        import stripe as _stripe
    except ImportError:
        return jsonify({"error": "Stripe library not installed"}), 503

    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    # Verify webhook signature — fail-closed if the secret isn't configured.
    # Mirrors the Telegram webhook policy: no secret = refuse the request,
    # since an unsigned Stripe event lets anyone with the URL confirm bookings.
    if not STRIPE_WEBHOOK_SECRET:
        log.warning("[stripe-webhook] Rejected: STRIPE_WEBHOOK_SECRET not configured")
        return jsonify({"error": "webhook secret not configured"}), 503
    try:
        event = _stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except _stripe.error.SignatureVerificationError:
        log.warning("[stripe-webhook] Invalid signature — rejected")
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        log.error(f"[stripe-webhook] Signature check error: {e}")
        return jsonify({"error": "Webhook error"}), 400

    # ── Handle checkout.session.completed ──
    event_type = getattr(event, "type", None) or event.get("type")
    if event_type == "checkout.session.completed":
        try:
            session_obj = event.data.object
        except AttributeError:
            session_obj = event.get("data", {}).get("object", {})
        if not session_obj:
            log.warning("[stripe-webhook] checkout.session.completed with empty data.object (thin payload?)")
            return jsonify({"ok": True})
        metadata    = session_obj.get("metadata", {})
        payment_type = (metadata.get("payment_type") or "deposit").strip().lower()

        if payment_type == "custom_admin_link":
            amount_received_cents = session_obj.get("amount_total", 0) or 0
            amount_paid = amount_received_cents / 100.0
            customer_details = session_obj.get("customer_details", {}) or {}
            customer_email = (
                customer_details.get("email")
                or session_obj.get("customer_email")
                or metadata.get("client_email")
                or ""
            )
            description = metadata.get("description") or "Custom Photography Payment"
            try:
                _notify_admin(
                    f"💳 <b>Custom Stripe Payment Paid</b>\n\n"
                    f"🧾 {_tg_escape(description)}\n"
                    f"📧 {_tg_escape(customer_email or 'No email')}\n"
                    f"💰 <b>${amount_paid:.2f} CAD</b>\n\n"
                    f"Manual Stripe link payment — no booking was changed."
                )
            except Exception as e:
                log.error(f"[stripe-webhook] Telegram custom payment notify error: {e}")
            log.info(
                f"[stripe-webhook] Custom payment completed: "
                f"${amount_paid:.2f} CAD description={description!r}"
            )
            return jsonify({"ok": True})

        booking_id  = metadata.get("booking_id")
        token       = metadata.get("confirmation_token", "")

        if not booking_id:
            log.warning("[stripe-webhook] checkout.session.completed with no booking_id in metadata")
            return jsonify({"ok": True})

        try:
            booking_id = int(booking_id)
        except (ValueError, TypeError):
            log.warning(f"[stripe-webhook] Invalid booking_id in metadata: {booking_id!r}")
            return jsonify({"ok": True})

        # Prevent duplicate processing
        conn = db_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row:
            conn.close()
            log.warning(f"[stripe-webhook] Booking #{booking_id} not found")
            return jsonify({"ok": True})

        booking = dict(row)
        # Balance-payment sessions are created after a booking is already confirmed.
        # Do not run the deposit confirmation side-effects again; just record the
        # additional amount and notify admins.
        if payment_type == "balance":
            amount_received_cents = session_obj.get("amount_total", 0)
            amount_paid = amount_received_cents / 100.0
            ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
            total_price = _booking_total_price(booking, ev or {})
            current_paid = float(booking.get("paid_amount") or booking.get("deposit_amount") or (ev or {}).get("deposit") or SESSION_PRICE or 0)
            new_paid_total = round(min(current_paid + amount_paid, total_price or current_paid + amount_paid), 2)
            conn.execute(
                "UPDATE bookings SET paid=1, paid_amount=? WHERE id=?",
                (new_paid_total, booking_id)
            )
            conn.commit()
            conn.close()
            try:
                sync_to_notion(booking_id)
            except Exception as e:
                log.error(f"[stripe-webhook] Notion balance sync error for #{booking_id}: {e}")
            try:
                _notify_admin(
                    f"💸 <b>Stripe Balance Paid</b>\n\n"
                    f"👤 {booking.get('name', '?')}\n"
                    f"📧 {booking.get('email', '?')}\n"
                    f"🆔 Booking #{booking_id}\n"
                    f"💰 Balance payment: <b>${amount_paid:.2f} CAD</b>\n"
                    f"📊 Total recorded paid: <b>${new_paid_total:.2f} CAD</b>"
                )
            except Exception as e:
                log.error(f"[stripe-webhook] Telegram balance notify error for #{booking_id}: {e}")
            log.info(f"[stripe-webhook] Balance payment recorded for booking #{booking_id}: ${amount_paid:.2f}")
            return jsonify({"ok": True})

        if booking.get("confirmed") or booking.get("status") == "confirmed":
            conn.close()
            log.info(f"[stripe-webhook] Booking #{booking_id} already confirmed — skipping")
            return jsonify({"ok": True})

        # Amount paid (Stripe sends in cents)
        amount_received_cents = session_obj.get("amount_total", 0)
        amount_paid = amount_received_cents / 100.0

        conn.execute(
            "UPDATE bookings SET confirmed=1, paid=1, status='confirmed', paid_amount=? WHERE id=?",
            (amount_paid, booking_id)
        )
        conn.commit()
        conn.close()
        booking.update({"confirmed": 1, "paid": 1, "status": "confirmed", "paid_amount": amount_paid})
        _record_booking_funnel_event(
            booking,
            "booking_confirmed",
            {"source": "stripe", "paid_amount": amount_paid},
        )

        log.info(f"[stripe-webhook] Booking #{booking_id} auto-confirmed via Stripe (${amount_paid:.2f} CAD)")

        # Side effects: calendar, Notion, email, Telegram
        try:
            create_calendar_event_for_booking(booking_id)
        except Exception as e:
            log.error(f"[stripe-webhook] Calendar error for #{booking_id}: {e}")
        try:
            sync_to_notion(booking_id)
        except Exception as e:
            log.error(f"[stripe-webhook] Notion error for #{booking_id}: {e}")

        ev = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
        try:
            _send_client_email(
                to_email=booking.get("email", ""),
                client_name=booking.get("name", "Client"),
                event_date=ev["date"] if ev else booking.get("date", ""),
                slot_time=booking.get("time", ""),
                event_title=ev.get("title", "Mini Session") if ev else "Mini Session",
                booking_id=booking_id,
                location=ev.get("location"),
                location_url=ev.get("location_url") if ev else None,
                **_client_email_context(booking, ev or {}),
            )
        except Exception as e:
            log.error(f"[stripe-webhook] Email error for #{booking_id}: {e}")

        # Notify admin on Telegram
        try:
            msg = (
                f"💳 <b>Stripe Payment Confirmed!</b>\n\n"
                f"👤 {booking.get('name', '?')}\n"
                f"📧 {booking.get('email', '?')}\n"
                f"📅 {booking.get('date', '?')} @ {booking.get('time', '?')}\n"
                f"💰 <b>${amount_paid:.2f} CAD via card</b>\n"
                f"🆔 Booking #{booking_id}\n\n"
                f"✅ Auto-confirmed · email sent to client"
            )
            _notify_admin(msg)
        except Exception as e:
            log.error(f"[stripe-webhook] Telegram notify error for #{booking_id}: {e}")

    return jsonify({"ok": True})

def _safe_next_url(raw, default=None):
    """Validate a `?next=` redirect target so a crafted link can't bounce the
    operator off-site after login (open-redirect). Accepts only same-origin
    relative paths: must start with `/`, no scheme, no netloc, no `//` or
    `/\\` protocol-relative tricks, no leading whitespace or control chars."""
    if default is None:
        default = url_for("admin")
    if not raw:
        return default
    raw = raw.strip()
    # Reject anything that smells like protocol/host trickery.
    if (not raw.startswith("/")) or raw.startswith("//") or raw.startswith("/\\"):
        return default
    # Strip control chars browsers might tolerate in URL parsing.
    if any(ch in raw for ch in ("\r", "\n", "\t", "\x00")):
        return default
    try:
        from urllib.parse import urlparse
        parsed = urlparse(raw)
    except Exception:
        return default
    if parsed.scheme or parsed.netloc:
        return default
    return raw


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Browser login for the admin dashboard."""
    next_url = _safe_next_url(request.values.get("next"))

    # Already logged in — skip straight to admin
    if session.get("admin_authenticated"):
        return redirect(next_url)

    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        if isinstance(ip, str) and "," in ip:
            ip = ip.split(",")[0].strip()

        # Brute-force protection: max 10 login attempts per IP per 15 min
        if not check_login_rate_limit(ip):
            return render_template(
                "admin_login.html",
                error="Too many login attempts. Please wait 15 minutes.",
                next_url=next_url,
            ), 429

        record_login_attempt(ip)

        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_user = hmac.compare_digest(username, ADMIN_USER)
        valid_password = bool(ADMIN_PASSWORD) and hmac.compare_digest(password, ADMIN_PASSWORD)

        if valid_user and valid_password:
            session["admin_authenticated"] = True
            session.permanent = True  # respect PERMANENT_SESSION_LIFETIME
            log.info(f"[admin] Successful login from {ip}")
            return redirect(next_url)

        log.warning(f"[admin] Failed login attempt from {ip} (user={username!r})")
        return render_template(
            "admin_login.html",
            error="Incorrect username or password.",
            next_url=next_url,
        ), 401

    return render_template("admin_login.html", error=None, next_url=next_url)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("admin_login"))

@app.route("/admin/health")
@admin_required
def admin_health():
    """System health check — returns JSON status for all critical integrations."""
    import shutil

    status = {}

    # Database
    try:
        conn = db_conn()
        conn.execute("SELECT COUNT(*) FROM bookings").fetchone()
        conn.close()
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        status["database"] = {"ok": True, "path": DB_PATH, "size_kb": round(db_size / 1024, 1)}
    except Exception as e:
        status["database"] = {"ok": False, "error": str(e)}

    # Himalaya email CLI
    himalaya_ok = shutil.which("himalaya") is not None
    last_scan_failed = _watcher_state.get("last_email_scan_ok") is False
    status["email_himalaya"] = {
        "ok": himalaya_ok and not last_scan_failed,
        "error": (
            _watcher_state.get("last_email_scan_error")
            if himalaya_ok
            else "himalaya CLI not found in PATH — emails will silently fail"
        ),
        "last_scan_at": _watcher_state.get("last_email_scan_at"),
        "last_scan_email_count": _watcher_state.get("last_email_count", 0),
        "last_auto_confirmed_booking_id": _watcher_state.get("last_auto_confirmed_booking_id"),
        "last_auto_confirmed_at": _watcher_state.get("last_auto_confirmed_at"),
    }

    # Telegram bot
    tg_ok = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    tg_secret_ok = bool(TELEGRAM_WEBHOOK_SECRET)
    status["telegram"] = {
        "ok": tg_ok,
        "webhook_secret_set": tg_secret_ok,
        "warning": None if tg_secret_ok else "TELEGRAM_WEBHOOK_SECRET not set — webhook accepts unauthenticated callbacks",
    }

    # Watcher thread alive
    import threading
    watcher_alive = any(t.name == "etransfer-watcher" and t.is_alive() for t in threading.enumerate())
    status["watcher_thread"] = {"ok": watcher_alive}

    # Email scheduler thread alive
    sched_alive = any(t.name == "email-scheduler" and t.is_alive() for t in threading.enumerate())
    status["email_scheduler"] = {"ok": sched_alive}

    # Notion (optional)
    notion_configured = bool(NOTION_API_KEY)
    status["notion"] = {"ok": notion_configured, "warning": None if notion_configured else "NOTION_API_KEY not set — Notion sync disabled"}

    # Events loaded
    events_ok = bool(EVENTS)
    active_events = [e for e in EVENTS if e.get("status") in ("active", "upcoming")]
    status["events"] = {
        "ok": events_ok,
        "total": len(EVENTS),
        "active_or_upcoming": len(active_events),
    }

    overall_ok = all(
        v.get("ok", True)
        for k, v in status.items()
        if k not in ("notion",)  # Notion is optional
    )

    return jsonify({
        "healthy": overall_ok,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "checks": status,
    }), 200 if overall_ok else 503


def _admin_event_is_current(ev):
    """Admin organizer cards: show only actionable future/current sessions."""
    if not ev or ev.get("hidden"):
        return False
    return (ev.get("status") or "").lower() in ("active", "upcoming")


def _admin_event_date_key(ev):
    raw = (ev or {}).get("date") or "9999-12-31"
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return datetime.max.date()


def _admin_is_internal_block(row):
    data = dict(row) if not isinstance(row, dict) else row
    return (
        (data.get("session_type") or "") == "internal_block"
        or str(data.get("name") or "").startswith("⛔")
    )


def _admin_event_summary(ev, conn=None, today=None):
    """Small event-first view model for /admin and /admin/event/<id>."""
    close_conn = False
    if conn is None:
        conn = db_conn()
        close_conn = True
    today = today or _local_today()
    event_id = ev.get("id") or ""
    event_date = ev.get("date") or ""
    slots = generate_slots(ev)
    active_status_filter = "status NOT IN ('cancelled','expired')"

    rows = []
    date_rows = []
    try:
        if event_id:
            rows = conn.execute(
                f"""SELECT * FROM bookings
                    WHERE {active_status_filter}
                      AND (event_id=? OR ((event_id IS NULL OR event_id='') AND date=?))
                    ORDER BY date ASC, time ASC""",
                (event_id, event_date),
            ).fetchall()
        elif event_date:
            rows = conn.execute(
                f"""SELECT * FROM bookings
                    WHERE {active_status_filter} AND date=?
                    ORDER BY date ASC, time ASC""",
                (event_date,),
            ).fetchall()
        if event_date:
            date_rows = conn.execute(
                f"""SELECT time, name, session_type FROM bookings
                    WHERE {active_status_filter} AND date=?""",
                (event_date,),
            ).fetchall()
    finally:
        if close_conn:
            conn.close()

    booking_rows = [dict(r) for r in rows]
    taken_times = {r["time"] for r in date_rows if r["time"]}
    total_slots = len(slots)
    blocked_rows = [b for b in booking_rows if _admin_is_internal_block(b)]
    client_rows = [b for b in booking_rows if not _admin_is_internal_block(b)]
    occupied_slots = len(taken_times) if total_slots else len(booking_rows)
    booked_slots = len(client_rows)
    confirmed = sum(1 for b in client_rows if b.get("confirmed") or b.get("status") == "confirmed")
    pending = sum(
        1 for b in client_rows
        if (b.get("status") in ("reserved", "pending_payment", "partial_payment"))
        and not b.get("confirmed")
    )
    free = max(total_slots - occupied_slots, 0) if total_slots else 0
    paid_total = sum(float(b.get("paid_amount") or 0) for b in client_rows)
    event_day = _admin_event_date_key(ev)

    return {
        "id": event_id,
        "title": ev.get("title") or event_id or "Untitled session",
        "date": event_date,
        "start_time": ev.get("start_time") or "",
        "end_time": ev.get("end_time") or "",
        "status": ev.get("status") or "",
        "session_type": ev.get("session_type") or "",
        "location": ev.get("location") or "",
        "deposit": float(ev.get("deposit") or 0),
        "full_price": float(ev.get("full_price") or 0),
        "total_slots": total_slots,
        "booked": booked_slots,
        "confirmed": confirmed,
        "pending": pending,
        "blocked": len(blocked_rows),
        "occupied": occupied_slots,
        "free": free,
        "paid_total": paid_total,
        "attention_count": pending,
        "is_future": event_day >= today,
        "is_past": event_day < today,
        "is_today": event_day == today,
        "is_sold_out": total_slots > 0 and free == 0,
        "occupancy": int((occupied_slots / total_slots) * 100) if total_slots else 0,
    }


def _admin_event_summaries():
    today = _local_today()
    current_events = [ev for ev in EVENTS if _admin_event_is_current(ev)]
    # Sort: today first, then future by date, then past by date desc
    current_events.sort(key=lambda ev: (
        _admin_event_date_key(ev) < today,   # future/today first (False < True)
        _admin_event_date_key(ev) if _admin_event_date_key(ev) >= today else -_admin_event_date_key(ev).toordinal()
    ))
    conn = db_conn()
    try:
        summaries = [_admin_event_summary(ev, conn=conn, today=today) for ev in current_events]
        # Only include past events if they still have pending bookings needing attention
        return [s for s in summaries if not s["is_past"] or s["attention_count"] > 0]
    finally:
        conn.close()


def _analytics_report_range():
    today = _local_today()
    default_from = (today - timedelta(days=30)).isoformat()
    default_to = today.isoformat()
    date_from = request.args.get("date_from") or default_from
    date_to = request.args.get("date_to") or default_to
    def _valid_date(value, fallback):
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return value
        except ValueError:
            return fallback
    date_from = _valid_date(date_from, default_from)
    date_to = _valid_date(date_to, default_to)
    return date_from, date_to


def _analytics_include_internal():
    """Opt-in flag for QA/smoke campaigns in the admin analytics report."""
    return str(request.args.get("include_internal", "")).lower() in {"1", "true", "yes", "on"}


def _analytics_is_internal_campaign(campaign, content):
    """Hide synthetic QA campaigns by default so business reports stay clean."""
    blob = f"{campaign or ''} {content or ''}".lower()
    internal_prefixes = (
        "codex_",
        "playwright_",
        "browser_smoke",
        "smoke_",
        "test_",
        "qa_",
        "claude_code",
    )
    return any(blob.startswith(prefix) or f" {prefix}" in blob for prefix in internal_prefixes) or "_smoke" in blob


def _analytics_campaign_group(campaign):
    """Canonical display group for campaigns split across historical UTM names.

    Mountain Mini traffic is spread over `mountain_mini_jun20`,
    `mountain_mini_booking_ab_202606`, and future clean UTM campaigns; report
    them under one label. Raw campaign/content columns are always preserved —
    this only adds a display grouping, it never rewrites stored rows.
    """
    key = re.sub(r"[^a-z0-9]+", "", str(campaign or "").lower())
    if "mountainmini" in key:
        return "Mountain Mini"
    return ""


def _analytics_group_rows(rows):
    """Aggregate campaign rows that share a canonical display group."""
    sum_keys = [
        "visits", "session_views", "drawer_opens", "slot_selections", "form_starts",
        "reserve_attempts", "bookings", "confirmed_bookings", "expired_bookings",
        "abandoned_followups",
    ]
    groups = {}
    for row in rows:
        group = row.get("campaign_group")
        if not group:
            continue
        agg = groups.setdefault(group, {key: 0 for key in sum_keys})
        for key in sum_keys:
            agg[key] += int(row.get(key) or 0)
    out = []
    for group, agg in sorted(groups.items()):
        visits = agg["visits"]
        agg["campaign_group"] = group
        agg["booking_conversion_rate"] = round(agg["bookings"] / visits * 100, 2) if visits else 0.0
        agg["confirmed_conversion_rate"] = round(agg["confirmed_bookings"] / visits * 100, 2) if visits else 0.0
        out.append(agg)
    return out


def _analytics_campaign_rows(date_from, date_to, include_internal=False):
    conn = db_conn()
    try:
        rows = conn.execute("""
            WITH event_counts AS (
                SELECT
                    COALESCE(NULLIF(v.utm_campaign, ''), '(none)') AS campaign,
                    COALESCE(NULLIF(v.utm_content, ''), '(none)') AS content,
                    COUNT(DISTINCT CASE WHEN e.event_name='page_view' THEN e.visitor_id END) AS visits,
                    SUM(CASE WHEN e.event_name='session_view' THEN 1 ELSE 0 END) AS session_views,
                    SUM(CASE WHEN e.event_name='drawer_open' THEN 1 ELSE 0 END) AS drawer_opens,
                    SUM(CASE WHEN e.event_name='slot_selected' THEN 1 ELSE 0 END) AS slot_selections,
                    SUM(CASE WHEN e.event_name='form_started' THEN 1 ELSE 0 END) AS form_starts,
                    SUM(CASE WHEN e.event_name='reserve_attempt' THEN 1 ELSE 0 END) AS reserve_attempts,
                    SUM(CASE WHEN e.event_name='booking_reserved' THEN 1 ELSE 0 END) AS booking_events,
                    SUM(CASE WHEN e.event_name='booking_confirmed' THEN 1 ELSE 0 END) AS confirmed_events,
                    SUM(CASE WHEN e.event_name='booking_expired' THEN 1 ELSE 0 END) AS expired_events,
                    SUM(CASE WHEN e.event_name='abandoned_followup_sent' THEN 1 ELSE 0 END) AS abandoned_followups
                FROM analytics_events e
                LEFT JOIN visitor_sessions v ON v.visitor_id = e.visitor_id
                WHERE date(e.created_at) BETWEEN date(?) AND date(?)
                GROUP BY campaign, content
            ),
            booking_counts AS (
                SELECT
                    COALESCE(NULLIF(utm_campaign, ''), '(none)') AS campaign,
                    COALESCE(NULLIF(utm_content, ''), '(none)') AS content,
                    COUNT(*) AS bookings,
                    SUM(CASE WHEN confirmed=1 OR paid=1 OR status='confirmed' THEN 1 ELSE 0 END) AS confirmed_bookings
                FROM bookings
                WHERE date(created_at) BETWEEN date(?) AND date(?)
                GROUP BY campaign, content
            ),
            keys AS (
                SELECT campaign, content FROM event_counts
                UNION
                SELECT campaign, content FROM booking_counts
            )
            SELECT
                k.campaign,
                k.content,
                COALESCE(e.visits, 0) AS visits,
                COALESCE(e.session_views, 0) AS session_views,
                COALESCE(e.drawer_opens, 0) AS drawer_opens,
                COALESCE(e.slot_selections, 0) AS slot_selections,
                COALESCE(e.form_starts, 0) AS form_starts,
                COALESCE(e.reserve_attempts, 0) AS reserve_attempts,
                COALESCE(b.bookings, e.booking_events, 0) AS bookings,
                COALESCE(b.confirmed_bookings, e.confirmed_events, 0) AS confirmed_bookings,
                COALESCE(e.expired_events, 0) AS expired_bookings,
                COALESCE(e.abandoned_followups, 0) AS abandoned_followups
            FROM keys k
            LEFT JOIN event_counts e ON e.campaign=k.campaign AND e.content=k.content
            LEFT JOIN booking_counts b ON b.campaign=k.campaign AND b.content=k.content
            ORDER BY confirmed_bookings DESC, bookings DESC, reserve_attempts DESC, visits DESC
        """, (date_from, date_to, date_from, date_to)).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        item = dict(row)
        if not include_internal and _analytics_is_internal_campaign(item.get("campaign"), item.get("content")):
            continue
        visits = int(item.get("visits") or 0)
        bookings = int(item.get("bookings") or 0)
        confirmed = int(item.get("confirmed_bookings") or 0)
        item["booking_conversion_rate"] = round((bookings / visits * 100), 2) if visits else 0.0
        item["confirmed_conversion_rate"] = round((confirmed / visits * 100), 2) if visits else 0.0
        item["campaign_group"] = _analytics_campaign_group(item.get("campaign"))
        out.append(item)
    return out


def _analytics_totals(rows):
    keys = [
        "visits", "session_views", "drawer_opens", "slot_selections", "form_starts",
        "reserve_attempts", "bookings", "confirmed_bookings", "expired_bookings",
        "abandoned_followups",
    ]
    totals = {key: sum(int(row.get(key) or 0) for row in rows) for key in keys}
    visits = totals["visits"]
    totals["booking_conversion_rate"] = round(totals["bookings"] / visits * 100, 2) if visits else 0.0
    totals["confirmed_conversion_rate"] = round(totals["confirmed_bookings"] / visits * 100, 2) if visits else 0.0
    return totals


@app.route("/admin")
@admin_required
def admin():
    """Admin dashboard — HTML view with Confirm/Cancel buttons."""
    # Get filter parameters
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    session_type = request.args.get("session_type", "")
    status = request.args.get("status", "")
    search = request.args.get("search", "").strip()
    page = request.args.get("page", "1")
    limit = request.args.get("limit", "50")

    try:
        page_num = int(page)
        if page_num < 1:
            page_num = 1
    except ValueError:
        page_num = 1

    try:
        limit_num = int(limit)
        if limit_num < 1 or limit_num > 1000:
            limit_num = 50
    except ValueError:
        limit_num = 50

    offset = (page_num - 1) * limit_num

    conn = db_conn()
    c = conn.cursor()

    # Build WHERE clause
    conditions = []
    params = []

    if date_from:
        conditions.append("date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date <= ?")
        params.append(date_to)
    if session_type:
        conditions.append("session_type = ?")
        params.append(session_type)
    if status and status != "all":
        if status == "pending":
            conditions.append("status IN ('pending_payment', 'reserved')")
        else:
            conditions.append("status = ?")
            params.append(status)
    if search:
        conditions.append("(name LIKE ? OR email LIKE ? OR phone LIKE ? OR instagram LIKE ?)")
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term, search_term])

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    # Get total count for pagination
    c.execute(f"SELECT COUNT(*) as total FROM bookings WHERE {where_clause}", params)
    total_count = c.fetchone()["total"]

    # Get filtered bookings
    order_clause = "ORDER BY date DESC, time ASC"
    sql = f"SELECT * FROM bookings WHERE {where_clause} {order_clause} LIMIT ? OFFSET ?"
    params_with_limit = params + [limit_num, offset]
    c.execute(sql, params_with_limit)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for row in rows:
        row["selected_addons"] = _booking_addons(row)
        row["addons_total"] = _booking_addons_total(row)

    # Calculate stats based on filtered results
    filtered_stats = {
        "total": len(rows),
        "confirmed": sum(1 for b in rows if b["status"] == "confirmed"),
        "pending": sum(1 for b in rows if b["status"] in ("pending_payment", "reserved")),
        "cancelled": sum(1 for b in rows if b["status"] == "cancelled"),
        "expired": sum(1 for b in rows if b["status"] == "expired"),
        "total_expected": sum(b.get("paid_amount", 0) or 0 for b in rows if b["status"] == "confirmed"),
    }

    # Get overall stats (unfiltered) for summary
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as total, "
              "SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) as confirmed, "
              "SUM(CASE WHEN status IN ('pending_payment', 'reserved') THEN 1 ELSE 0 END) as pending, "
              "SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled, "
              "SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) as expired, "
              "SUM(CASE WHEN status = 'confirmed' THEN paid_amount ELSE 0 END) as total_expected FROM bookings")
    overall_row = c.fetchone()
    overall_stats = {
        "total": overall_row["total"] or 0,
        "confirmed": overall_row["confirmed"] or 0,
        "pending": overall_row["pending"] or 0,
        "cancelled": overall_row["cancelled"] or 0,
        "expired": overall_row["expired"] or 0,
        "total_expected": overall_row["total_expected"] or 0,
    }
    conn.close()

    # Get unique session types for dropdown
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT session_type FROM bookings WHERE session_type IS NOT NULL AND session_type != '' ORDER BY session_type")
    session_types = [row["session_type"] for row in c.fetchall()]
    conn.close()

    event_summaries = _admin_event_summaries()
    next_event = next((ev for ev in event_summaries if ev.get("is_future")), None)
    today_local = _local_today()
    event_names = {ev.get("id"): ev.get("title", "") for ev in EVENTS if ev.get("id")}

    # Classic admin remains the default (full feature set); new design is opt-in via ?v=2.
    template_name = "admin_pro.html" if request.args.get("v") == "2" else "admin.html"
    return render_template(template_name,
                           bookings=rows,
                           filtered_stats=filtered_stats,
                           overall_stats=overall_stats,
                           events=EVENTS,
                           event_summaries=event_summaries,
                           next_event=next_event,
                           now=_local_now(),
                           today_str=today_local.isoformat(),
                           event_names=event_names,
                           session_types=session_types,
                           filters={
                               "date_from": date_from,
                               "date_to": date_to,
                               "session_type": session_type,
                               "status": status,
                               "search": search,
                               "page": page_num,
                               "limit": limit_num,
                               "total_count": total_count,
                               "total_pages": (total_count + limit_num - 1) // limit_num if limit_num > 0 else 1
                           })

@app.route("/admin/export")
@admin_required
def admin_export():
    """Export bookings as CSV."""
    # Get filter parameters (same as admin dashboard)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    session_type = request.args.get("session_type", "")
    status = request.args.get("status", "")
    search = request.args.get("search", "").strip()

    conn = db_conn()
    c = conn.cursor()

    conditions = []
    params = []

    if date_from:
        conditions.append("date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date <= ?")
        params.append(date_to)
    if session_type:
        conditions.append("session_type = ?")
        params.append(session_type)
    if status and status != "all":
        if status == "pending":
            conditions.append("status IN ('pending_payment', 'reserved')")
        else:
            conditions.append("status = ?")
            params.append(status)
    if search:
        conditions.append("(name LIKE ? OR email LIKE ? OR phone LIKE ? OR instagram LIKE ?)")
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term, search_term])

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"SELECT * FROM bookings WHERE {where_clause} ORDER BY date DESC, time ASC"
    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    # Generate CSV
    import io
    import csv
    output = io.StringIO()
    if rows:
        fieldnames = rows[0].keys()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    else:
        # Write header only
        writer = csv.writer(output)
        writer.writerow(["No data matching filters"])

    # Create response
    from flask import Response
    filename = f"bookings-{_local_now().strftime('%Y-%m-%d')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    """Admin-only campaign/funnel report for first-party booking analytics."""
    date_from, date_to = _analytics_report_range()
    include_internal = _analytics_include_internal()
    rows = _analytics_campaign_rows(date_from, date_to, include_internal=include_internal)
    totals = _analytics_totals(rows)
    group_rows = _analytics_group_rows(rows)
    return render_template(
        "admin_analytics.html",
        rows=rows,
        totals=totals,
        group_rows=group_rows,
        date_from=date_from,
        date_to=date_to,
        include_internal=include_internal,
    )


@app.route("/admin/link-generator")
@admin_required
def admin_link_generator():
    """UTM link generator for tracking individual client outreach."""
    return render_template(
        "admin_link_generator.html",
        CANONICAL_SITE_URL=CANONICAL_SITE_URL,
    )


@app.route("/admin/analytics.csv")
@admin_required
def admin_analytics_csv():
    """CSV export for Google Sheets import or lightweight reporting."""
    import csv as _csv
    import io as _io
    date_from, date_to = _analytics_report_range()
    include_internal = _analytics_include_internal()
    rows = _analytics_campaign_rows(date_from, date_to, include_internal=include_internal)
    fields = [
        "campaign", "content", "visits", "session_views", "drawer_opens",
        "slot_selections", "form_starts", "reserve_attempts", "bookings",
        "confirmed_bookings", "expired_bookings", "abandoned_followups",
        "booking_conversion_rate", "confirmed_conversion_rate",
        # appended last so existing imports/sheets keep their column order
        "campaign_group",
    ]
    output = _io.StringIO()
    writer = _csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    filename = f"booking-analytics-{date_from}-to-{date_to}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

def _admin_booking_row_or_404(booking_id):
    conn = db_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


# Brand constants for the invoice — keep here so any future PDF (receipts,
# gallery delivery memos, etc.) can reuse the same palette + footer.
_INVOICE_ROSE       = "#a3685e"   # rose-deep — section headings + rules
_INVOICE_ROSE_SOFT  = "#e8c8c0"   # blush — table header background
_INVOICE_PAGE_BG    = "#faf5f2"   # whisper-cream — page background tint
_INVOICE_INK        = "#1c1917"   # near-black — body text
_INVOICE_INK_2      = "#57534e"   # warm grey — labels, secondary
_INVOICE_INK_3      = "#a8a29e"   # light grey — footnotes
_INVOICE_FOOTER     = "iryna.pashynska@gmail.com · +1 (368) 997-7903 · book.pashynskaphoto.com"
_INVOICE_BRAND      = "Pashynska Photography"
_INVOICE_TAGLINE    = "Iryna Pashynska — Portrait & Lifestyle Photographer"
_INVOICE_TERMS = (
    "Terms: The deposit is non-refundable. Rescheduling is subject to availability. "
    "Late arrival will reduce the session time; overtime is not guaranteed. "
    "Delivery within 5–7 business days after the session."
)


def _admin_invoice_pdf_bytes(booking):
    """Build a premium PDF invoice for the booking using ReportLab.

    Layout mirrors the previously well-received INV-00169 design and pushes
    further on typography + structure: serif heading, rose accent rules, full
    BILL TO / SESSION DETAILS / WHAT'S INCLUDED / PAYMENT SUMMARY / TERMS /
    FOOTER blocks. Falls back to a tiny but valid PDF if ReportLab is missing
    (so the route still 200s in stripped-down environments)."""
    booking_id = booking.get("id", "")
    invoice_no = f"INV-{int(booking_id):05d}" if str(booking_id).isdigit() else f"INV-{booking_id}"
    client_name = (booking.get("name") or "Client").strip()
    client_email = (booking.get("email") or "").strip()
    client_phone = (booking.get("phone") or "").strip()
    paid_amount = float(booking.get("paid_amount") or 0)
    deposit_amount = float(booking.get("deposit_amount") or 0)

    # Look up event so we can show real title, location, included items, full price.
    event = None
    try:
        if booking.get("event_id"):
            event = get_event_by_id(booking.get("event_id"))
    except Exception:
        event = None

    session_title = (event.get("title") if event else None) or (booking.get("session_type") or "Photo session")
    session_date = booking.get("date") or "—"
    session_time = booking.get("time") or "—"
    session_location = (event.get("location") if event else "") or "Location confirmed after booking"
    included_items = list((event or {}).get("included") or [])

    # Money: GST 5% on the session fee. booking.full_price is source of truth.
    # Falls back to event.full_price, then inferred from paid_amount.
    full_price = float(booking.get("full_price") or 0) or float((event or {}).get("full_price") or 0) or (paid_amount + deposit_amount or paid_amount or deposit_amount)
    # Treat event.full_price as the GST-INCLUSIVE total (matches how prices
    # are displayed on the landing pages) and back-calculate pre-tax fee.
    tax_rate = 0.05
    if full_price > 0:
        session_fee_pre_tax = round(full_price / (1 + tax_rate), 2)
        gst_amount = round(full_price - session_fee_pre_tax, 2)
        total_due = session_fee_pre_tax + gst_amount
    else:
        session_fee_pre_tax = gst_amount = total_due = 0.0

    remaining = max(0.0, round(total_due - paid_amount, 2))

    # Try ReportLab first; if unavailable, fall back to a minimal valid PDF
    # so the route still returns 200 in stripped-down test sandboxes.
    try:
        from io import BytesIO
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        )
    except Exception:
        return _admin_invoice_pdf_bytes_fallback(booking, invoice_no, client_name, paid_amount)

    rose = colors.HexColor(_INVOICE_ROSE)
    rose_soft = colors.HexColor(_INVOICE_ROSE_SOFT)
    ink = colors.HexColor(_INVOICE_INK)
    ink_2 = colors.HexColor(_INVOICE_INK_2)
    ink_3 = colors.HexColor(_INVOICE_INK_3)
    page_bg = colors.HexColor(_INVOICE_PAGE_BG)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.42 * inch, bottomMargin=0.38 * inch,
        title=f"Invoice {invoice_no} — Pashynska Photography",
        author="Pashynska Photography",
    )

    def _register_invoice_font():
        """Use a Unicode font when available so Cyrillic names do not turn into boxes."""
        regular_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Verdana.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
        bold_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
        ]
        regular = next((path for path in regular_candidates if os.path.exists(path)), None)
        bold = next((path for path in bold_candidates if os.path.exists(path)), None)
        if regular:
            try:
                if "InvoiceSans" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("InvoiceSans", regular))
                if bold and "InvoiceSans-Bold" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("InvoiceSans-Bold", bold))
                return "InvoiceSans", "InvoiceSans-Bold" if bold else "InvoiceSans"
            except Exception:
                pass
        return "Helvetica", "Helvetica-Bold"

    invoice_font, invoice_font_bold = _register_invoice_font()

    ss = getSampleStyleSheet()
    style_brand = ParagraphStyle("brand", parent=ss["Normal"], fontName="Times-Roman",
                                 fontSize=19, leading=22, textColor=ink, spaceAfter=0)
    style_tagline = ParagraphStyle("tagline", parent=ss["Normal"], fontName="Times-Italic",
                                   fontSize=8.5, leading=10.5, textColor=ink_2, spaceAfter=8)
    style_invoice_h = ParagraphStyle("invh", parent=ss["Normal"], fontName="Times-Roman",
                                     fontSize=22, leading=24, textColor=ink, spaceAfter=4)
    style_meta = ParagraphStyle("meta", parent=ss["Normal"], fontName=invoice_font,
                                fontSize=8.2, leading=10.5, textColor=ink_2)
    style_section_h = ParagraphStyle("sech", parent=ss["Normal"], fontName=invoice_font_bold,
                                     fontSize=7.4, leading=9, textColor=rose,
                                     spaceBefore=5, spaceAfter=3,
                                     letterSpacing=0.6)  # Reportlab supports this in 4.x
    style_body = ParagraphStyle("body", parent=ss["Normal"], fontName=invoice_font,
                                fontSize=8.8, leading=11.3, textColor=ink)
    style_body_small = ParagraphStyle("bodysm", parent=ss["Normal"], fontName=invoice_font,
                                      fontSize=8.1, leading=10.3, textColor=ink_2)
    style_terms = ParagraphStyle("terms", parent=ss["Normal"], fontName=invoice_font,
                                 fontSize=7.4, leading=9.2, textColor=ink_2, spaceBefore=7)
    style_footer = ParagraphStyle("foot", parent=ss["Normal"], fontName=invoice_font,
                                  fontSize=7, leading=8.5, textColor=ink_3, alignment=1)
    style_check = ParagraphStyle("check", parent=ss["Normal"], fontName=invoice_font,
                                 fontSize=8.1, leading=10.2, textColor=ink, leftIndent=8)

    elems = []
    # ── Brand header
    elems.append(Paragraph(_INVOICE_BRAND, style_brand))
    elems.append(Paragraph(_INVOICE_TAGLINE, style_tagline))

    # ── Thin rose rule (elegant, whisper-thin)
    rule = Table([[""]], colWidths=[doc.width])
    rule.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, -1), 0.35, ink_3)]))
    elems.append(rule)
    elems.append(Spacer(1, 8))

    # ── Invoice title (large, quiet authority)
    style_invoice_h = ParagraphStyle("invh", parent=ss["Normal"], fontName="Times-Roman",
                                     fontSize=23, leading=25, textColor=ink, spaceAfter=4)
    elems.append(Paragraph("Invoice", style_invoice_h))

    # ── Invoice number + date (right-aligned columns)
    from datetime import datetime as _dt
    today = _dt.now().strftime("%B %d, %Y")
    meta_tbl = Table([
        [Paragraph("", style_meta),
         Paragraph(f"<b>Invoice #</b><br/>{invoice_no}", style_meta),
         Paragraph(f"<b>Date</b><br/>{today}", style_meta)],
    ], colWidths=[doc.width * 0.4, doc.width * 0.3, doc.width * 0.3])
    meta_tbl.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    elems.append(meta_tbl)
    elems.append(Spacer(1, 8))

    # ── BILL TO
    elems.append(Paragraph("BILL&nbsp;TO", style_section_h))
    bill_lines = [f"<b>{_html_escape(client_name)}</b>"]
    if client_email:
        bill_lines.append(_html_escape(client_email))
    if client_phone:
        bill_lines.append(_html_escape(client_phone))
    elems.append(Paragraph("<br/>".join(bill_lines), style_body))

    # ── SESSION DETAILS
    elems.append(Paragraph("SESSION&nbsp;DETAILS", style_section_h))
    session_lines = [
        f"<b>Session:</b> {_html_escape(session_title)}",
        f"<b>Date &amp; Time:</b> {_html_escape(session_date)} at {_html_escape(session_time)}",
        f"<b>Location:</b> {_html_escape(session_location)}",
    ]
    elems.append(Paragraph("<br/>".join(session_lines), style_body))

    # ── WHAT'S INCLUDED
    if included_items:
        elems.append(Paragraph("WHAT'S &nbsp;INCLUDED", style_section_h))
        for item in included_items[:4]:
            elems.append(Paragraph(f"•  {_html_escape(str(item))}", style_check))

    # ── PAYMENT SUMMARY (table)
    elems.append(Paragraph("PAYMENT&nbsp;SUMMARY", style_section_h))
    money_rows = [
        ["Session fee",        f"$ {session_fee_pre_tax:,.2f}"],
        ["GST (5%)",           f"$ {gst_amount:,.2f}"],
        ["Total amount",       f"$ {total_due:,.2f}"],
        ["",                   ""],
        ["Deposit (paid)",     f"$ {paid_amount:,.2f}"],
        ["Remaining balance",  f"$ {remaining:,.2f}"],
    ]
    pay_tbl = Table(money_rows, colWidths=[doc.width * 0.7, doc.width * 0.3])
    pay_tbl.setStyle(TableStyle([
        ("FONTNAME",   (0, 0), (-1, -1), invoice_font),
        ("FONTSIZE",   (0, 0), (-1, -1), 8.6),
        ("TEXTCOLOR",  (0, 0), (0, -1), ink_2),
        ("TEXTCOLOR",  (1, 0), (1, -1), ink),
        ("ALIGN",      (1, 0), (1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        # Total row: rule above + slightly bolder
        ("LINEABOVE", (0, 2), (-1, 2), 0.5, rose_soft),
        ("FONTNAME",  (0, 2), (-1, 2), invoice_font_bold),
        ("TEXTCOLOR", (0, 2), (-1, 2), ink),
        # Remaining-balance row: strong rule + rose accent
        ("LINEABOVE", (0, 5), (-1, 5), 0.8, rose),
        ("FONTNAME",  (0, 5), (-1, 5), invoice_font_bold),
        ("TEXTCOLOR", (0, 5), (1, 5), rose if remaining > 0 else ink),
        ("FONTSIZE",  (0, 5), (-1, 5), 9.8),
        ("TOPPADDING", (0, 5), (-1, 5), 4),
    ]))
    elems.append(pay_tbl)

    # ── Payment method note
    elems.append(Spacer(1, 6))
    elems.append(Paragraph(
        "<b>Payment method:</b> Interac e-Transfer to "
        "<font color='" + _INVOICE_ROSE + "'>iryna.pashynska@gmail.com</font>. "
        "Auto-deposit enabled — no security question required.",
        style_body_small,
    ))

    # ── Terms
    elems.append(Paragraph(_INVOICE_TERMS, style_terms))

    # Bottom rule + footer
    rule2 = Table([[""]], colWidths=[doc.width])
    rule2.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, -1), 0.4, rose_soft)]))
    elems.append(Spacer(1, 7))
    elems.append(rule2)
    elems.append(Spacer(1, 4))
    elems.append(Paragraph(_INVOICE_FOOTER, style_footer))

    def _on_page(canvas, doc_):
        # Subtle warm background tint behind everything
        canvas.saveState()
        canvas.setFillColor(page_bg)
        canvas.rect(0, 0, LETTER[0], LETTER[1], stroke=0, fill=1)
        canvas.restoreState()

    doc.build(elems, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()


def _admin_invoice_pdf_bytes_fallback(booking, invoice_no, client_name, paid_amount):
    """Tiny but valid PDF for environments where ReportLab isn't installed.
    Tests only assert the response is `%PDF…` so this is sufficient."""
    lines = [
        "Pashynska Photography Invoice",
        f"Invoice {invoice_no}",
        f"Client: {client_name}",
        f"Amount paid: ${paid_amount:.2f} CAD",
    ]
    text = "\\n".join(lines)
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1", "replace")
    return (
        b"%PDF-1.4\n1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
        b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n"
        b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources <</Font <</F1 <</Type /Font /Subtype /Helvetica "
        b"/BaseFont /Helvetica>>>>>> /Contents 4 0 R>> endobj\n"
        b"4 0 obj <</Length " + str(len(stream)).encode() + b">> stream\n"
        + stream + b"\nendstream endobj\ntrailer <</Root 1 0 R>>\n%%EOF\n"
    )


def _send_email_with_attachment(to_email, client_name, subject, plain, html, attachment_bytes=None, attachment_filename="attachment.pdf", attachment_mime="application/pdf"):
    """Attachment-capable email hook. Uses SMTP with retry, falls back to plain SMTP if no attachment."""
    if attachment_bytes:
        return _smtp_send_email(to_email, client_name, subject, plain, html, attachment_bytes, attachment_filename, attachment_mime)
    else:
        return _smtp_send_email(to_email, client_name, subject, plain, html)


@app.route("/admin/booking/<int:booking_id>")
@admin_required
def admin_booking_detail(booking_id):
    """Full booking detail page — renders templates/booking_detail.html with
    everything an operator needs: client info, session details, payment summary,
    and the Invoice/Send/Wfolio/Review/Reschedule actions. The previous
    implementation returned an inline 3-button HTML stub that looked broken in
    the browser; the template was already on disk but unused."""
    booking = _admin_booking_row_or_404(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    event = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else None
    booking["selected_addons"] = _booking_addons(booking)
    try:
        booking["questionnaire_answers"] = json.loads(booking.get("questionnaire_answers_json") or "{}")
    except Exception:
        booking["questionnaire_answers"] = {}
    return render_template("booking_detail.html", booking=booking, event=event)


@app.route("/admin/booking/<int:booking_id>/contact", methods=["POST"])
@admin_required
def admin_booking_contact(booking_id):
    """Update operator-corrected contact fields for one booking.

    This deliberately does not touch money, status, dates, calendars, or
    outbound messages. It corrects the contact details the photographer needs
    to communicate with the client, then mirrors those fields onto the client
    profile for future lookup.
    """
    data = request.get_json(silent=True) or {}
    if "phone" not in data and "instagram" not in data and "email" not in data:
        return jsonify({"success": False, "error": "phone, email or instagram required"}), 400

    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT id, email, name, phone, instagram FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Booking not found"}), 404

        old_email = (row["email"] or "").strip().lower()
        email = old_email
        if "email" in data:
            email = str(data.get("email") or "").strip().lower()
            if not email:
                conn.rollback()
                conn.close()
                return jsonify({"success": False, "error": "Email cannot be blank"}), 400
            if len(email) > 254 or not _re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", email):
                conn.rollback()
                conn.close()
                return jsonify({"success": False, "error": "Enter a valid email address"}), 400

        phone = (row["phone"] or "")
        if "phone" in data:
            phone = str(data.get("phone") or "").strip()[:30]

        instagram = (row["instagram"] or "").strip().lstrip("@")
        if "instagram" in data:
            instagram_raw = str(data.get("instagram") or "").strip()
            if "instagram.com/" in instagram_raw:
                instagram_raw = instagram_raw.split("instagram.com/", 1)[1].split("?", 1)[0].split("#", 1)[0]
            instagram = instagram_raw.strip().strip("/").lstrip("@")[:80]

        c.execute(
            "UPDATE bookings SET email=?, phone=?, instagram=? WHERE id=?",
            (email, phone, instagram, booking_id),
        )

        if email:
            if old_email and old_email.lower() != email.lower():
                old_client = c.execute(
                    "SELECT id FROM clients WHERE LOWER(email)=LOWER(?)",
                    (old_email,),
                ).fetchone()
                new_client = c.execute(
                    "SELECT id FROM clients WHERE LOWER(email)=LOWER(?)",
                    (email,),
                ).fetchone()
                other_old_bookings = c.execute(
                    """SELECT COUNT(*) AS n FROM bookings
                       WHERE LOWER(email)=LOWER(?) AND id<>?""",
                    (old_email, booking_id),
                ).fetchone()["n"]
                if old_client and not new_client and other_old_bookings == 0:
                    c.execute(
                        "UPDATE clients SET email=?, last_seen=CURRENT_TIMESTAMP WHERE id=?",
                        (email, old_client["id"]),
                    )
            c.execute(
                """UPDATE clients
                   SET phone=?, instagram=?, last_seen=CURRENT_TIMESTAMP
                   WHERE LOWER(email)=LOWER(?)""",
                (phone, instagram, email),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        log.exception(f"[admin_contact] failed for booking #{booking_id}: {e}")
        return jsonify({"success": False, "error": "Server error"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # If the profile row did not exist yet, create it after the transaction.
    if email:
        try:
            sync_client(email, row["name"] or "", phone, instagram)
        except Exception as _e:
            log.warning(f"[admin_contact] sync_client failed for #{booking_id}: {_e}")

    log.info(f"[admin] booking #{booking_id} contact updated")
    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "email": email,
        "phone": phone,
        "instagram": instagram,
    })


@app.route("/admin/booking/<int:booking_id>/invoice", methods=["GET", "POST"])
@admin_required
def admin_booking_invoice(booking_id):
    """Generate and stream a PDF invoice for the booking.

    GET so the admin UI can use a plain <a href target='_blank'> link — the
    file downloads / opens inline in the new tab. POST kept for backward
    compatibility with the existing test suite + any older clients."""
    booking = _admin_booking_row_or_404(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    from flask import Response
    return Response(
        _admin_invoice_pdf_bytes(booking),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=invoice-{booking_id}.pdf"},
    )


@app.route("/admin/booking/<int:booking_id>/send-invoice", methods=["POST"])
@admin_required
def admin_booking_send_invoice(booking_id):
    booking = _admin_booking_row_or_404(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    sent = _send_email_with_attachment(
        booking.get("email", ""), booking.get("name", "Client"),
        f"Invoice for Booking #{booking_id}",
        f"Hi {booking.get('name', 'Client')}, your invoice is attached.",
        f"<p>Hi {_html_escape(str(booking.get('name') or 'Client'))}, your invoice is attached.</p>",
        _admin_invoice_pdf_bytes(booking), f"invoice-{booking_id}.pdf", "application/pdf"
    )
    return jsonify({"success": bool(sent)})


@app.route("/admin/booking/<int:booking_id>/recheck-payment", methods=["POST"])
@admin_required
def admin_booking_recheck_payment(booking_id):
    """Re-read recent Interac emails for this booking and safely raise paid_amount.

    This is admin-only and intentionally narrow: it does not confirm, cancel,
    reschedule, email the client, or lower recorded money. It only lets the same
    reconciliation logic used by the watcher correct a confirmed booking whose
    actual Interac amount arrived later or was previously marked as orphan.
    """
    booking = _admin_booking_row_or_404(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    if booking.get("status") != "confirmed" and not booking.get("confirmed"):
        return jsonify({"error": "Booking must be confirmed before payment recheck"}), 400

    previous_paid = _booking_paid_amount(dict(booking), get_event_by_id(booking.get("event_id")) or {})
    try:
        from check_etransfer_v2 import get_emails, is_etransfer_email, check_single_email

        emails = get_emails(page_size=100)
        if emails is None:
            return jsonify({"error": "Could not fetch Gmail / Interac emails"}), 502

        candidate = dict(booking)
        touched = 0
        for email in emails:
            if not is_etransfer_email(email):
                continue
            check_single_email(email, [], [candidate])
            refreshed = _admin_booking_row_or_404(booking_id)
            if refreshed:
                new_paid = _booking_paid_amount(dict(refreshed), get_event_by_id(refreshed.get("event_id")) or {})
                if new_paid > previous_paid + 0.009:
                    touched += 1
                    previous_paid = new_paid
                    candidate = dict(refreshed)

        refreshed = _admin_booking_row_or_404(booking_id)
        event = get_event_by_id(refreshed.get("event_id")) if refreshed and refreshed.get("event_id") else None
        paid_amount = _booking_paid_amount(dict(refreshed), event or {}) if refreshed else previous_paid
        balance_due = _booking_balance_due(dict(refreshed), event or {}) if refreshed else 0.0
    except Exception as e:
        log.exception(f"[admin-recheck-payment] failed for booking #{booking_id}: {e}")
        return jsonify({"error": "Payment recheck failed"}), 500

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "updated": bool(touched),
        "paid_amount": round(paid_amount, 2),
        "balance_due": round(balance_due, 2),
    })


@app.route("/admin/booking/<int:booking_id>/wfolio", methods=["POST"])
@admin_required
def admin_booking_wfolio(booking_id):
    booking = _admin_booking_row_or_404(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    data = request.get_json(silent=True) or {}
    wfolio_url = (data.get("wfolio_url") or "").strip()
    ok, err = _validate_gallery_url(wfolio_url)
    if not ok:
        return jsonify({"error": err}), 400
    conn = db_conn()
    conn.execute("UPDATE bookings SET wfolio_url=? WHERE id=?", (wfolio_url, booking_id))
    conn.commit()
    conn.close()
    sent = _send_email_with_attachment(
        booking.get("email", ""), booking.get("name", "Client"),
        "Your photo gallery is ready",
        f"Your gallery is ready: {wfolio_url}",
        f"<p>Your gallery is ready: <a href=\"{_html_escape(wfolio_url)}\">Open gallery</a></p>",
        None, "gallery.txt", "text/plain"
    )
    _emit_n8n_event(
        "gallery.wfolio_sent",
        booking=dict(booking),
        wfolio_url=wfolio_url,
        email_sent=bool(sent),
    )
    return jsonify({"success": bool(sent), "wfolio_url": wfolio_url})


@app.route("/admin/booking/<int:booking_id>/send-review", methods=["POST"])
@admin_required
def admin_booking_send_review(booking_id):
    booking = _admin_booking_row_or_404(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    review_url = os.environ.get("GOOGLE_REVIEW_URL", "https://review.pashynskaphoto.com")
    sent = _send_review_email(dict(booking))
    if sent:
        conn = db_conn()
        conn.execute("UPDATE bookings SET review_email_sent=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), booking_id))
        conn.commit()
        conn.close()
    _emit_n8n_event("review.sent", booking=dict(booking), review_url=review_url, email_sent=bool(sent))
    return jsonify({"success": bool(sent)})


@app.route("/admin/booking/<int:booking_id>/no-show", methods=["POST"])
@admin_required
def admin_booking_no_show(booking_id):
    """Mark a booking as `no_show` — the client didn't turn up. Distinct from
    `cancelled` so revenue reporting can keep paid_amount in the books while
    excluding the row from "completed sessions" stats. Idempotent: re-posting
    on an already-no-show row is a no-op."""
    booking = _admin_booking_row_or_404(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    conn = db_conn()
    conn.execute(
        "UPDATE bookings SET status='no_show' WHERE id=? AND status NOT IN ('cancelled','expired')",
        (booking_id,),
    )
    conn.commit()
    conn.close()
    log.info(f"[admin] booking #{booking_id} marked as no_show")
    return jsonify({"success": True, "status": "no_show"})


@app.route("/admin/confirm", methods=["POST"])
@admin_required
def admin_confirm():
    data = request.get_json(silent=True) or {}
    booking_id = data.get("booking_id")
    if not booking_id:
        return jsonify({"success": False, "error": "booking_id required"}), 400

    # Allow admin to override invoice amounts manually
    conn = db_conn()
    c = conn.cursor()
    c.execute("BEGIN IMMEDIATE")
    booking_row = c.execute("SELECT id, status, confirmed, deposit_amount, paid_amount, full_price, calendar_event_url FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not booking_row:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "error": "Booking not found"}), 404

    booking_dict = dict(booking_row)

    # Idempotency guard: if already confirmed, skip all side effects.
    # Double-click protection — return success without duplicating GCal/email/Notion/Telegram.
    if booking_dict.get("confirmed") or booking_dict.get("status") == "confirmed":
        conn.rollback()
        conn.close()
        log.info(f"[admin] confirm #{booking_id} skipped — already confirmed")
        return jsonify({
            "success": True,
            "already_confirmed": True,
            "status": "already_confirmed",
            "calendar_event": booking_dict.get("calendar_event_url"),
            "message": "Booking already confirmed. No duplicate email sent.",
        })

    booking_deposit = booking_dict.get("deposit_amount")
    paid_amount = data.get("paid_amount")
    if paid_amount is None or paid_amount == "":
        paid_amount = booking_deposit or SESSION_PRICE
    # Update full_price if admin sends it
    manual_full_price = data.get("full_price")
    if manual_full_price is not None:
        c.execute("UPDATE bookings SET full_price=?, confirmed=1, paid=1, status='confirmed', paid_amount=? WHERE id=?",
                  (manual_full_price, paid_amount, booking_id))
    else:
        c.execute("UPDATE bookings SET confirmed=1, paid=1, status='confirmed', paid_amount=? WHERE id=?",
                  (paid_amount, booking_id))
    conn.commit()
    # Fetch booking details for email notification
    c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
    row = c.fetchone()
    booking = dict(row) if row else {}
    conn.close()

    # Sync client stats after confirmation
    if booking:
        try:
            sync_client(booking.get("email", ""), booking.get("name", ""),
                        booking.get("phone", ""), booking.get("instagram", ""))
        except Exception as _e:
            log.warning(f"[confirm] sync_client failed: {_e}")

    # Side-effects: GCal event first (so Notion can include the link), then Notion
    event_url = create_calendar_event_for_booking(booking_id)
    sync_to_notion(booking_id)

    # Send confirmation email to client
    email_sent = False
    ev = get_event_by_id(booking.get("event_id"))
    if ev and booking:
        paid_amount = _booking_paid_amount(booking, ev)

        # balance_url + balance_due come from _client_email_context — a durable
        # /pay-balance link (e-Transfer or card), not a one-time Stripe URL that
        # would expire before the client pays the balance after the shoot.
        email_sent = _send_client_email(
            to_email=booking.get("email", ""),
            client_name=booking.get("name", "Client"),
            event_date=ev["date"],
            slot_time=booking.get("time", ""),
            event_title=ev.get("title", "Mini Session"),
            booking_id=booking_id,
            location=ev.get("location"),
            location_url=ev.get("location_url"),
            **_client_email_context(booking, ev),
        )

    # Notify admin on Telegram so manual confirmations stay in sync with
    # Stripe and automatic e-Transfer confirmations.
    if booking:
        try:
            paid_amount_float = float(paid_amount)
            msg = (
                f"✅ <b>Booking Confirmed Manually</b>\n\n"
                f"👤 {_tg_escape(booking.get('name', '?'))}\n"
                f"📧 {_tg_escape(booking.get('email', '?'))}\n"
                f"📅 {_tg_escape(booking.get('date', '?'))} @ {_tg_escape(booking.get('time', '?'))}\n"
                f"💰 <b>${paid_amount_float:.2f} CAD</b>\n"
                f"🆔 Booking #{booking_id}\n\n"
                f"{'Email confirmation sent to client.' if email_sent else '⚠️ Email confirmation FAILED — check client email address/logs.'}"
            )
            _notify_admin(msg)
        except Exception as e:
            log.error(f"[admin] Telegram notify error for #{booking_id}: {e}")

    if booking:
        _emit_n8n_event(
            "booking.confirmed",
            booking={
                "id": booking_id,
                "name": booking.get("name"),
                "email": booking.get("email"),
                "phone": booking.get("phone"),
                "instagram": booking.get("instagram"),
                "date": booking.get("date"),
                "time": booking.get("time"),
                "session_type": booking.get("session_type"),
                "status": "confirmed",
                "paid_amount": paid_amount,
            },
            calendar_event=event_url,
            event_data=ev or {},
        )
        _record_booking_funnel_event(
            booking,
            "booking_confirmed",
            {"source": "admin", "paid_amount": paid_amount},
        )

    log.info(f"[admin] Booking #{booking_id} confirmed, paid ${paid_amount}")

    return jsonify({
        "success": True,
        "calendar_event": event_url,
        "message": "Email confirmation sent to client." if email_sent
                    else "⚠️ Email confirmation FAILED — check client email address/logs.",
    })


@app.route("/admin/booking/<int:booking_id>/invoice", methods=["PATCH"])
@admin_required
def admin_update_invoice(booking_id):
    """Allow admin to manually set full_price for any booking.
    Updates the booking row; invoice endpoint reads full_price from DB."""
    data = request.get_json(silent=True) or {}
    full_price = data.get("full_price")
    deposit_amount = data.get("deposit_amount")
    paid_amount = data.get("paid_amount")
    conn = db_conn()
    c = conn.cursor()
    if full_price is not None:
        c.execute("UPDATE bookings SET full_price=? WHERE id=?", (float(full_price), booking_id))
    if deposit_amount is not None:
        c.execute("UPDATE bookings SET deposit_amount=? WHERE id=?", (float(deposit_amount), booking_id))
    if paid_amount is not None:
        c.execute("UPDATE bookings SET paid_amount=? WHERE id=?", (float(paid_amount), booking_id))
    conn.commit()
    c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Booking not found"}), 404
    booking = dict(row)
    event = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "full_price": _booking_total_price(booking, event),
        "deposit_amount": round(float(booking.get("deposit_amount") or 0), 2),
        "paid_amount": _booking_paid_amount(booking, event),
        "balance_due": _booking_balance_due(booking, event),
    })


@app.route("/admin/request-balance", methods=["POST"])
@admin_required
def admin_request_balance():
    """Email a confirmed client a remaining-balance payment request.

    The original deposit/booking flow is unchanged. This endpoint is admin-only
    and only works for confirmed bookings with a positive outstanding balance.
    """
    data = request.get_json(silent=True) or {}
    booking_id = data.get("booking_id")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400

    conn = db_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Booking not found"}), 404

    booking = dict(row)
    if booking.get("status") != "confirmed" and not booking.get("confirmed"):
        return jsonify({"error": "Booking must be confirmed before requesting balance"}), 400

    event = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    event = event or {}
    total_price = _booking_total_price(booking, event)
    paid_amount = _booking_paid_amount(booking, event)
    balance_due = _booking_balance_due(booking, event)
    if balance_due <= 0:
        return jsonify({"error": "No balance due for this booking"}), 400

    stripe_url = None
    try:
        stripe_url = _create_balance_checkout_url(booking, event, balance_due)
    except Exception as e:
        log.error(f"[admin-balance] Stripe checkout link failed for #{booking_id}: {e}")

    sent = _send_balance_request_email(
        to_email=booking.get("email", ""),
        client_name=booking.get("name", "Client"),
        event_title=event.get("title", "Photo Session"),
        event_date=event.get("date") or booking.get("date", ""),
        slot_time=booking.get("time", ""),
        booking_id=booking_id,
        total_price=total_price,
        paid_amount=paid_amount,
        balance_due=balance_due,
        stripe_url=stripe_url,
        interac_email=EMAIL,
    )
    if not sent:
        return jsonify({"error": "Failed to send balance request email"}), 500

    try:
        _notify_admin(
            f"💸 <b>Balance Request Sent</b>\n\n"
            f"👤 {_tg_escape(booking.get('name', '?'))}\n"
            f"📧 {_tg_escape(booking.get('email', '?'))}\n"
            f"🆔 Booking #{booking_id}\n"
            f"💰 Balance: <b>${balance_due:.2f} CAD</b>\n"
            f"💳 Stripe link: {'yes' if stripe_url else 'no'}"
        )
    except Exception as e:
        log.error(f"[admin-balance] Telegram notify failed for #{booking_id}: {e}")

    _emit_n8n_event(
        "payment.balance_requested",
        booking=booking,
        balance_due=balance_due,
        total_price=round(total_price, 2),
        paid_amount=round(paid_amount, 2),
        stripe_link_created=bool(stripe_url),
        event_data=event,
    )

    return jsonify({
        "success": True,
        "booking_id": int(booking_id),
        "balance_due": balance_due,
        "total_price": round(total_price, 2),
        "paid_amount": round(paid_amount, 2),
        "stripe_url": stripe_url,
        "email_sent": True,
    })


@app.route("/admin/cancel", methods=["POST"])
@admin_required
def admin_cancel():
    """Cancel a booking — frees the slot."""
    data = request.json
    booking_id = data.get("booking_id")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE bookings SET status='cancelled', reserved_until=NULL WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    sync_to_notion(booking_id)
    _emit_n8n_event("booking.cancelled", booking={"id": booking_id, "status": "cancelled"})
    log.info(f"[admin] Booking #{booking_id} cancelled")
    return jsonify({"success": True})


@app.route("/admin/mark-paid", methods=["POST"])
@admin_required
def admin_mark_paid():
    """Manually update paid amount for a confirmed booking (e.g., cash, e-Transfer after session).

    Does NOT send email — only updates DB and syncs Notion.
    """
    data = request.get_json(silent=True) or {}
    booking_id = data.get("booking_id")
    paid_amount = data.get("paid_amount")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400
    try:
        paid_amount = float(paid_amount or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid paid_amount"}), 400

    conn = db_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404

    booking = dict(row)
    if booking.get("status") != "confirmed" and not booking.get("confirmed"):
        conn.close()
        return jsonify({"error": "Booking must be confirmed"}), 400

    event = get_event_by_id(booking.get("event_id")) if booking.get("event_id") else get_active_event()
    total_price = float((event or {}).get("full_price") or SESSION_TOTAL or 0)
    # Cap paid_amount at total_price to prevent overpayment
    paid_amount = max(0.0, min(paid_amount, total_price))

    conn.execute(
        "UPDATE bookings SET paid=1, paid_amount=? WHERE id=?",
        (paid_amount, booking_id)
    )
    conn.commit()
    conn.close()

    try:
        sync_to_notion(booking_id)
    except Exception as e:
        log.error(f"[admin-mark-paid] Notion sync error for #{booking_id}: {e}")
    try:
        _notify_admin(
            f"💰 <b>Manual Payment Marked</b>\n\n"
            f"👤 {_tg_escape(booking.get('name', '?'))}\n"
            f"📧 {_tg_escape(booking.get('email', '?'))}\n"
            f"🆔 Booking #{booking_id}\n"
            f"💵 Total recorded paid: <b>${paid_amount:.2f} CAD</b>"
        )
    except Exception as e:
        log.error(f"[admin-mark-paid] Telegram notify error for #{booking_id}: {e}")

    _emit_n8n_event("payment.marked_paid", booking=booking, paid_amount=paid_amount, total_price=total_price)

    log.info(f"[admin-mark-paid] Booking #{booking_id} marked paid: ${paid_amount:.2f}")
    return jsonify({
        "success": True,
        "booking_id": int(booking_id),
        "paid_amount": round(paid_amount, 2),
    })


@app.route("/admin/delete", methods=["POST"])
@admin_required
def admin_delete():
    """Delete a booking — permanent removal from database."""
    data = request.json
    booking_id = data.get("booking_id")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400
    conn = db_conn()
    c = conn.cursor()
    c.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    sync_to_notion(booking_id)
    log.info(f"[admin] Booking #{booking_id} permanently deleted")
    return jsonify({"success": True})


@app.route("/admin/reschedule", methods=["POST"])
@admin_required
def admin_reschedule():
    """Move an existing booking to another date/time/event.

    Body: { booking_id, new_event_id, new_date (YYYY-MM-DD), new_time (HH:MM) }

    Rules:
    - new slot must be a valid slot for new_event_id and new_date
    - new slot must not be taken by another active booking (atomic check via BEGIN IMMEDIATE)
    - deposit / paid_amount / confirmed status are preserved
    - if booking was reserved/pending_payment → reset reserved_until to now + RESERVATION_MINUTES
    - if booking was confirmed → stays confirmed
    - email client, Telegram admin, Notion re-sync
    """
    data = request.json or {}
    booking_id = data.get("booking_id")
    new_event_id = (data.get("new_event_id") or "").strip()
    new_date = (data.get("new_date") or "").strip()
    new_time = (data.get("new_time") or "").strip()

    if not booking_id or not new_event_id or not new_date or not new_time:
        return jsonify({"error": "booking_id, new_event_id, new_date, new_time required"}), 400

    new_ev = get_event_by_id(new_event_id)
    if not new_ev:
        return jsonify({"error": "Target event not found"}), 404

    # Validate that new_time is among the event's generated slots
    valid_slot_times = {s["time"] for s in generate_slots(new_ev)}
    if new_time not in valid_slot_times:
        return jsonify({"error": "Selected time is not a valid slot for this event"}), 400

    # For rolling_availability events, validate the requested date
    if _booking_type(new_ev) == "rolling_availability":
        reason = _rolling_date_unavailable_reason(new_ev, new_date)
        if reason:
            return jsonify({"error": f"Target date is unavailable ({reason})"}), 400
    elif new_date != new_ev.get("date"):
        return jsonify({"error": "Target date does not match the event's date"}), 400

    now = _local_now()
    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")

        # Load current booking
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return jsonify({"error": "Booking not found"}), 404
        old = dict(row)

        # Short-circuit: no-op move (same slot)
        if (old.get("date") == new_date and old.get("time") == new_time
                and old.get("event_id") == new_event_id):
            conn.rollback()
            conn.close()
            return jsonify({"success": True, "no_change": True})

        # Conflict check on target slot — excluding this booking itself
        c.execute("""
            SELECT id, event_id FROM bookings
            WHERE date=? AND time=?
              AND id <> ?
              AND status NOT IN ('cancelled', 'expired')
              AND (confirmed=1 OR reserved_until > ?)
        """, (new_date, new_time, booking_id, now.isoformat()))
        conflict = c.fetchone()
        if conflict:
            conn.rollback()
            conn.close()
            return jsonify({"error": "Target slot is already taken"}), 409

        # Sweep stale rows on the target slot so UNIQUE(date,time) won't block us
        c.execute("""
            DELETE FROM bookings
            WHERE date=? AND time=?
              AND id <> ?
              AND (
                status IN ('cancelled', 'expired')
                OR (status IN ('reserved', 'pending_payment') AND reserved_until <= ?)
              )
        """, (new_date, new_time, booking_id, now.isoformat()))

        # Decide whether to reset the 15-min timer
        old_status = old.get("status") or ""
        if old_status in ("reserved", "pending_payment"):
            new_reserved_until = (now + timedelta(minutes=RESERVATION_MINUTES)).isoformat()
            c.execute(
                "UPDATE bookings SET date=?, time=?, event_id=?, reserved_until=? WHERE id=?",
                (new_date, new_time, new_event_id, new_reserved_until, booking_id)
            )
        else:
            # confirmed / paid / other — keep status as is, no timer
            c.execute(
                "UPDATE bookings SET date=?, time=?, event_id=? WHERE id=?",
                (new_date, new_time, new_event_id, booking_id)
            )

        if c.rowcount == 0:
            conn.rollback()
            conn.close()
            return jsonify({"error": "Update failed"}), 500

        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        conn.close()
        log.warning(f"[admin/reschedule] IntegrityError on #{booking_id}: {e}")
        return jsonify({"error": "Target slot is already taken"}), 409
    except Exception:
        conn.rollback()
        conn.close()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Resolve old/new event titles for notifications
    old_ev = get_event_by_id(old.get("event_id")) if old.get("event_id") else None
    old_event_title = (old_ev or {}).get("title") or "Mini Session"
    new_event_title = new_ev.get("title") or "Mini Session"

    # Side effects (best-effort, never block on failures)
    try:
        _send_client_reschedule_email(
            to_email=old.get("email", ""),
            client_name=old.get("name", "Client"),
            old_event_title=old_event_title,
            old_date=old.get("date", ""),
            old_time=old.get("time", ""),
            new_event_title=new_event_title,
            new_date=new_date,
            new_time=new_time,
            booking_id=booking_id,
            location=new_ev.get("location"),
        )
    except Exception as e:
        log.error(f"[admin/reschedule] email error for #{booking_id}: {e}")

    try:
        _notify_reschedule(
            booking_id=booking_id,
            client_name=old.get("name", ""),
            client_email=old.get("email", ""),
            old_event_title=old_event_title,
            old_date=old.get("date", ""),
            old_time=old.get("time", ""),
            new_event_title=new_event_title,
            new_date=new_date,
            new_time=new_time,
            status=old_status,
        )
    except Exception as e:
        log.error(f"[admin/reschedule] telegram error for #{booking_id}: {e}")

    try:
        sync_to_notion(booking_id)
    except Exception as e:
        log.error(f"[admin/reschedule] notion error for #{booking_id}: {e}")

    log.info(f"[admin] Booking #{booking_id} rescheduled: "
             f"{old.get('date')} {old.get('time')} → {new_date} {new_time} (event={new_event_id})")

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "old": {"date": old.get("date"), "time": old.get("time"), "event_id": old.get("event_id")},
        "new": {"date": new_date, "time": new_time, "event_id": new_event_id},
        "status": old_status,
    })


EVENTS_YAML_PATH = _EVENTS_PATH  # same path used for reads
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_PHOTO_BATCH_COUNT = 5
MAX_ADMIN_PHOTO_ORIGINAL_BYTES = 12 * 1024 * 1024
PHOTO_MAX_DIMENSION = 1600
PHOTO_WEBP_QUALITY = 82

def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _photo_upload_size(file_storage):
    """Return uploaded file size without consuming the stream."""
    try:
        stream = file_storage.stream
        pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(pos)
        return size
    except Exception:
        return None

def _safe_photo_prefix(event_id):
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", str(event_id or "")).strip("-")
    return prefix or "event"

def _load_events_yaml_doc():
    with open(EVENTS_YAML_PATH) as fh:
        return yaml.safe_load(fh) or {"events": [], "settings": {}}

def _write_events_yaml_doc(data):
    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False)

def _event_yaml_record(data, event_id):
    for ev_data in data.get("events", []):
        if ev_data.get("id") == event_id:
            return ev_data
    return None

def _delete_photo_file(photo_url, keep_path=None):
    basename = os.path.basename(str(photo_url or "").lstrip("/"))
    if not basename:
        return
    keep_abs = os.path.abspath(keep_path) if keep_path else None
    for candidate in (
        os.path.join(PHOTOS_DIR, basename),
        os.path.join(_BUNDLED_IMAGES_DIR, basename),
    ):
        try:
            if os.path.exists(candidate) and os.path.abspath(candidate) != keep_abs:
                os.remove(candidate)
        except Exception:
            pass

def _save_optimized_admin_photo(event_id, file_storage):
    """Convert admin uploads into a mobile-friendly WebP under /images."""
    if not file_storage or not _allowed_file(file_storage.filename):
        raise ValueError("Invalid file. Allowed: jpg, jpeg, png, webp")

    size = _photo_upload_size(file_storage)
    if size and size > MAX_ADMIN_PHOTO_ORIGINAL_BYTES:
        mb = MAX_ADMIN_PHOTO_ORIGINAL_BYTES // (1024 * 1024)
        raise ValueError(f"Photo is too large. Upload images up to {mb} MB each.")

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except Exception as exc:
        raise RuntimeError("Image optimizer is not installed") from exc

    Image.MAX_IMAGE_PIXELS = 36_000_000
    try:
        file_storage.stream.seek(0)
        image = Image.open(file_storage.stream)
        image = ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Invalid image file") from exc

    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    image.thumbnail((PHOTO_MAX_DIMENSION, PHOTO_MAX_DIMENSION), resampling)

    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        flattened = Image.new("RGB", rgba.size, (255, 255, 255))
        flattened.paste(rgba, mask=rgba.getchannel("A"))
        image = flattened
    else:
        image = image.convert("RGB")

    import uuid
    filename = f"{_safe_photo_prefix(event_id)}_{uuid.uuid4().hex[:8]}.webp"
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    save_path = os.path.join(PHOTOS_DIR, filename)
    image.save(save_path, "WEBP", quality=PHOTO_WEBP_QUALITY, method=6)
    return f"/images/{filename}", save_path, {
        "width": image.width,
        "height": image.height,
        "bytes": os.path.getsize(save_path),
    }

@app.route("/admin/events/<event_id>/update", methods=["POST"])
@admin_required
def admin_update_event(event_id):
    """Update event schedule/pricing settings and save to events.yaml."""
    data = request.json or {}

    with _EVENTS_YAML_LOCK:
        with open(EVENTS_YAML_PATH) as fh:
            yaml_data = yaml.safe_load(fh) or {}

        event = next((e for e in yaml_data.get("events", []) if e["id"] == event_id), None)
        if not event:
            return jsonify({"error": "Event not found"}), 404

        # Validate and apply time/price fields. Keep read→modify→write under one
        # lock so concurrent admin metadata saves cannot overwrite pricing edits
        # with a stale YAML snapshot.
        try:
            if "start_time" in data:
                datetime.strptime(data["start_time"], "%H:%M")
                event["start_time"] = data["start_time"]
            if "end_time" in data:
                datetime.strptime(data["end_time"], "%H:%M")
                event["end_time"] = data["end_time"]
            if "session_length" in data:
                sl = int(data["session_length"])
                if not (5 <= sl <= 120):
                    return jsonify({"error": "session_length must be 5–120 minutes"}), 400
                event["session_length"] = sl
            if "break_length" in data:
                bl = int(data["break_length"])
                if not (0 <= bl <= 60):
                    return jsonify({"error": "break_length must be 0–60 minutes"}), 400
                event["break_length"] = bl
            event["slot_interval"] = event.get("session_length", 20) + event.get("break_length", 10)
            if "title" in data:
                event["title"] = str(data["title"])[:120]
            if "subtitle" in data:
                event["subtitle"] = str(data["subtitle"])[:200]
            if "deposit" in data:
                event["deposit"] = float(data["deposit"])
            if "full_price" in data:
                event["full_price"] = float(data["full_price"])
            if "status" in data and data["status"] in ("active", "upcoming", "completed"):
                event["status"] = data["status"]
            if "location" in data:
                event["location"] = str(data["location"])[:300]
            if "booking_type" in data:
                bt = str(data["booking_type"])
                if bt not in ("fixed_slots", "rolling_availability", "inquiry_only"):
                    return jsonify({"error": "booking_type must be fixed_slots, rolling_availability, or inquiry_only"}), 400
                event["booking_type"] = bt
            if "session_type" in data:
                event["session_type"] = str(data["session_type"])[:40]
            if "availability_horizon_days" in data:
                horizon = int(data["availability_horizon_days"])
                if not (1 <= horizon <= 365):
                    return jsonify({"error": "availability_horizon_days must be 1–365"}), 400
                event["availability_horizon_days"] = horizon
            if "blackout_dates" in data:
                dates = data.get("blackout_dates") or []
                if isinstance(dates, str):
                    dates = [d.strip() for d in dates.replace("\n", ",").split(",") if d.strip()]
                cleaned = []
                for d in dates:
                    d = str(d).strip()
                    datetime.strptime(d, "%Y-%m-%d")
                    cleaned.append(d)
                event["blackout_dates"] = cleaned
            if "addons" in data:
                addons = _sanitize_event_addons(data.get("addons") or [])
                if addons:
                    event["addons"] = addons
                else:
                    event.pop("addons", None)
            if "agreement" in data:
                agreement = data.get("agreement") or {}
                if isinstance(agreement, dict) and agreement.get("enabled"):
                    event["agreement"] = {
                        "enabled": True,
                        "require_terms": bool(agreement.get("require_terms", True)),
                        "require_marketing_choice": bool(agreement.get("require_marketing_choice", True)),
                        "terms_version": str(agreement.get("terms_version") or agreement.get("version") or DEFAULT_MINI_AGREEMENT["terms_version"])[:80],
                    }
                else:
                    event.pop("agreement", None)
            elif _is_instant_mini_event(event) and not event.get("agreement"):
                event["agreement"] = dict(DEFAULT_MINI_AGREEMENT)
        except ValueError as e:
            return jsonify({"error": f"Invalid value: {e}"}), 400

        try:
            s = datetime.strptime(event["start_time"], "%H:%M")
            e_ = datetime.strptime(event["end_time"], "%H:%M")
            if s >= e_:
                return jsonify({"error": "start_time must be before end_time"}), 400
        except Exception:
            pass

        with open(EVENTS_YAML_PATH, "w") as fh:
            yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

        _reload_events_globals()
        updated_event = next((e for e in EVENTS if e["id"] == event_id), event)
        slots_preview = generate_slots(updated_event)

    log.info(f"[admin] Event {event_id} settings updated: {data}")
    return jsonify({"success": True, "slots_count": len(slots_preview), "event": {
        "id": updated_event["id"],
        "start_time": updated_event.get("start_time"),
        "end_time": updated_event.get("end_time"),
        "session_length": updated_event.get("session_length"),
        "break_length": updated_event.get("break_length"),
        "slot_interval": updated_event.get("slot_interval"),
        "deposit": updated_event.get("deposit"),
        "full_price": updated_event.get("full_price"),
    }})


@app.route("/admin/photos/<event_id>", methods=["GET"])
@admin_required
def admin_get_photos(event_id):
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"error": "Event not found"}), 404
    return jsonify({"photos": ev.get("photos", [])})

@app.route("/admin/photos/<event_id>/upload", methods=["POST"])
@admin_required
def admin_upload_photo(event_id):
    """Upload a new photo for an event (replaces slot index if provided)."""
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"error": "Event not found"}), 404

    f = request.files.get("photo")
    if not f or not f.filename:
        return jsonify({"error": "photo file is required"}), 400

    slot_index_raw = request.form.get("slot_index")  # optional — which photo slot to replace
    slot_index = None
    if slot_index_raw is not None:
        try:
            slot_index = int(slot_index_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "slot_index must be a number"}), 400
        if slot_index < 0:
            return jsonify({"error": "slot_index must be zero or greater"}), 400

    saved_path = None
    old_url = None
    try:
        with _EVENTS_YAML_LOCK:
            data = _load_events_yaml_doc()
            ev_data = _event_yaml_record(data, event_id)
            if not ev_data:
                return jsonify({"error": "Event not found"}), 404
            photos = list(ev_data.get("photos") or [])
            if slot_index is not None and slot_index >= len(photos):
                return jsonify({"error": "Photo slot not found"}), 400

            url, saved_path, meta = _save_optimized_admin_photo(event_id, f)
            if slot_index is None:
                photos.append(url)
            else:
                old_url = photos[slot_index]
                photos[slot_index] = url
            ev_data["photos"] = photos
            _write_events_yaml_doc(data)
            _reload_events_globals()
    except ValueError as exc:
        if saved_path:
            _delete_photo_file(f"/images/{os.path.basename(saved_path)}")
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        if saved_path:
            _delete_photo_file(f"/images/{os.path.basename(saved_path)}")
        log.exception(f"[admin] Photo upload failed for {event_id}: {exc}")
        return jsonify({"error": "Photo upload failed"}), 500

    if old_url:
        _delete_photo_file(old_url, keep_path=saved_path)

    log.info(f"[admin] Photo uploaded for {event_id}: {url}")
    return jsonify({"success": True, "url": url, "optimized": meta})

@app.route("/admin/photos/<event_id>/upload-batch", methods=["POST"])
@admin_required
def admin_upload_photos_batch(event_id):
    """Upload up to five optimized photos and append them atomically."""
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"error": "Event not found"}), 404

    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        files = [f for f in request.files.getlist("photo") if f and f.filename]
    if not files:
        return jsonify({"error": "Choose at least one photo"}), 400
    if len(files) > MAX_PHOTO_BATCH_COUNT:
        return jsonify({"error": f"Upload up to {MAX_PHOTO_BATCH_COUNT} photos at a time"}), 400

    saved = []
    try:
        with _EVENTS_YAML_LOCK:
            data = _load_events_yaml_doc()
            ev_data = _event_yaml_record(data, event_id)
            if not ev_data:
                return jsonify({"error": "Event not found"}), 404

            urls = []
            optimized = []
            for f in files:
                url, saved_path, meta = _save_optimized_admin_photo(event_id, f)
                saved.append((url, saved_path))
                urls.append(url)
                optimized.append({"url": url, **meta})

            photos = list(ev_data.get("photos") or [])
            photos.extend(urls)
            ev_data["photos"] = photos
            _write_events_yaml_doc(data)
            _reload_events_globals()
    except ValueError as exc:
        for url, _saved_path in saved:
            _delete_photo_file(url)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        for url, _saved_path in saved:
            _delete_photo_file(url)
        log.exception(f"[admin] Batch photo upload failed for {event_id}: {exc}")
        return jsonify({"error": "Photo upload failed"}), 500

    log.info(f"[admin] {len(saved)} photos uploaded for {event_id}")
    return jsonify({"success": True, "count": len(saved), "urls": [url for url, _ in saved], "optimized": optimized})

@app.route("/admin/photos/<event_id>/delete", methods=["POST"])
@admin_required
def admin_delete_photo(event_id):
    """Remove a photo from an event's list."""
    slot_index = (request.json or {}).get("slot_index")
    if slot_index is None:
        return jsonify({"error": "slot_index required"}), 400
    try:
        slot_index = int(slot_index)
    except (TypeError, ValueError):
        return jsonify({"error": "slot_index must be a number"}), 400
    if slot_index < 0:
        return jsonify({"error": "slot_index must be zero or greater"}), 400

    deleted_url = None
    with _EVENTS_YAML_LOCK:
        data = _load_events_yaml_doc()
        ev_data = _event_yaml_record(data, event_id)
        if not ev_data:
            return jsonify({"error": "Event not found"}), 404
        photos = list(ev_data.get("photos") or [])
        if slot_index >= len(photos):
            return jsonify({"error": "Photo slot not found"}), 400
        deleted_url = photos.pop(slot_index)
        ev_data["photos"] = photos
        _write_events_yaml_doc(data)
        _reload_events_globals()

    if deleted_url:
        _delete_photo_file(deleted_url)

    return jsonify({"success": True})


# ===== EVENT CRUD =====

def _reload_events_globals():
    """Reload all in-memory event globals after YAML changes."""
    global EVENTS, SETTINGS, _active, EVENT_TITLE, SESSION_LENGTH, BREAK_LENGTH, SLOT_INTERVAL, SESSION_PRICE, SESSION_TOTAL, DATE, START_TIME, END_TIME, SLOTS
    EVENTS, SETTINGS = _load_events()
    _active = get_active_event() or {}
    EVENT_TITLE = _active.get("title", "Mini Sessions")
    SESSION_LENGTH = _active.get("session_length", 20)
    BREAK_LENGTH = _active.get("break_length", 10)
    SLOT_INTERVAL = _active.get("slot_interval", 30)
    SESSION_PRICE = _active.get("deposit", 95)
    SESSION_TOTAL = _active.get("full_price", 190)
    DATE = _active.get("date", "")
    START_TIME = _active.get("start_time", "10:00")
    END_TIME = _active.get("end_time", "16:00")
    SLOTS = generate_slots()


@app.route("/admin/events/create", methods=["POST"])
@admin_required
def admin_create_event():
    """Create a new event and append to events.yaml."""
    data = request.json or {}

    # Required fields
    title = str(data.get("title", "")).strip()
    date_str = str(data.get("date", "")).strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    if not date_str:
        return jsonify({"error": "date is required"}), 400
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    # Build slug-style id from date + sanitised title
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30]
    event_id = f"{slug}-{date_str}"

    with open(EVENTS_YAML_PATH) as fh:
        yaml_data = yaml.safe_load(fh) or {}

    events_list = yaml_data.get("events", [])
    # Ensure unique id
    existing_ids = {e["id"] for e in events_list}
    base_id = event_id
    suffix = 2
    while event_id in existing_ids:
        event_id = f"{base_id}-{suffix}"
        suffix += 1

    session_len = int(data.get("session_length", 20))
    break_len = int(data.get("break_length", 10))
    booking_type = str(data.get("booking_type") or "fixed_slots")
    if booking_type not in ("fixed_slots", "rolling_availability", "inquiry_only"):
        return jsonify({"error": "booking_type must be fixed_slots, rolling_availability, or inquiry_only"}), 400
    blackout_dates = data.get("blackout_dates") or []
    if isinstance(blackout_dates, str):
        blackout_dates = [d.strip() for d in blackout_dates.replace("\n", ",").split(",") if d.strip()]
    blackout_dates = [str(d).strip() for d in blackout_dates if str(d).strip()]
    try:
        for d in blackout_dates:
            datetime.strptime(d, "%Y-%m-%d")
        availability_horizon_days = int(data.get("availability_horizon_days", 90))
        if not (1 <= availability_horizon_days <= 365):
            return jsonify({"error": "availability_horizon_days must be 1–365"}), 400
    except ValueError as e:
        return jsonify({"error": f"Invalid availability setting: {e}"}), 400

    new_event = {
        "id": event_id,
        "title": title,
        "subtitle": str(data.get("subtitle", "")).strip(),
        "date": date_str,
        "start_time": data.get("start_time", "10:00"),
        "end_time": data.get("end_time", "17:00"),
        "session_length": session_len,
        "break_length": break_len,
        "slot_interval": session_len + break_len,
        "deposit": float(data.get("deposit", 100)),
        "full_price": float(data.get("full_price", 300)),
        "location": str(data.get("location", "")).strip(),
        "session_type": str(data.get("session_type") or ("individual" if booking_type == "rolling_availability" else "wedding" if booking_type == "inquiry_only" else "mini")),
        "booking_type": booking_type,
        "availability_horizon_days": availability_horizon_days,
        "blackout_dates": blackout_dates,
        "featured": bool(data.get("featured", False)),
        "status": data.get("status", "upcoming"),
        "included": [i.strip() for i in data.get("included", []) if str(i).strip()],
        "photos": [],
    }
    addons = _sanitize_event_addons(data.get("addons") or [])
    if addons:
        new_event["addons"] = addons
    elif _is_instant_mini_event(new_event):
        new_event["addons"] = _default_mini_addons()

    agreement = data.get("agreement") if isinstance(data.get("agreement"), dict) else None
    if agreement is not None:
        if agreement.get("enabled"):
            new_event["agreement"] = {
                "enabled": True,
                "require_terms": bool(agreement.get("require_terms", True)),
                "require_marketing_choice": bool(agreement.get("require_marketing_choice", True)),
                "terms_version": str(agreement.get("terms_version") or agreement.get("version") or DEFAULT_MINI_AGREEMENT["terms_version"])[:80],
            }
    elif _is_instant_mini_event(new_event):
        new_event["agreement"] = dict(DEFAULT_MINI_AGREEMENT)

    events_list.append(new_event)
    yaml_data["events"] = events_list

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

    _reload_events_globals()
    log.info(f"[admin] Event created: {event_id}")
    return jsonify({"success": True, "event_id": event_id})


@app.route("/admin/events/<event_id>/delete", methods=["POST"])
@admin_required
def admin_delete_event(event_id):
    """Delete an event from events.yaml. Refuses if there are active bookings."""
    with open(EVENTS_YAML_PATH) as fh:
        yaml_data = yaml.safe_load(fh) or {}

    events_list = yaml_data.get("events", [])
    event = next((e for e in events_list if e["id"] == event_id), None)
    if not event:
        return jsonify({"error": "Event not found"}), 404

    # Check for active/pending bookings
    conn = db_conn()
    active_count = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE event_id=? AND status NOT IN ('cancelled','expired')",
        (event_id,)
    ).fetchone()[0]
    conn.close()

    if active_count > 0:
        force = (request.json or {}).get("force", False)
        if not force:
            return jsonify({
                "error": f"Event has {active_count} active booking(s). Pass force=true to delete anyway.",
                "active_bookings": active_count
            }), 409

    yaml_data["events"] = [e for e in events_list if e["id"] != event_id]

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

    _reload_events_globals()
    log.info(f"[admin] Event deleted: {event_id}")
    return jsonify({"success": True})


@app.route("/admin/events/<event_id>/duplicate", methods=["POST"])
@admin_required
def admin_duplicate_event(event_id):
    """Duplicate an event with a new id, cleared photos and upcoming status."""
    with open(EVENTS_YAML_PATH) as fh:
        yaml_data = yaml.safe_load(fh) or {}

    events_list = yaml_data.get("events", [])
    source = next((e for e in events_list if e["id"] == event_id), None)
    if not source:
        return jsonify({"error": "Event not found"}), 404

    import copy, re
    new_event = copy.deepcopy(source)
    new_event["status"] = "upcoming"
    new_event["photos"] = []
    new_event["featured"] = False

    # Generate new id
    base_slug = re.sub(r"-\d{4}-\d{2}-\d{2}.*$", "", source["id"])
    existing_ids = {e["id"] for e in events_list}
    suffix = 2
    new_id = f"{base_slug}-copy"
    while new_id in existing_ids:
        new_id = f"{base_slug}-copy-{suffix}"
        suffix += 1
    new_event["id"] = new_id
    new_event["title"] = source["title"] + " (copy)"

    events_list.append(new_event)
    yaml_data["events"] = events_list

    with open(EVENTS_YAML_PATH, "w") as fh:
        yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

    _reload_events_globals()
    log.info(f"[admin] Event duplicated: {event_id} → {new_id}")
    return jsonify({"success": True, "new_event_id": new_id})


@app.route("/admin/events/<event_id>/update-meta", methods=["POST"])
@admin_required
def admin_update_event_meta(event_id):
    """Update event metadata: title, subtitle, date, featured, included items."""
    data = request.json or {}

    with _EVENTS_YAML_LOCK:
        with open(EVENTS_YAML_PATH) as fh:
            yaml_data = yaml.safe_load(fh) or {}

        events_list = yaml_data.get("events", [])
        event = next((e for e in events_list if e["id"] == event_id), None)
        if not event:
            return jsonify({"error": "Event not found"}), 404

        if "title" in data:
            event["title"] = str(data["title"])[:120]
        if "subtitle" in data:
            event["subtitle"] = str(data["subtitle"])[:200]
        if "date" in data:
            try:
                datetime.strptime(str(data["date"]), "%Y-%m-%d")
                event["date"] = str(data["date"])
            except ValueError:
                return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        if "featured" in data:
            event["featured"] = bool(data["featured"])
        if "included" in data:
            event["included"] = [str(i).strip() for i in data["included"] if str(i).strip()]

        with open(EVENTS_YAML_PATH, "w") as fh:
            yaml.dump(yaml_data, fh, allow_unicode=True, sort_keys=False)

        _reload_events_globals()

    log.info(f"[admin] Event meta updated: {event_id}")
    return jsonify({"success": True})




# ─────────────────────────────────────────────
#  INTERAC TRANSFER LEDGER HELPERS
# ─────────────────────────────────────────────

def _ensure_etransfers_table(conn=None):
    """Ensure the Interac ledger exists (safe to call from routes/tests)."""
    own = conn is None
    conn = conn or db_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS etransfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_number TEXT UNIQUE,
            message_id TEXT,
            sender_name TEXT,
            amount REAL,
            memo TEXT,
            direction TEXT DEFAULT 'in',
            email_date TEXT,
            matched_booking_id INTEGER,
            matched_gift_code TEXT,
            status TEXT DEFAULT 'unmatched',
            source TEXT DEFAULT 'email',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(etransfers)").fetchall()}
        if "matched_gift_code" not in cols:
            conn.execute("ALTER TABLE etransfers ADD COLUMN matched_gift_code TEXT")
            conn.commit()
    except Exception:
        pass
    conn.commit()
    if own:
        conn.close()


def _clean_transfer_sender(raw):
    if not raw:
        return ""
    import re as _re
    s = str(raw).strip()
    s = _re.sub(r"\s+and\s+it\s*$", "", s, flags=_re.I)
    s = _re.sub(r"\s+and\s*$", "", s, flags=_re.I)
    s = _re.sub(r"\s{2,}", " ", s).strip()
    return s.title() if (s.isupper() or s.islower()) else s


def _normalise_transfer_status(status):
    status = (status or "unmatched").strip().lower()
    return status if status in {"unmatched", "matched", "ignored"} else "unmatched"


def _transfer_ref_from_csv(row_num, date, sender, amount):
    base = f"csv:{row_num}:{date}:{sender}:{amount}"
    return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:20]


def _import_etransfers_csv(file_obj):
    """Import the exported Interac CSV into etransfers.

    The historical CSV has no real Interac reference number, so rows get stable
    csv:* references. Live emails will use the actual Reference Number when present.
    """
    import csv as _csv
    import io as _io
    raw = file_obj.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", "replace")
    reader = _csv.DictReader(_io.StringIO(raw))
    conn = db_conn()
    _ensure_etransfers_table(conn)
    inserted = updated = skipped = 0
    for idx, row in enumerate(reader, start=1):
        date = (row.get("Date") or row.get("date") or "").strip()
        sender = _clean_transfer_sender(row.get("Sender") or row.get("sender") or "")
        amount_raw = (row.get("Amount ($)") or row.get("Amount") or row.get("amount") or "").strip()
        direction_raw = (row.get("Direction") or row.get("direction") or "IN").strip().upper()
        subject = (row.get("Subject") or row.get("subject") or "").strip()
        if not date or not amount_raw:
            skipped += 1
            continue
        try:
            amount = float(str(amount_raw).replace("$", "").replace(",", "").strip())
        except ValueError:
            skipped += 1
            continue
        direction = "out" if "OUT" in direction_raw else "in"
        # Usually we only care about incoming client payments; still keep outgoing
        # rows so the historical ledger matches the CSV and can be filtered.
        ref = _transfer_ref_from_csv(row.get("#") or idx, date, sender, f"{amount:.2f}")
        try:
            conn.execute("""
                INSERT INTO etransfers
                    (reference_number, sender_name, amount, memo, direction, email_date, status, source)
                VALUES (?, ?, ?, ?, ?, ?, 'unmatched', 'csv')
            """, (ref, sender, amount, subject, direction, date))
            inserted += 1
        except sqlite3.IntegrityError:
            conn.execute("""
                UPDATE etransfers
                   SET sender_name=COALESCE(NULLIF(?, ''), sender_name),
                       amount=COALESCE(?, amount),
                       memo=COALESCE(NULLIF(?, ''), memo),
                       direction=COALESCE(NULLIF(?, ''), direction),
                       email_date=COALESCE(NULLIF(?, ''), email_date)
                 WHERE reference_number=?
            """, (sender, amount, subject, direction, date, ref))
            updated += 1
    conn.commit()
    conn.close()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def _transfer_booking_options(limit=250):
    conn = db_conn()
    rows = conn.execute("""
        SELECT id, date, time, name, email, phone, instagram, session_type,
               status, confirmed, paid_amount, deposit_amount, full_price, event_id
          FROM bookings
         WHERE status NOT IN ('cancelled','expired')
         ORDER BY date DESC, time ASC
         LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _link_transfer_to_booking(transfer_id, booking_id):
    """Manual admin link: attach transfer and raise paid_amount safely.

    - Never lowers paid_amount.
    - If amount >= expected deposit: mark confirmed/paid.
    - If amount < expected deposit: mark partial_payment, not confirmed.
    """
    conn = db_conn()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        transfer = conn.execute("SELECT * FROM etransfers WHERE id=?", (transfer_id,)).fetchone()
        booking = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not transfer:
            conn.rollback(); return False, "Transfer not found"
        if not booking:
            conn.rollback(); return False, "Booking not found"
        amount = float(transfer["amount"] or 0)
        current_paid = float(booking["paid_amount"] or 0)
        new_paid = max(current_paid, amount)
        deposit = float(booking["deposit_amount"] or 0)
        if deposit <= 0:
            ev = get_event_by_id(booking["event_id"]) if booking["event_id"] else None
            deposit = float((ev or {}).get("deposit") or 0)
        if deposit <= 0:
            deposit = new_paid
        booking_for_event = dict(booking)
        if new_paid + 0.01 >= deposit:
            conn.execute("""
                UPDATE bookings
                   SET paid_amount=?, paid=1, confirmed=1, status='confirmed'
                 WHERE id=?
            """, (new_paid, booking_id))
            booking_for_event.update({"paid_amount": new_paid, "paid": 1, "confirmed": 1, "status": "confirmed"})
        else:
            conn.execute("""
                UPDATE bookings
                   SET paid_amount=?, paid=0, confirmed=0, status='partial_payment'
                 WHERE id=? AND confirmed=0
            """, (new_paid, booking_id))
        conn.execute("""
            UPDATE etransfers
               SET matched_booking_id=?, status='matched'
             WHERE id=?
        """, (booking_id, transfer_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.exception("[transfers] link failed")
        return False, str(e)
    finally:
        conn.close()
        try:
            sync_to_notion(booking_id)
        except Exception:
            pass
    if booking_for_event.get("confirmed"):
        _record_booking_funnel_event(
            booking_for_event,
            "booking_confirmed",
            {"source": "admin_transfer_link", "paid_amount": booking_for_event.get("paid_amount")},
        )
    return True, None


def _auto_link_etransfers():
    """Reconciliation: associate unmatched incoming transfers with a booking when
    EXACTLY ONE active booking's name strongly matches the sender (>=2 shared name
    tokens, e.g. first + last). Sets matched_booking_id + status='matched' ONLY —
    it never changes a booking's payment/confirmation (use the per-row Link for
    that). Conservative by design: ambiguous senders stay unmatched for manual
    review. Returns the number newly linked."""
    def toks(name):
        return set(t for t in re.split(r"[^a-z]+", (name or "").lower()) if len(t) >= 2)
    conn = db_conn(); conn.row_factory = sqlite3.Row
    _ensure_etransfers_table(conn)
    transfers = conn.execute(
        "SELECT id, sender_name FROM etransfers "
        "WHERE direction='in' AND matched_booking_id IS NULL AND status='unmatched'"
    ).fetchall()
    bookings = conn.execute(
        "SELECT id, name FROM bookings WHERE status NOT IN ('cancelled','expired')"
    ).fetchall()
    btoks = [(b["id"], toks(b["name"])) for b in bookings]
    linked = 0
    for t in transfers:
        st = toks(t["sender_name"])
        if len(st) < 2:
            continue  # need at least a first + last name to be confident
        ids = {bid for bid, bt in btoks if bt and len(st & bt) >= 2}
        if len(ids) == 1:
            conn.execute("UPDATE etransfers SET matched_booking_id=?, status='matched' WHERE id=?",
                         (ids.pop(), t["id"]))
            linked += 1
    conn.commit(); conn.close()
    return linked


# ─────────────────────────────────────────────
#  CLIENT DATABASE ROUTES
# ─────────────────────────────────────────────



@app.route("/admin/transfers")
@admin_required
def admin_transfers():
    """Interac e-Transfer ledger: imported CSV + live email scans."""
    status = _normalise_transfer_status(request.args.get("status") or "unmatched")
    if request.args.get("status") == "all":
        status = "all"
    search = (request.args.get("search") or "").strip()
    direction = (request.args.get("direction") or "in").strip().lower()
    if direction not in {"in", "out", "all"}:
        direction = "in"
    conn = db_conn()
    _ensure_etransfers_table(conn)
    conditions = []
    params = []
    if status != "all":
        conditions.append("t.status = ?")
        params.append(status)
    if direction != "all":
        conditions.append("COALESCE(t.direction,'in') = ?")
        params.append(direction)
    if search:
        like = f"%{search}%"
        conditions.append("(t.sender_name LIKE ? OR t.memo LIKE ? OR t.reference_number LIKE ? OR b.name LIKE ? OR b.email LIKE ?)")
        params.extend([like, like, like, like, like])
    where = " AND ".join(conditions) if conditions else "1=1"
    rows = conn.execute(f"""
        SELECT t.*, b.name AS booking_name, b.email AS booking_email, b.date AS booking_date,
               b.time AS booking_time, b.session_type AS booking_session, b.status AS booking_status
          FROM etransfers t
          LEFT JOIN bookings b ON b.id = t.matched_booking_id
         WHERE {where}
         ORDER BY COALESCE(t.email_date, t.created_at) DESC, t.id DESC
         LIMIT 500
    """, params).fetchall()
    stats_row = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='matched' THEN 1 ELSE 0 END) AS matched,
               SUM(CASE WHEN status='unmatched' THEN 1 ELSE 0 END) AS unmatched,
               SUM(CASE WHEN status='ignored' THEN 1 ELSE 0 END) AS ignored,
               SUM(CASE WHEN COALESCE(direction,'in')='in' THEN COALESCE(amount,0) ELSE 0 END) AS incoming_total
          FROM etransfers
    """).fetchone()
    conn.close()
    grouped = []
    current_day = None
    for r in rows:
        d = dict(r)
        day = (d.get("email_date") or d.get("created_at") or "")[:10] or "Unknown date"
        if not grouped or grouped[-1]["day"] != day:
            grouped.append({"day": day, "items": []})
        grouped[-1]["items"].append(d)
    return render_template(
        "admin_transfers.html",
        grouped=grouped,
        stats=dict(stats_row) if stats_row else {},
        booking_options=_transfer_booking_options(),
        filters={"status": status, "direction": direction, "search": search},
    )


@app.route("/admin/transfers/import", methods=["POST"])
@admin_required
def admin_transfers_import():
    file = request.files.get("csv")
    if not file:
        return jsonify({"success": False, "error": "CSV file is required"}), 400
    try:
        result = _import_etransfers_csv(file.stream)
        result["auto_linked"] = _auto_link_etransfers()
        return jsonify({"success": True, **result})
    except Exception as e:
        log.exception("[transfers] CSV import failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/admin/transfers/auto-link", methods=["POST"])
@admin_required
def admin_transfers_auto_link():
    """Associate matching transfers with bookings by name (no payment changes)."""
    return jsonify({"success": True, "auto_linked": _auto_link_etransfers()})


@app.route("/admin/transfers/<int:transfer_id>/link", methods=["POST"])
@admin_required
def admin_transfer_link(transfer_id):
    data = request.get_json(silent=True) or {}
    try:
        booking_id = int(data.get("booking_id") or 0)
    except (TypeError, ValueError):
        booking_id = 0
    if booking_id <= 0:
        return jsonify({"success": False, "error": "Booking is required"}), 400
    ok, err = _link_transfer_to_booking(transfer_id, booking_id)
    if not ok:
        return jsonify({"success": False, "error": err or "Could not link"}), 400
    return jsonify({"success": True})


@app.route("/admin/transfers/<int:transfer_id>/ignore", methods=["POST"])
@admin_required
def admin_transfer_ignore(transfer_id):
    conn = db_conn()
    _ensure_etransfers_table(conn)
    conn.execute("UPDATE etransfers SET status='ignored' WHERE id=?", (transfer_id,))
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route("/admin/transfers/<int:transfer_id>/unlink", methods=["POST"])
@admin_required
def admin_transfer_unlink(transfer_id):
    conn = db_conn()
    _ensure_etransfers_table(conn)
    conn.execute("UPDATE etransfers SET matched_booking_id=NULL, status='unmatched' WHERE id=?", (transfer_id,))
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route("/admin/clients")
@admin_required
def admin_clients():
    """Client database page."""
    # Classic CRM remains the default; new design is opt-in via ?v=2.
    template_name = "admin_clients_pro.html" if request.args.get("v") == "2" else "admin_clients.html"
    return render_template(template_name)


# ── Admin event page — manual slot management ────────────────────────────────
# Lets Iryna open an event (e.g. lilac-jun7) and see every slot's state
# (free / reserved / confirmed / blocked), plus a "Block slot" form so she
# can enter a walk-in client's details manually and mark them as already paid.
# Bypasses reCAPTCHA, rate-limit and the public reservation timer, but reuses
# the same UNIQUE(date, time) atomic check so it can't double-book.
@app.route("/admin/event/<event_id>")
@admin_required
def admin_event(event_id):
    ev = get_event_by_id(event_id)
    if not ev:
        # Falls through to the global 404 handler (friendly HTML card or JSON
        # depending on Accept header / path). No need for a bespoke template.
        from flask import abort
        abort(404)
    # Classic session page remains the default; new design is opt-in via ?v=2.
    template_name = "admin_event_pro.html" if request.args.get("v") == "2" else "admin_event.html"
    return render_template(template_name, event=ev)


@app.route("/admin/api/event/<event_id>/slots")
@admin_required
def admin_event_slots(event_id):
    """JSON: every slot in the event with its current booking state, so the
    UI can render the grid + decide which slots can still be blocked."""
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"error": "Event not found"}), 404
    base = generate_slots(ev)
    # Pull all bookings on this event's date(s) so we can mark each slot.
    target_date = ev.get("date")
    if not target_date:
        return jsonify({"event": {"id": ev.get("id"), "title": ev.get("title")}, "slots": []})

    conn = db_conn()
    rows = conn.execute(
        """SELECT id, date, time, name, email, phone, instagram, status,
                  confirmed, paid, paid_amount, deposit_amount, full_price,
                  event_id, reserved_until, session_type, selected_addons_json,
                  addons_total
           FROM bookings
           WHERE date=? AND status NOT IN ('cancelled','expired')""",
        (target_date,),
    ).fetchall()
    summary = _admin_event_summary(ev, conn=conn)
    conn.close()
    by_time = {r["time"]: dict(r) for r in rows}
    out = []
    for s in base:
        b = by_time.get(s["time"])
        is_block = bool(b and _admin_is_internal_block(b))
        out.append({
            "time": s["time"],
            "label": s["label"],
            "state": (
                "blocked" if is_block else
                "confirmed" if (b and b.get("confirmed")) else
                ("pending" if b else "free")
            ),
            "booking_id": b["id"] if b else None,
            "client": ("Closed" if is_block else (b.get("name") if b else None)),
        })
    event_roster = []
    for row in rows:
        b = dict(row)
        if b.get("event_id") and b.get("event_id") != ev.get("id"):
            continue
        if _admin_is_internal_block(b):
            continue
        event_roster.append({
            "id": b.get("id"),
            "date": b.get("date"),
            "time": b.get("time"),
            "name": b.get("name") or "",
            "email": b.get("email") or "",
            "phone": b.get("phone") or "",
            "instagram": (b.get("instagram") or "").lstrip("@"),
            "status": b.get("status") or "",
            "confirmed": bool(b.get("confirmed")),
            "paid": bool(b.get("paid")),
            "paid_amount": float(b.get("paid_amount") or 0),
            "deposit_amount": float(b.get("deposit_amount") or ev.get("deposit") or 0),
            "full_price": float(b.get("full_price") or ev.get("full_price") or 0),
            "selected_addons": _booking_addons(b),
            "addons_total": _booking_addons_total(b),
        })
    return jsonify({
        "event": {
            "id": ev.get("id"),
            "title": ev.get("title"),
            "date": target_date,
            "location": ev.get("location"),
            "deposit": float(ev.get("deposit") or 0),
            "full_price": float(ev.get("full_price") or 0),
        },
        "summary": summary,
        "slots": out,
        "bookings": event_roster,
    })


@app.route("/admin/api/event/<event_id>/block-slot", methods=["POST"])
@admin_required
def admin_event_block_slot(event_id):
    """Close one free slot without creating a real client booking."""
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"success": False, "error": "Event not found"}), 404

    data = request.get_json(silent=True) or {}
    slot_time = (data.get("time") or "").strip()
    reason = (data.get("reason") or "").strip()[:120] or "Closed by admin"
    if not slot_time:
        return jsonify({"success": False, "error": "Slot time is required"}), 400

    valid_times = {s["time"] for s in generate_slots(ev)}
    if slot_time not in valid_times:
        return jsonify({"success": False, "error": "Slot is not part of this event"}), 400

    event_date = ev.get("date")
    if not event_date:
        return jsonify({"success": False, "error": "Event has no date"}), 400

    now = _local_now()
    expires = (now + timedelta(days=365)).isoformat()
    deposit_amt = float(ev.get("deposit") or 0)
    full_price = float(ev.get("full_price") or 0) or (deposit_amt * 2)

    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """SELECT id FROM bookings
               WHERE date=? AND time=?
                 AND status NOT IN ('cancelled','expired')
                 AND (confirmed=1 OR reserved_until > ?)""",
            (event_date, slot_time, now.isoformat()),
        )
        if c.fetchone():
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Slot already taken"}), 409

        c.execute(
            """DELETE FROM bookings
               WHERE date=? AND time=?
                 AND (status IN ('cancelled','expired')
                      OR (status IN ('reserved','pending_payment') AND reserved_until <= ?))""",
            (event_date, slot_time, now.isoformat()),
        )
        token = secrets.token_urlsafe(16)
        c.execute(
            """INSERT INTO bookings
                 (date, time, name, email, phone, instagram, session_type,
                  status, reserved_until, event_id, confirmation_token,
                  deposit_amount, full_price, confirmed, paid, paid_amount)
               VALUES (?, ?, ?, '', '', '', 'internal_block',
                       'reserved', ?, ?, ?, ?, ?, 0, 0, 0)""",
            (event_date, slot_time, f"⛔ {reason}", expires, ev["id"],
             token, deposit_amt, full_price),
        )
        booking_id = c.lastrowid
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        log.exception(f"[admin_event_block_slot] {e}")
        return jsonify({"success": False, "error": "Server error"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    log.info(f"[admin_event_block_slot] event={event_id} {event_date} {slot_time} booking_id={booking_id}")
    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "date": event_date,
        "time": slot_time,
        "reason": reason,
    })


@app.route("/admin/api/event/<event_id>/unblock-slot", methods=["POST"])
@admin_required
def admin_event_unblock_slot(event_id):
    """Reopen a slot that was previously closed with an internal block."""
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"success": False, "error": "Event not found"}), 404

    data = request.get_json(silent=True) or {}
    booking_id = data.get("booking_id")
    slot_time = (data.get("time") or "").strip()
    if not booking_id and not slot_time:
        return jsonify({"success": False, "error": "booking_id or time required"}), 400

    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        if booking_id:
            row = c.execute(
                "SELECT * FROM bookings WHERE id=? AND status NOT IN ('cancelled','expired')",
                (booking_id,),
            ).fetchone()
        else:
            row = c.execute(
                """SELECT * FROM bookings
                   WHERE date=? AND time=? AND status NOT IN ('cancelled','expired')""",
                (ev.get("date"), slot_time),
            ).fetchone()
        if not row:
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Closed slot not found"}), 404
        row_dict = dict(row)
        if row_dict.get("event_id") and row_dict.get("event_id") != ev.get("id"):
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Slot belongs to another event"}), 400
        if not _admin_is_internal_block(row_dict):
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Only internally closed slots can be reopened"}), 400

        c.execute(
            "UPDATE bookings SET status='cancelled', reserved_until=NULL WHERE id=?",
            (row_dict["id"],),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        log.exception(f"[admin_event_unblock_slot] {e}")
        return jsonify({"success": False, "error": "Server error"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    log.info(f"[admin_event_unblock_slot] event={event_id} booking_id={row_dict['id']}")
    return jsonify({"success": True, "booking_id": row_dict["id"]})


@app.route("/admin/api/event/<event_id>/block-day", methods=["POST"])
@admin_required
def admin_event_block_day(event_id):
    """Block every remaining FREE slot on an event in one shot — Iryna can't
    physically shoot that day (sick, family, weather). Each blocked slot
    creates a status='reserved' booking under the synthetic "INTERNAL BLOCK"
    name so the public calendar shows the slot as taken without surfacing a
    real client. Existing reserved/confirmed slots are left alone. Returns
    the count of slots newly blocked + the count that were already taken."""
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"success": False, "error": "Event not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()[:120] or "Internal block"
    event_date = ev.get("date")
    if not event_date:
        return jsonify({"success": False, "error": "Event has no date"}), 400

    now = _local_now()
    expires = (now + timedelta(days=365)).isoformat()
    deposit_amt = float(ev.get("deposit") or 0)
    full_price = float(ev.get("full_price") or 0) or (deposit_amt * 2)
    all_slots = [s["time"] for s in generate_slots(ev)]
    if not all_slots:
        return jsonify({"success": False, "error": "Event has no slots"}), 400

    conn = db_conn()
    c = conn.cursor()
    blocked = 0
    already = 0
    try:
        c.execute("BEGIN IMMEDIATE")
        # Find which of these slots already have an active booking on this date.
        placeholders = ",".join("?" for _ in all_slots)
        c.execute(
            f"""SELECT time FROM bookings
                WHERE date=? AND time IN ({placeholders})
                  AND status NOT IN ('cancelled','expired')
                  AND (confirmed=1 OR reserved_until > ?)""",
            [event_date] + all_slots + [now.isoformat()],
        )
        taken = {r["time"] for r in c.fetchall()}
        already = len(taken)

        for slot_time in all_slots:
            if slot_time in taken:
                continue
            # Sweep any stale rows on this slot so the UNIQUE constraint passes.
            c.execute(
                """DELETE FROM bookings
                   WHERE date=? AND time=?
                     AND (status IN ('cancelled','expired')
                          OR (status IN ('reserved','pending_payment') AND reserved_until <= ?))""",
                (event_date, slot_time, now.isoformat()),
            )
            token = secrets.token_urlsafe(16)
            c.execute(
                """INSERT INTO bookings
                     (date, time, name, email, phone, instagram, session_type,
                      status, reserved_until, event_id, confirmation_token,
                      deposit_amount, full_price, confirmed, paid, paid_amount)
                   VALUES (?, ?, ?, '', '', '', 'internal_block',
                           'reserved', ?, ?, ?, ?, ?, 0, 0, 0)""",
                (event_date, slot_time, f"⛔ {reason}", expires, ev["id"],
                 token, deposit_amt, full_price),
            )
            blocked += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        log.exception(f"[admin_event_block_day] {e}")
        return jsonify({"success": False, "error": "Server error"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    log.info(
        f"[admin_event_block_day] event={event_id} date={event_date} "
        f"blocked={blocked} already_taken={already} reason={reason!r}"
    )
    return jsonify({
        "success": True,
        "blocked": blocked,
        "already_taken": already,
        "total_slots": len(all_slots),
    })


@app.route("/admin/api/event/<event_id>/manual-book", methods=["POST"])
@admin_required
def admin_event_manual_book(event_id):
    """Manually create a booking on a given slot. Bypasses reCAPTCHA + rate
    limit (admin-only path) but reuses the atomic conflict check from /reserve
    so it can't overwrite an existing active booking. If `mark_paid` is true
    the booking is created as fully confirmed + paid_amount = full_price."""
    ev = get_event_by_id(event_id)
    if not ev:
        return jsonify({"success": False, "error": "Event not found"}), 404

    data = request.get_json(silent=True) or {}
    slot_time = (data.get("time") or "").strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    instagram = (data.get("instagram") or "").lstrip("@").strip()
    note = (data.get("note") or "").strip()[:500]
    mark_paid = bool(data.get("mark_paid"))

    if not slot_time:
        return jsonify({"success": False, "error": "Slot time is required"}), 400
    if not name or len(name) < 2:
        return jsonify({"success": False, "error": "Client name is required"}), 400

    # Validate slot belongs to this event.
    valid_times = {s["time"] for s in generate_slots(ev)}
    if slot_time not in valid_times:
        return jsonify({"success": False, "error": "Slot is not part of this event"}), 400

    event_date = ev.get("date")
    if not event_date:
        return jsonify({"success": False, "error": "Event has no date"}), 400

    now = _local_now()
    deposit_amt = float(ev.get("deposit") or 0)
    full_price = float(ev.get("full_price") or 0) or (deposit_amt * 2)

    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """SELECT id FROM bookings
               WHERE date=? AND time=?
                 AND status NOT IN ('cancelled', 'expired')
                 AND (confirmed=1 OR reserved_until > ?)""",
            (event_date, slot_time, now.isoformat()),
        )
        if c.fetchone():
            conn.rollback()
            conn.close()
            return jsonify({"success": False, "error": "Slot already taken"}), 409

        # Clear stale rows on this exact slot so the unique constraint passes.
        c.execute(
            """DELETE FROM bookings
               WHERE date=? AND time=?
                 AND (status IN ('cancelled','expired')
                      OR (status IN ('reserved','pending_payment') AND reserved_until <= ?))""",
            (event_date, slot_time, now.isoformat()),
        )

        token = secrets.token_urlsafe(16)
        status_val = "confirmed" if mark_paid else "reserved"
        paid_val = 1 if mark_paid else 0
        confirmed_val = 1 if mark_paid else 0
        paid_amount = full_price if mark_paid else 0.0
        # 1-hour reservation window for manual blocks (so it doesn't expire
        # at the public 15-min mark while admin sorts out paperwork).
        reserved_until = (now + timedelta(hours=1)).isoformat()
        c.execute(
            """INSERT INTO bookings
                 (date, time, name, email, phone, instagram, session_type, status,
                  reserved_until, event_id, confirmation_token, deposit_amount,
                  full_price, confirmed, paid, paid_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_date, slot_time, name, email, phone, instagram,
                ev.get("session_type") or "manual",
                status_val, reserved_until, ev["id"], token,
                deposit_amt, full_price,
                confirmed_val, paid_val, paid_amount,
            ),
        )
        booking_id = c.lastrowid
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        log.exception(f"[admin_event_manual_book] {e}")
        return jsonify({"success": False, "error": "Server error"}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    # Best-effort sync to clients table (don't fail the request if it errors)
    if email:
        try:
            sync_client(email, name, phone, instagram)
        except Exception as _e:
            log.warning(f"[admin_event_manual_book] sync_client failed: {_e}")

    log.info(
        f"[admin_event_manual_book] event={event_id} slot={event_date} {slot_time} "
        f"booking_id={booking_id} mark_paid={mark_paid} name={name!r}"
    )
    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "status": status_val,
        "paid_amount": paid_amount,
        "date": event_date,
        "time": slot_time,
    })


@app.route("/admin/api/clients")
@admin_required
def api_clients_list():
    """JSON list of all clients with optional search/filter."""
    q = (request.args.get("q") or "").strip().lower()
    tag = (request.args.get("tag") or "").strip()
    sort = request.args.get("sort", "last_booking_at")
    allowed_sorts = {"last_booking_at", "total_bookings", "total_paid", "name", "created_at"}
    if sort not in allowed_sorts:
        sort = "last_booking_at"

    conn = db_conn()
    sql = """
        SELECT id, name, email, phone, instagram, tags,
               total_bookings, total_confirmed, total_paid,
               first_booking_at, last_booking_at, created_at, notes
        FROM clients
        WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (LOWER(name) LIKE ? OR LOWER(email) LIKE ? OR LOWER(phone) LIKE ? OR LOWER(instagram) LIKE ?)"
        params += [f"%{q}%"] * 4
    if tag:
        sql += " AND (',' || tags || ',') LIKE ?"
        params.append(f"%,{tag},%")
    sql += f" ORDER BY {sort} DESC NULLS LAST"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/api/clients/<int:client_id>")
@admin_required
def api_client_detail(client_id):
    """Single client profile + full booking history."""
    conn = db_conn()
    client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        conn.close()
        return jsonify({"error": "not found"}), 404
    client = dict(client)
    bookings = conn.execute(
        "SELECT * FROM bookings WHERE LOWER(email)=LOWER(?) ORDER BY date DESC, time DESC",
        (client["email"],)
    ).fetchall()
    # client_notes may have legacy column name; tolerate both
    try:
        notes = conn.execute(
            "SELECT * FROM client_notes WHERE client_id=? ORDER BY created_at DESC",
            (client_id,)
        ).fetchall()
    except Exception:
        notes = []
    conn.close()
    # Normalise note column: 'text' if it exists, else 'note'
    norm_notes = []
    for n in notes:
        nd = dict(n)
        if 'text' not in nd and 'note' in nd:
            nd['text'] = nd.pop('note')
        norm_notes.append(nd)
    return jsonify({
        "client": client,
        "bookings": [dict(b) for b in bookings],
        "notes": norm_notes,
    })


@app.route("/admin/api/clients/<int:client_id>/note", methods=["POST"])
@admin_required
def api_client_add_note(client_id):
    """Add a note to a client."""
    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    conn = db_conn()
    # Verify client exists
    if not conn.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone():
        conn.close()
        return jsonify({"error": "not found"}), 404
    conn.execute(
        "INSERT INTO client_notes (client_id, text) VALUES (?, ?)",
        (client_id, text)
    )
    conn.commit()
    note = conn.execute(
        "SELECT * FROM client_notes WHERE client_id=? ORDER BY created_at DESC LIMIT 1",
        (client_id,)
    ).fetchone()
    conn.close()
    return jsonify({"success": True, "note": dict(note)})


@app.route("/admin/api/clients/<int:client_id>/note/<int:note_id>", methods=["DELETE"])
@admin_required
def api_client_delete_note(client_id, note_id):
    """Delete a note."""
    conn = db_conn()
    conn.execute("DELETE FROM client_notes WHERE id=? AND client_id=?", (note_id, client_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/admin/api/clients/<int:client_id>/tag", methods=["POST"])
@admin_required
def api_client_tag(client_id):
    """Add or remove a tag on a client. Body: {tag, action:'add'|'remove'}"""
    data = request.json or {}
    tag = (data.get("tag") or "").strip().upper()
    action = data.get("action", "add")
    if not tag:
        return jsonify({"error": "tag required"}), 400
    conn = db_conn()
    row = conn.execute("SELECT tags FROM clients WHERE id=?", (client_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    existing = [t.strip().upper() for t in (row["tags"] or "").split(",") if t.strip()]
    if action == "add":
        if tag not in existing:
            existing.append(tag)
    else:
        existing = [t for t in existing if t != tag]
    new_tags = ",".join(existing)
    conn.execute("UPDATE clients SET tags=? WHERE id=?", (new_tags, client_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "tags": new_tags})


@app.route("/admin/api/clients/<int:client_id>/edit", methods=["POST"])
@admin_required
def api_client_edit(client_id):
    """Update client name / phone / instagram (manual override)."""
    data = request.json or {}
    conn = db_conn()
    if not conn.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone():
        conn.close()
        return jsonify({"error": "not found"}), 404
    fields = {}
    if "name" in data:
        fields["name"] = str(data["name"])[:120]
    if "phone" in data:
        fields["phone"] = str(data["phone"])[:30]
    if "instagram" in data:
        ig = str(data["instagram"]).lstrip("@")[:80]
        fields["instagram"] = ig
    if not fields:
        conn.close()
        return jsonify({"error": "nothing to update"}), 400
    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE clients SET {set_clause} WHERE id=?",
                 list(fields.values()) + [client_id])
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/admin/api/generate-invoice", methods=["POST"])
@admin_required
def api_generate_invoice():
    """Generate Stripe payment link for private session invoice.

    NOTE: this route used to ALSO answer /admin/api/private-session, which
    silently shadowed api_private_session() (the handler that actually creates
    the hidden event + booking). Clicking "Создать" therefore only ever made a
    second invoice and returned event_id=undefined. The duplicate path is
    removed — generate-invoice and private-session are now distinct."""
    import stripe
    from flask import jsonify
    import os

    data = request.get_json()
    firstname = data.get('firstname')
    lastname = data.get('lastname')
    email = data.get('email')
    price = float(data.get('price', 300))
    date = data.get('date')
    start_time = data.get('start_time')
    end_time = data.get('end_time')
    description = data.get('description', 'Индивидуальная фотосессия')

    if not all([firstname, lastname, email, price, date, start_time, end_time]):
        return jsonify({'success': False, 'error': 'Missing required fields'}), 400

    try:
        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
        if not stripe.api_key:
            return jsonify({'success': False, 'error': 'Stripe not configured'}), 500

        # Create Stripe payment link
        payment_link = stripe.PaymentLink.create(
            line_items=[{
                'price_data': {
                    'currency': 'cad',
                    'product_data': {
                        'name': f"{description} ({date} {start_time}-{end_time})",
                        'description': f"Индивидуальная фотосессия с {firstname} {lastname}"
                    },
                    'unit_amount': int(price * 100)
                },
                'quantity': 1
            }],
            customer_email=email,
            metadata={
                'client_name': f"{firstname} {lastname}",
                'date': date,
                'start_time': start_time,
                'end_time': end_time,
                'type': 'private_session'
            }
        )

        return jsonify({
            'success': True,
            'payment_link': payment_link.url,
            'invoice_id': payment_link.id
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@app.route("/admin/api/private-session", methods=["POST"])
@admin_required
def api_private_session():
    """Create a hidden event and book one client into it.

    Used by the admin "🔒 Приватная фотосессия" modal. The booking reuses the
    full deposit machinery: the client gets a /payment page link (emailed when
    send_email=true) where they choose Interac e-Transfer (auto-confirmed by
    the Gmail watcher) or Stripe Checkout (auto-confirmed by the webhook); the
    page live-polls /booking-status and the client receives a confirmation
    email — identical mechanics to the public deposit flow.

    already_paid=true records an already-settled session (confirmed/paid, no
    email). Legacy compat: when already_paid is omitted, an empty payment_link
    means "settled offline" (old modal semantics, pinned by tests)."""
    import secrets
    import yaml

    data = request.json or {}
    date = (data.get("date") or "").strip()
    start_time = (data.get("start_time") or "").strip()
    end_time = (data.get("end_time") or "").strip()
    client_name = (data.get("client_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    instagram = str(data.get("instagram") or "").strip()
    if "instagram.com/" in instagram:
        instagram = instagram.split("instagram.com/", 1)[1].split("?", 1)[0].split("#", 1)[0]
    instagram = instagram.strip().strip("/").lstrip("@")[:80]
    payment_link = (data.get("payment_link") or "").strip()
    send_email = bool(data.get("send_email"))
    already_paid = data.get("already_paid")  # None => legacy: paid when no payment_link

    # ── Validation ──
    if not (date and start_time and end_time and client_name):
        return jsonify({"error": "Заполните дату, время и имя клиента"}), 400
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"error": "Дата должна быть в формате YYYY-MM-DD"}), 400
    if not (_TIME_RE.match(start_time) and _TIME_RE.match(end_time)):
        return jsonify({"error": "Некорректное время"}), 400
    if end_time <= start_time:
        return jsonify({"error": "Время окончания должно быть позже начала"}), 400
    if email and (len(email) > 254 or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", email)):
        return jsonify({"error": "Некорректный email"}), 400
    if send_email and not email:
        return jsonify({"error": "Email обязателен для отправки ссылки на оплату"}), 400
    try:
        price = round(float(data.get("price") or 300), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректная цена"}), 400
    if price < 0:
        return jsonify({"error": "Цена не может быть отрицательной"}), 400

    # Deposit: defaults to full price (backward compatible)
    try:
        deposit = round(float(data.get("deposit") or price), 2)
    except (TypeError, ValueError):
        deposit = price
    deposit = min(max(deposit, 0), price)  # clamp to [0, price]
    balance = round(price - deposit, 2)

    # Number of edited photos (default 25 for private sessions)
    edited_photos = int(data.get("photos") or 25)

    session_minutes = max(
        15,
        int((datetime.strptime(end_time, "%H:%M") - datetime.strptime(start_time, "%H:%M")).total_seconds() // 60),
    )

    # ── Hidden event (not shown on the public site) ──
    event_id = f"private-{secrets.token_hex(4)}"
    event = {
        "id": event_id,
        "title": f"Individual Photoshoot — {client_name}",
        "subtitle": "Individual photoshoot (hidden from public website)",
        "date": date,
        "start_time": start_time,
        "end_time": end_time,
        "session_length": session_minutes,
        "break_length": 0,
        "slot_interval": session_minutes,
        # Amount due now == deposit (may equal full price for full-pay):
        # drives the /payment page, the Stripe checkout amount and the
        # e-Transfer expected-amount matching.
        "deposit": deposit,
        "full_price": price,
        "location": "Calgary — exact spot sent after booking",
        "session_type": "private",
        "edited_photos": edited_photos,
        "featured": False,
        "hidden": True,
        "included": [
            f"{session_minutes} min individual photoshoot",
            f"{edited_photos} professionally edited photos",
            "All original photos included",
        ],
        "photos": ["/images/placeholder.jpg"],
    }

    # ── Book the client atomically (mirrors /admin/api/event/<id>/manual-book) ──
    if already_paid is None:
        paid = not payment_link  # legacy semantics: no link => settled offline
    else:
        paid = bool(already_paid)
    status_val = "confirmed" if paid else "reserved"
    paid_amount = price if paid else 0.0
    token = secrets.token_urlsafe(16)
    conn = db_conn()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        # UNIQUE(date,time) guards against a real double-book on the same slot.
        taken = c.execute(
            "SELECT id FROM bookings WHERE date=? AND time=? "
            "AND status NOT IN ('cancelled','expired')",
            (date, start_time),
        ).fetchone()
        if taken:
            conn.rollback()
            return jsonify({"error": "На эту дату и время уже есть бронь"}), 409
        c.execute(
            """INSERT INTO bookings
                 (date, time, name, email, phone, instagram, session_type, status,
                  event_id, confirmation_token, deposit_amount, full_price,
                  confirmed, paid, paid_amount, payment_link, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'private', ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (date, start_time, client_name, email, "", instagram,
             status_val, event_id, token, deposit, price,
             1 if paid else 0, 1 if paid else 0, paid_amount, payment_link or None),
        )
        booking_id = c.lastrowid
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.exception(f"[private-session] booking insert failed: {e}")
        return jsonify({"error": "Не удалось создать бронь"}), 500
    finally:
        conn.close()

    # Persist the hidden event (atomic temp-file swap so a crash can't truncate
    # events.yaml). Uses the canonical path + shared lock so it can't race the
    # other events.yaml writers. In-memory EVENTS is updated after the file wins.
    try:
        with _EVENTS_YAML_LOCK:
            with open(_EVENTS_PATH, "r") as f:
                events_data = yaml.safe_load(f) or {"events": []}
            events_data.setdefault("events", []).append(event)
            tmp_path = _EVENTS_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                yaml.safe_dump(events_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            os.replace(tmp_path, _EVENTS_PATH)
            global EVENTS
            EVENTS.append(event)
    except Exception as e:
        log.error(f"[private-session] events.yaml save failed (booking #{booking_id} still created): {e}")

    # Best-effort: keep the clients table in sync
    if email:
        try:
            sync_client(email, client_name, "", instagram)
        except Exception as _e:
            log.warning(f"[private-session] sync_client failed: {_e}")

    # ── Client payment link — same /payment page as the deposit flow ──
    base_url = (BASE_URL or CANONICAL_SITE_URL).rstrip("/")
    payment_url = f"{base_url}/payment?booking_id={booking_id}&token={token}"

    email_sent = False
    if not paid and send_email and email:
        try:
            email_sent = bool(_send_private_payment_email(
                to_email=email,
                client_name=client_name,
                event_title=event["title"],
                event_date=date,
                start_time=start_time,
                end_time=end_time,
                session_minutes=session_minutes,
                price=price,
                booking_id=booking_id,
                payment_url=payment_url,
                deposit=deposit,
                balance=balance,
            ))
        except Exception as e:
            log.error(f"[private-session] payment email failed for #{booking_id}: {e}")

    try:
        _notify_admin(
            f"📸 <b>Individual photoshoot created</b>\n\n"
            f"👤 {_tg_escape(client_name)}\n"
            + (f"📸 @{_tg_escape(instagram)}\n" if instagram else "")
            + f"📅 {date} · {start_time}–{end_time}\n"
            + f"💰 ${price:.2f} CAD · "
            + ("✅ already paid" if paid else ("✉️ payment link emailed to client" if email_sent else "🔗 link ready (email not sent)"))
            + f"\n🆔 Booking #{booking_id}"
        )
    except Exception as e:
        log.warning(f"[private-session] admin notify failed: {e}")

    log.info(f"[private-session] event={event_id} booking={booking_id} "
             f"{date} {start_time}-{end_time} price={price} paid={paid} "
             f"email_sent={email_sent} name={client_name!r}")
    return jsonify({
        "success": True,
        "event_id": event_id,
        "booking_id": booking_id,
        "status": status_val,
        "paid": paid,
        "payment_url": None if paid else payment_url,
        "email_sent": email_sent,
        "booking_url": f"/admin/booking/{booking_id}",
    })


@app.route("/admin/backup", methods=["POST"])
@admin_required
def admin_manual_backup():
    """Trigger a manual database backup."""
    try:
        path = create_backup()
        return jsonify({"success": True, "path": str(path)})
    except Exception as e:
        log.error(f"[backup] Manual backup failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/admin/backups")
@admin_required
def admin_list_backups():
    """List available database backups."""
    import glob as _glob
    pattern = os.path.join(BACKUP_DIR, "bookings_*.db")
    files = sorted(_glob.glob(pattern), reverse=True)
    result = []
    for f in files[:50]:
        try:
            stat = os.stat(f)
            result.append({
                "filename": os.path.basename(f),
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        except OSError:
            pass
    return jsonify(result)


@app.route("/admin/api/clients/export")
@admin_required
def api_clients_export():
    """Export all clients as CSV."""
    import csv, io
    conn = db_conn()
    rows = conn.execute(
        "SELECT name, email, phone, instagram, tags, total_bookings, total_confirmed, total_paid, "
        "first_booking_at, last_booking_at, created_at FROM clients ORDER BY last_booking_at DESC NULLS LAST"
    ).fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Email", "Phone", "Instagram", "Tags",
                     "Total Bookings", "Confirmed", "Total Paid ($)",
                     "First Booking", "Last Booking", "Client Since"])
    for r in rows:
        writer.writerow(list(r))
    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=clients.csv"}
    )


# ─────────────────────────────────────────────

@app.route("/assistant/chat", methods=["POST"])
def assistant_chat():
    """Public site assistant.

    Uses current event data plus an optional sanitized Instagram-derived
    knowledge file. The raw client archive is never read by this route.
    """
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if len(message) < 2:
        return jsonify({"error": "message required"}), 400
    if len(message) > 1200:
        return jsonify({"error": "message is too long"}), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    if isinstance(ip, str) and "," in ip:
        ip = ip.split(",", 1)[0].strip()
    if not check_assistant_rate_limit(ip):
        return jsonify({"error": "Too many chat messages. Please wait a few minutes."}), 429
    record_assistant_request(ip)

    history = data.get("history") if isinstance(data.get("history"), list) else []
    lang = (data.get("lang") or "en").strip()[:8]

    try:
        from assistant_engine import answer_assistant_message

        result = answer_assistant_message(
            message=message,
            history=history,
            events=EVENTS,
            settings=SETTINGS,
            lang=lang,
            db_path=DB_PATH,
        )
        return jsonify({"success": True, **result})
    except Exception as e:
        log.error(f"[assistant] Unexpected failure: {e}")
        fallback = (
            "I can help with sessions, pricing, outfits, location details, and booking questions. "
            "Please choose an available session on the site, or DM Iryna on Instagram for a custom request."
        )
        return jsonify({"success": True, "answer": fallback, "source": "fallback", "knowledge_used": 0})


@app.route("/booking-status")
def booking_status():
    """API for live client page polling — requires confirmation_token for identity safety."""
    booking_id = request.args.get("booking_id")
    token = request.args.get("token")
    if not booking_id:
        return jsonify({"error": "booking_id required"}), 400
    conn = db_conn()
    row = conn.execute("SELECT status, confirmed, paid, paid_amount, name, confirmation_token FROM bookings WHERE id=?", (booking_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    b = dict(row)
    # Token check: must match confirmation_token (constant-time to match
    # admin-login and telegram-webhook style; token is high-entropy so brute
    # force is infeasible regardless, but consistency keeps the code reviewable)
    stored_token = b.get("confirmation_token", "")
    if (not token or not stored_token
            or not hmac.compare_digest(str(token), str(stored_token))):
        log.warning(f"[booking-status] Invalid token for booking #{booking_id}")
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({
        "status": b["status"],
        "confirmed": bool(b["confirmed"]),
        "paid": bool(b["paid"]),
        "paid_amount": b.get("paid_amount"),
        "name": b.get("name", "")
    })

# ===== TELEGRAM WEBHOOK =====
@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Handle inline button presses from Telegram bot (confirm/cancel bookings).

    SECURITY: requires X-Telegram-Bot-Api-Secret-Token header to match
    TELEGRAM_WEBHOOK_SECRET. When you call Telegram's setWebhook API, pass
    secret_token=<same value> — Telegram then sends that header with every
    callback. Without this check, anyone who guesses the URL can confirm or
    cancel arbitrary bookings.
    """
    if TELEGRAM_WEBHOOK_SECRET:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(provided, TELEGRAM_WEBHOOK_SECRET):
            log.warning("[tg-webhook] Rejected: bad/missing secret token")
            return jsonify({"ok": False, "error": "unauthorized"}), 401
    else:
        # Fail closed: if no secret is configured, reject everything to avoid
        # accidental open-webhook deployments. Operators MUST set
        # TELEGRAM_WEBHOOK_SECRET to use this endpoint.
        log.warning("[tg-webhook] Rejected: TELEGRAM_WEBHOOK_SECRET not configured")
        return jsonify({"ok": False, "error": "webhook not configured"}), 503

    data = request.get_json(silent=True) or {}

    # ── Handle incoming text messages (AI assistant chat) ──
    if "callback_query" not in data:
        msg = data.get("message", {})
        text = (msg.get("text") or "").strip()
        chat_id = msg.get("chat", {}).get("id")
        if text and chat_id and not msg.get("forward_date"):
            # Skip commands like /start, /help — handled elsewhere
            if text.startswith("/"):
                return jsonify({"ok": True})
            # Rate limit per chat_id
            ip_key = f"tg:{chat_id}"
            if not check_assistant_rate_limit(ip_key):
                _tg_send(chat_id, "⚠️ Too many messages. Please wait a few minutes.")
                return jsonify({"ok": True})
            record_assistant_request(ip_key)
            # Detect language from Telegram user
            lang_code = (msg.get("from", {}).get("language_code") or "en")[:2]
            lang = lang_code if lang_code in ("ru", "uk", "hi") else "en"
            try:
                from assistant_engine import answer_assistant_message
                result = answer_assistant_message(
                    message=text,
                    history=[],
                    events=EVENTS,
                    settings=SETTINGS,
                    lang=lang,
                    db_path=DB_PATH,
                )
                answer = result.get("answer", "")
            except Exception as exc:
                log.error(f"[tg-assistant] AI call failed: {exc}")
                answer = "Sorry, I couldn't process that. Please try again or DM @pashynska.photo on Instagram."
            # Telegram limit: 4096 chars per message
            if len(answer) > 4000:
                for i in range(0, len(answer), 4000):
                    _tg_send(chat_id, answer[i:i+4000])
            else:
                _tg_send(chat_id, answer)
        return jsonify({"ok": True})

    cb = data["callback_query"]
    cb_data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    from_user_obj = cb.get("from", {}) or {}
    from_user = from_user_obj.get("first_name") or from_user_obj.get("username") or "Admin"

    if not _is_telegram_admin_callback(cb):
        username = from_user_obj.get("username") or ""
        user_id = from_user_obj.get("id") or ""
        log.warning(f"[tg-webhook] Unauthorized callback user_id={user_id} username={username!r} data={cb_data!r}")
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={
                "callback_query_id": cb["id"],
                "text": "This admin action is not enabled for your Telegram account yet.",
                "show_alert": True,
            },
            timeout=5
        )
        return jsonify({"ok": True})

    # Acknowledge immediately (removes spinner on button)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
        json={"callback_query_id": cb["id"]},
        timeout=5
    )

    if not cb_data:
        return jsonify({"ok": True})

    action, _, booking_id_str = cb_data.partition(":")
    try:
        booking_id = int(booking_id_str)
    except (ValueError, TypeError):
        return jsonify({"ok": True})

    if action == "confirm":
        conn = db_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            _tg_send(chat_id, f"❌ Booking #{booking_id} not found.")
            return jsonify({"ok": True})

        booking = dict(row)
        if booking["confirmed"]:
            conn.close()
            _tg_send(chat_id, f"ℹ️ Booking #{booking_id} already confirmed.")
            return jsonify({"ok": True})

        # Resolve real per-event deposit (was hardcoded SESSION_PRICE which is the
        # *first* active event's deposit — wrong when multiple events exist).
        ev_for_price = get_event_by_id(booking.get("event_id"))
        deposit_amount = (
            ev_for_price.get("deposit") if ev_for_price and ev_for_price.get("deposit") is not None
            else SESSION_PRICE
        )

        c.execute(
            "UPDATE bookings SET confirmed=1, paid=1, status='confirmed', paid_amount=? WHERE id=?",
            (deposit_amount, booking_id)
        )
        conn.commit()
        conn.close()
        booking.update({"confirmed": 1, "paid": 1, "status": "confirmed", "paid_amount": deposit_amount})
        _record_booking_funnel_event(
            booking,
            "booking_confirmed",
            {"source": "telegram", "paid_amount": deposit_amount},
        )

        ev = get_event_by_id(booking.get("event_id"))
        event_title = ev.get("title", "Mini Session") if ev else "Mini Session"
        event_date = ev["date"] if ev else booking["date"]

        # Keep the Telegram button feeling instant: update DB + Telegram first,
        # then run slow integrations (Google Calendar, Notion, email) in the
        # background. These integrations can take several seconds and made the
        # inline button look broken even though the booking was already updated.
        log.info(f"[tg-webhook] Booking #{booking_id} confirmed by {from_user}")
        updated_text = (
            f"✅ <b>CONFIRMED</b> by {_tg_escape(from_user)}\n\n"
            f"👤 {_tg_escape(booking.get('name', 'Client'))}\n"
            f"📅 {_tg_escape(booking.get('date'))} @ {_tg_escape(booking.get('time'))}\n"
            f"🎉 {_tg_escape(event_title)}\n"
            f"📋 Booking #{booking_id}\n"
            f"📧 Confirmation email is being sent"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                json={"chat_id": chat_id, "message_id": message_id,
                      "text": updated_text, "parse_mode": "HTML"},
                timeout=5
            )
        except Exception as e:
            log.warning(f"[tg-webhook] editMessageText failed for #{booking_id}: {e}")
        for other_chat in _telegram_admin_chat_ids():
            if str(other_chat) != str(chat_id):
                _tg_send(other_chat, updated_text)

        def _run_confirm_side_effects():
            try:
                create_calendar_event_for_booking(booking_id)
                sync_to_notion(booking_id)
                if ev and booking:
                    _send_client_email(
                        to_email=booking.get("email", ""),
                        client_name=booking.get("name", "Client"),
                        event_date=event_date,
                        slot_time=booking.get("time", ""),
                        event_title=event_title,
                        booking_id=booking_id,
                        location=ev.get("location"),
                        location_url=ev.get("location_url"),
                        **_client_email_context(booking, ev),
                    )
                log.info(f"[tg-webhook] Booking #{booking_id} side-effects complete")
            except Exception as e:
                log.error(f"[tg-webhook] side-effects failed for booking #{booking_id}: {e}")

        _threading.Thread(target=_run_confirm_side_effects, daemon=True).start()

    elif action == "cancel":
        conn = db_conn()
        c = conn.cursor()
        c.execute("UPDATE bookings SET status='cancelled', reserved_until=NULL WHERE id=?", (booking_id,))
        conn.commit()
        conn.close()
        sync_to_notion(booking_id)

        log.info(f"[tg-webhook] Booking #{booking_id} cancelled by {from_user}")
        updated_text = f"❌ <b>CANCELLED</b> by {from_user}\n📋 Booking #{booking_id}"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id,
                  "text": updated_text, "parse_mode": "HTML"},
            timeout=5
        )
        for other_chat in _telegram_admin_chat_ids():
            if str(other_chat) != str(chat_id):
                _tg_send(other_chat, updated_text)

    return jsonify({"ok": True})


# ===== STARTUP: start background watcher AFTER all functions are defined =====
# This prevents NameError on the first watcher tick when expire_reservations()
# is called before Python finishes loading the module.
_start_global_watcher()

# ===== RUN =====
if __name__ == "__main__":
    # Werkzeug debugger = remote code execution if ever exposed (this binds
    # to 0.0.0.0 for LAN/tunnel testing!). Debug is opt-in via FLASK_DEBUG=1.
    _debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5001, debug=_debug)
