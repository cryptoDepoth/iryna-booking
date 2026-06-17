"""Tests for referral code logic."""

import os
import sys
import re
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import gift_referral_db as db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = str(tmp_path / "test_referral.db")
    db.init_db(db_path)
    yield
    db.DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gift_referral.db")


def _make_referral(owner_email="anna@example.com", owner_name="Anna Sample", **kwargs):
    return db.create_referral_code(owner_email=owner_email, owner_name=owner_name, **kwargs)


# ---------------------------------------------------------------------------
# test_referral_code_generation
# ---------------------------------------------------------------------------

def test_referral_code_format():
    code = db.generate_referral_code()
    assert re.match(r"^REF-PASHYN-[A-Z0-9]{4}$", code), f"Bad format: {code}"


def test_referral_codes_are_unique():
    codes = {db.generate_referral_code() for _ in range(200)}
    assert len(codes) >= 199


def test_create_referral_code_returns_code():
    code = _make_referral()
    assert re.match(r"^REF-PASHYN-[A-Z0-9]{4}$", code)


def test_create_referral_code_stored_in_db():
    code = _make_referral(owner_email="test@x.com", owner_name="Test User", owner_booking_id=5)
    ref  = db.get_referral_code(code)
    assert ref is not None
    assert ref["owner_email"] == "test@x.com"
    assert ref["owner_name"]  == "Test User"
    assert ref["owner_booking_id"] == 5
    assert ref["status"] == "active"
    assert ref["uses_count"] == 0
    assert ref["max_uses"] == 10
    assert ref["discount_for_friend"] == 20.0
    assert ref["reward_for_owner"] == 20.0


# ---------------------------------------------------------------------------
# test_referral_validation
# ---------------------------------------------------------------------------

def test_validate_active_referral_code():
    code   = _make_referral()
    result = db.validate_referral_code(code)
    assert result["valid"] is True
    assert result["type"] == "referral"
    assert result["discount"] == 20.0
    assert result["owner_name"] == "Anna Sample"


def test_validate_nonexistent_referral_code():
    result = db.validate_referral_code("REF-FAKE-0000")
    assert result["valid"] is False
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# test_referral_self_use_blocked
# ---------------------------------------------------------------------------

def test_referral_self_use_blocked():
    code   = _make_referral(owner_email="owner@example.com")
    result = db.validate_referral_code(code, referee_email="owner@example.com")
    assert result["valid"] is False
    assert "own" in result["error"].lower()


def test_referral_self_use_blocked_case_insensitive():
    code   = _make_referral(owner_email="Owner@Example.COM")
    result = db.validate_referral_code(code, referee_email="owner@example.com")
    assert result["valid"] is False


def test_referral_different_user_allowed():
    code   = _make_referral(owner_email="owner@example.com")
    result = db.validate_referral_code(code, referee_email="friend@example.com")
    assert result["valid"] is True


# ---------------------------------------------------------------------------
# test_referral_max_uses
# ---------------------------------------------------------------------------

def test_referral_max_uses_enforced():
    code = _make_referral()
    ref  = db.get_referral_code(code)
    # Manually set uses_count to max
    conn = db.get_db()
    conn.execute("UPDATE referral_codes SET uses_count = 10 WHERE id = ?", (ref["id"],))
    conn.commit()
    conn.close()

    result = db.validate_referral_code(code)
    assert result["valid"] is False
    assert "maximum" in result["error"].lower()


def test_referral_eleventh_use_rejected():
    code = _make_referral()
    # Record 10 uses
    for i in range(10):
        db.record_referral_use(code, referee_email=f"user{i}@x.com")
    # 11th validation should fail
    result = db.validate_referral_code(code, referee_email="user10@x.com")
    assert result["valid"] is False
    assert "maximum" in result["error"].lower()


def test_referral_uses_count_increments():
    code = _make_referral()
    db.record_referral_use(code, referee_email="a@x.com")
    db.record_referral_use(code, referee_email="b@x.com")
    ref = db.get_referral_code(code)
    assert ref["uses_count"] == 2


# ---------------------------------------------------------------------------
# test_referral_reward_trigger
# ---------------------------------------------------------------------------

def test_referral_reward_trigger_on_payment():
    code = _make_referral(owner_email="owner@x.com")
    db.record_referral_use(code, referee_email="friend@x.com",
                           referee_name="Friend Name", referee_booking_id=101)

    # Confirm payment → should trigger reward
    use = db.confirm_referral_payment(101)
    assert use is not None
    assert use["payment_confirmed"] == 1
    assert use["reward_triggered"] == 1
    assert use["owner_email"] == "owner@x.com"
    assert use["confirmed_at"] is not None


def test_referral_reward_not_on_booking():
    """Reward must NOT be triggered just by recording a referral use — only on payment."""
    code    = _make_referral()
    use_id  = db.record_referral_use(code, referee_email="friend@x.com",
                                     referee_name="Friend", referee_booking_id=202)
    # Check DB directly — reward should NOT be triggered yet
    conn = db.get_db()
    row  = conn.execute("SELECT * FROM referral_uses WHERE id = ?", (use_id,)).fetchone()
    conn.close()
    assert row["payment_confirmed"] == 0
    assert row["reward_triggered"] == 0


def test_referral_reward_not_triggered_twice():
    code = _make_referral()
    db.record_referral_use(code, referee_email="friend@x.com", referee_booking_id=303)
    db.confirm_referral_payment(303)  # First confirmation — works
    use2 = db.confirm_referral_payment(303)  # Second call — should return None
    assert use2 is None


def test_referral_reward_not_triggered_for_wrong_booking():
    code = _make_referral()
    db.record_referral_use(code, referee_email="friend@x.com", referee_booking_id=404)
    use = db.confirm_referral_payment(999)  # Wrong booking_id
    assert use is None


# ---------------------------------------------------------------------------
# test_record_referral_use
# ---------------------------------------------------------------------------

def test_record_referral_use_returns_id():
    code   = _make_referral()
    use_id = db.record_referral_use(code, referee_email="a@x.com", referee_name="Alice")
    assert isinstance(use_id, int)
    assert use_id > 0


def test_record_referral_use_stores_discount():
    code = _make_referral()
    db.record_referral_use(code, referee_email="a@x.com", discount_applied=20.0)
    conn = db.get_db()
    row  = conn.execute("SELECT * FROM referral_uses WHERE referee_email = 'a@x.com'").fetchone()
    conn.close()
    assert row["discount_applied"] == 20.0


def test_list_referral_codes():
    _make_referral(owner_email="a@x.com", owner_name="A")
    _make_referral(owner_email="b@x.com", owner_name="B")
    codes = db.list_referral_codes()
    assert len(codes) >= 2


def test_list_referral_uses():
    code = _make_referral()
    db.record_referral_use(code, referee_email="x@x.com")
    uses = db.list_referral_uses()
    assert len(uses) >= 1
    assert "code" in uses[0]  # joined field from referral_codes


# ---------------------------------------------------------------------------
# test_get_referral_code_by_owner
# ---------------------------------------------------------------------------

def test_get_referral_code_by_owner():
    code = _make_referral(owner_email="owner@lookup.com")
    found = db.get_referral_code_by_owner("owner@lookup.com")
    assert found is not None
    assert found["code"] == code


def test_get_referral_code_by_owner_not_found():
    found = db.get_referral_code_by_owner("nobody@nowhere.com")
    assert found is None
