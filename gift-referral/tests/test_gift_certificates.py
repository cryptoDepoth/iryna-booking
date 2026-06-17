"""Tests for gift certificate logic."""

import os
import sys
import re
import pytest

# Add parent dir so imports work from tests/ subdirectory
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import gift_referral_db as db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Each test gets a clean in-memory-ish SQLite DB via a temp file."""
    db_path = str(tmp_path / "test_gift.db")
    db.init_db(db_path)
    yield
    # Cleanup: reset DB_PATH to default after test
    db.DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gift_referral.db")


def _make_cert(**kwargs):
    defaults = dict(
        purchaser_email  = "buyer@example.com",
        purchaser_name   = "John Buyer",
        recipient_name   = "Jane Gift",
        recipient_email  = "jane@example.com",
        personal_message = "Happy birthday!",
        session_type     = "mini",
        amount           = 230.0,
        amount_with_gst  = 241.50,
    )
    defaults.update(kwargs)
    return db.create_gift_certificate(**defaults)


# ---------------------------------------------------------------------------
# test_gift_code_generation
# ---------------------------------------------------------------------------

def test_gift_code_format():
    code = db.generate_gift_code()
    assert re.match(r"^GIFT-[A-Z0-9]{4}-[A-Z0-9]{4}$", code), f"Bad format: {code}"


def test_gift_codes_are_unique():
    codes = {db.generate_gift_code() for _ in range(200)}
    # Allow up to 1 collision in 200 — statistically extremely unlikely
    assert len(codes) >= 199


def test_create_gift_certificate_returns_code():
    code = _make_cert()
    assert re.match(r"^GIFT-[A-Z0-9]{4}-[A-Z0-9]{4}$", code)


def test_create_gift_certificate_stored_in_db():
    code = _make_cert(recipient_name="Alice")
    cert = db.get_gift_certificate(code)
    assert cert is not None
    assert cert["purchaser_email"] == "buyer@example.com"
    assert cert["recipient_name"] == "Alice"
    assert cert["session_type"] == "mini"
    assert cert["amount"] == 230.0
    assert cert["amount_with_gst"] == 241.50
    assert cert["status"] == "active"


# ---------------------------------------------------------------------------
# test_gift_validation
# ---------------------------------------------------------------------------

def test_validate_active_certificate():
    code   = _make_cert()
    result = db.validate_gift_certificate(code)
    assert result["valid"] is True
    assert result["type"] == "gift"
    assert result["session_type"] == "mini"
    assert result["amount"] == 230.0


def test_validate_nonexistent_code():
    result = db.validate_gift_certificate("GIFT-FAKE-0000")
    assert result["valid"] is False
    assert "not found" in result["error"].lower()


def test_validate_redeemed_certificate():
    code = _make_cert()
    db.redeem_gift_certificate(code, booking_id=99)
    result = db.validate_gift_certificate(code)
    assert result["valid"] is False
    assert "redeemed" in result["error"].lower()


def test_validate_expired_certificate():
    code = _make_cert()
    # Force expiry date into the past
    conn = db.get_db()
    conn.execute("UPDATE gift_certificates SET expires_at = '2020-01-01' WHERE code = ?", (code,))
    conn.commit()
    conn.close()
    result = db.validate_gift_certificate(code)
    assert result["valid"] is False
    assert "expired" in result["error"].lower()


def test_validate_session_type_mismatch():
    code   = _make_cert(session_type="mini")
    result = db.validate_gift_certificate(code, session_type="family")
    assert result["valid"] is False
    assert "mini" in result["error"]


def test_validate_custom_cert_works_for_any_session():
    code   = _make_cert(session_type="custom")
    result = db.validate_gift_certificate(code, session_type="family")
    assert result["valid"] is True


def test_validate_session_type_match():
    code   = _make_cert(session_type="family")
    result = db.validate_gift_certificate(code, session_type="family")
    assert result["valid"] is True


# ---------------------------------------------------------------------------
# test_gift_redemption
# ---------------------------------------------------------------------------

def test_redemption_marks_status_redeemed():
    code = _make_cert()
    ok   = db.redeem_gift_certificate(code, booking_id=42)
    assert ok is True
    cert = db.get_gift_certificate(code)
    assert cert["status"] == "redeemed"
    assert cert["redeemed_booking_id"] == 42
    assert cert["redeemed_at"] is not None


def test_redemption_cannot_redeem_twice():
    code = _make_cert()
    db.redeem_gift_certificate(code, booking_id=1)
    ok2  = db.redeem_gift_certificate(code, booking_id=2)
    assert ok2 is False  # second redeem returns False (no rows changed)
    cert = db.get_gift_certificate(code)
    assert cert["redeemed_booking_id"] == 1  # still points to first booking


def test_list_gift_certificates():
    _make_cert()
    _make_cert()
    certs = db.list_gift_certificates()
    assert len(certs) >= 2


# ---------------------------------------------------------------------------
# test_pdf_generation
# ---------------------------------------------------------------------------

def test_pdf_generation_returns_bytes():
    from gift_referral_pdf import generate_gift_certificate_pdf
    code = _make_cert()
    cert = db.get_gift_certificate(code)
    pdf  = generate_gift_certificate_pdf(cert)
    assert isinstance(pdf, bytes)
    assert len(pdf) > 1000  # A real PDF is at least ~1 KB
    assert pdf[:4] == b"%PDF"  # PDF magic bytes


def test_pdf_contains_code():
    from gift_referral_pdf import generate_gift_certificate_pdf
    code = _make_cert()
    cert = db.get_gift_certificate(code)
    pdf  = generate_gift_certificate_pdf(cert)
    # Code should appear somewhere in the PDF bytes as ASCII
    assert code.encode() in pdf


# ---------------------------------------------------------------------------
# test_amount_calculation
# ---------------------------------------------------------------------------

def test_gst_calculation_mini():
    gst = db.calculate_gst(230.0)
    assert gst == 241.50


def test_gst_calculation_family():
    gst = db.calculate_gst(290.0)
    assert gst == 304.50


def test_gst_calculation_custom():
    gst = db.calculate_gst(100.0)
    assert gst == 105.00


def test_gst_calculation_rounds_correctly():
    # $150 + 5% = $157.50 exactly
    gst = db.calculate_gst(150.0)
    assert gst == 157.50


def test_gst_rate_is_five_percent():
    assert db.GST_RATE == 0.05
