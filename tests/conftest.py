import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Hermetic test environment ─────────────────────────────────────────────────
# app.py loads .env at import time. On the dev machine .env contains LIVE
# credentials (Stripe rk_live, Telegram bot token), so without this guard the
# test suite would hit real external APIs — e.g. create live-mode Stripe
# Checkout sessions from /admin/confirm. Setting these BEFORE the first
# `import app` wins over load_dotenv(), which never overrides existing vars.
# Tests that exercise Stripe code paths monkeypatch STRIPE_SECRET_KEY="sk_test"
# themselves (see test_admin_event_organizer.py).
os.environ.setdefault("FLASK_ENV", "development")
for _var in (
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "N8N_WEBHOOK_URL",
    "RECAPTCHA_SECRET_KEY",
    "NOTION_API_KEY",
    "GCAL_HELPER",
    "GOOGLE_CALENDAR_REFRESH_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
):
    os.environ[_var] = ""


@pytest.fixture
def client():
    from app import app
    app.testing = True
    with app.test_client() as client:
        yield client
