"""
Tests for gift certificate security controls.
Covers: rate limiting, input validation, honeypot, and the checkout flow.
"""
import sys
import os
import time

import pytest

# Make gift-referral importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gift-referral"))

from gift_security import (
    check_rate_limit,
    check_honeypot,
    validate_name_field,
    validate_email_field,
    validate_optional_email,
    validate_message_field,
    _reset_rate_limit,
)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TestRateLimit:
    def setup_method(self):
        # Use a unique IP per test to avoid cross-test pollution
        self._ip = f"10.0.0.{id(self) % 250}"
        _reset_rate_limit(self._ip)

    def test_allows_first_request(self):
        assert check_rate_limit(self._ip, max_requests=3, window_seconds=3600) is True

    def test_allows_up_to_max(self):
        for _ in range(3):
            assert check_rate_limit(self._ip, max_requests=3, window_seconds=3600) is True

    def test_blocks_after_max(self):
        for _ in range(3):
            check_rate_limit(self._ip, max_requests=3, window_seconds=3600)
        assert check_rate_limit(self._ip, max_requests=3, window_seconds=3600) is False

    def test_different_ips_are_independent(self):
        ip2 = self._ip + "x"
        _reset_rate_limit(ip2)
        for _ in range(3):
            check_rate_limit(self._ip, max_requests=3, window_seconds=3600)
        # ip2 should still be allowed
        assert check_rate_limit(ip2, max_requests=3, window_seconds=3600) is True

    def test_window_expiry_allows_again(self):
        # Use a 1-second window for speed
        for _ in range(2):
            check_rate_limit(self._ip, max_requests=2, window_seconds=1)
        assert check_rate_limit(self._ip, max_requests=2, window_seconds=1) is False
        time.sleep(1.1)
        assert check_rate_limit(self._ip, max_requests=2, window_seconds=1) is True


# ---------------------------------------------------------------------------
# Name field validation
# ---------------------------------------------------------------------------

class TestNameValidation:
    def test_valid_name(self):
        ok, _ = validate_name_field("Jane Smith")
        assert ok is True

    def test_empty_name_is_invalid(self):
        ok, msg = validate_name_field("")
        assert ok is False
        assert "required" in msg.lower()

    def test_name_too_long(self):
        ok, msg = validate_name_field("A" * 101)
        assert ok is False
        assert "too long" in msg.lower()

    def test_rejects_http_url(self):
        ok, msg = validate_name_field("https://t.me/spam_channel")
        assert ok is False
        assert "url" in msg.lower() or "link" in msg.lower()

    def test_rejects_www_url(self):
        ok, _ = validate_name_field("www.spam-site.com")
        assert ok is False

    def test_rejects_dotcom(self):
        ok, _ = validate_name_field("buy cheap meds at pill.com now")
        assert ok is False

    def test_rejects_telegram(self):
        ok, _ = validate_name_field("telegra.ph/get-rich")
        assert ok is False

    def test_rejects_at_sign(self):
        # user@example.com is caught either by the URL pattern (.com) or the @ check
        ok, _msg = validate_name_field("user@example.com")
        assert ok is False

    def test_rejects_pure_at_sign_no_tld(self):
        # bare @ with no .com so only the @ check fires
        ok, msg = validate_name_field("user@localdomain")
        assert ok is False
        assert "email" in msg.lower()

    def test_rejects_crypto_keyword(self):
        ok, _ = validate_name_field("Get rich with crypto")
        assert ok is False

    def test_rejects_bitcoin(self):
        ok, _ = validate_name_field("Bitcoin investment opportunity")
        assert ok is False

    def test_rejects_click_here(self):
        ok, _ = validate_name_field("Click here to win")
        assert ok is False

    def test_unicode_name_is_valid(self):
        ok, _ = validate_name_field("Ірина Пашинська")
        assert ok is True

    def test_hyphenated_name(self):
        ok, _ = validate_name_field("Mary-Jane Watson")
        assert ok is True


# ---------------------------------------------------------------------------
# Email field validation
# ---------------------------------------------------------------------------

class TestEmailValidation:
    def test_valid_email(self):
        ok, _ = validate_email_field("user@example.com")
        assert ok is True

    def test_empty_email_is_invalid(self):
        ok, msg = validate_email_field("")
        assert ok is False
        assert "required" in msg.lower()

    def test_missing_at_sign(self):
        ok, _ = validate_email_field("notanemail")
        assert ok is False

    def test_missing_domain(self):
        ok, _ = validate_email_field("user@")
        assert ok is False

    def test_missing_tld(self):
        ok, _ = validate_email_field("user@example")
        assert ok is False

    def test_rejects_tempmail(self):
        ok, msg = validate_email_field("anon@tempmail.com")
        assert ok is False
        assert "disposable" in msg.lower() or "temporary" in msg.lower()

    def test_rejects_mailinator(self):
        ok, _ = validate_email_field("throwaway@mailinator.com")
        assert ok is False

    def test_rejects_yopmail(self):
        ok, _ = validate_email_field("x@yopmail.com")
        assert ok is False

    def test_valid_gmail(self):
        ok, _ = validate_email_field("user@gmail.com")
        assert ok is True

    def test_valid_plus_addressing(self):
        ok, _ = validate_email_field("user+tag@example.org")
        assert ok is True

    def test_too_long_email(self):
        ok, _ = validate_email_field("a" * 200 + "@example.com")
        assert ok is False


class TestOptionalEmail:
    def test_empty_is_ok(self):
        ok, _ = validate_optional_email("")
        assert ok is True

    def test_none_is_ok(self):
        ok, _ = validate_optional_email(None)
        assert ok is True

    def test_valid_email_passes(self):
        ok, _ = validate_optional_email("friend@example.com")
        assert ok is True

    def test_invalid_email_fails(self):
        ok, _ = validate_optional_email("notanemail")
        assert ok is False


# ---------------------------------------------------------------------------
# Message field validation
# ---------------------------------------------------------------------------

class TestMessageValidation:
    def test_valid_message(self):
        ok, _ = validate_message_field("Happy birthday! Enjoy your session.")
        assert ok is True

    def test_empty_message_is_ok(self):
        ok, _ = validate_message_field("")
        assert ok is True

    def test_message_too_long(self):
        ok, msg = validate_message_field("x" * 501)
        assert ok is False
        assert "too long" in msg.lower()

    def test_rejects_url_in_message(self):
        ok, _ = validate_message_field("Check out https://spam.com for prizes")
        assert ok is False

    def test_unicode_message_is_valid(self):
        ok, _ = validate_message_field("Вітаю з днем народження! 🎂")
        assert ok is True


# ---------------------------------------------------------------------------
# Honeypot
# ---------------------------------------------------------------------------

class TestHoneypot:
    def test_empty_website_field_passes(self):
        assert check_honeypot({"website": ""}) is True

    def test_missing_website_field_passes(self):
        assert check_honeypot({}) is True

    def test_filled_website_field_fails(self):
        assert check_honeypot({"website": "http://spam.com"}) is False

    def test_whitespace_only_passes(self):
        assert check_honeypot({"website": "   "}) is True

    def test_any_text_fails(self):
        assert check_honeypot({"website": "anything"}) is False


# ---------------------------------------------------------------------------
# Integration: _validate_checkout_form (via routes helper)
# ---------------------------------------------------------------------------

class TestCheckoutFormValidation:
    """
    Test the _validate_checkout_form helper from routes without starting Flask.
    We import it directly.
    """

    @pytest.fixture(autouse=True)
    def _import_helper(self):
        # Patch stripe so the import doesn't fail without an API key
        import unittest.mock as mock
        with mock.patch.dict("sys.modules", {"stripe": mock.MagicMock()}):
            # Re-import the routes module
            if "gift_referral_routes" in sys.modules:
                del sys.modules["gift_referral_routes"]
            import gift_referral_routes as routes
            self._validate = routes._validate_checkout_form

    def test_valid_form_returns_no_errors(self):
        form = {
            "purchaser_name": "Jane Smith",
            "purchaser_email": "jane@example.com",
            "recipient_name": "Bob Jones",
            "recipient_email": "bob@example.com",
            "personal_message": "Happy birthday!",
        }
        assert self._validate(form) == []

    def test_spam_name_returns_error(self):
        form = {
            "purchaser_name": "https://t.me/get_rich",
            "purchaser_email": "victim@example.com",
        }
        errors = self._validate(form)
        assert len(errors) > 0
        assert any("url" in e.lower() or "link" in e.lower() for e in errors)

    def test_invalid_email_returns_error(self):
        form = {
            "purchaser_name": "Jane Smith",
            "purchaser_email": "not-an-email",
        }
        errors = self._validate(form)
        assert len(errors) > 0

    def test_missing_purchaser_name_returns_error(self):
        form = {
            "purchaser_name": "",
            "purchaser_email": "jane@example.com",
        }
        errors = self._validate(form)
        assert len(errors) > 0

    def test_url_in_message_returns_error(self):
        form = {
            "purchaser_name": "Jane",
            "purchaser_email": "jane@example.com",
            "personal_message": "Visit www.spam.com",
        }
        errors = self._validate(form)
        assert len(errors) > 0

    def test_optional_fields_empty_is_ok(self):
        form = {
            "purchaser_name": "Jane",
            "purchaser_email": "jane@example.com",
            "recipient_name": "",
            "recipient_email": "",
            "personal_message": "",
        }
        assert self._validate(form) == []
