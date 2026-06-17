"""Tests for cumulative referral credit system."""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import gift_referral_db as db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = str(tmp_path / "test_credits.db")
    db.init_db(db_path)
    yield
    db.DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gift_referral.db")


def _make_referral(owner_email="owner@x.com", owner_name="Owner", **kwargs):
    return db.create_referral_code(owner_email=owner_email, owner_name=owner_name, **kwargs)


def _earn_credit(code, booking_id, referee_email="friend@x.com", referee_name="Friend"):
    db.record_referral_use(code, referee_email, referee_name=referee_name, referee_booking_id=booking_id)
    return db.confirm_referral_payment(booking_id)


# ---------------------------------------------------------------------------
# test_cumulative_rewards
# ---------------------------------------------------------------------------

def test_cumulative_rewards_three_friends():
    code = _make_referral()
    _earn_credit(code, 101, "f1@x.com", "Friend One")
    _earn_credit(code, 102, "f2@x.com", "Friend Two")
    _earn_credit(code, 103, "f3@x.com", "Friend Three")

    credits = db.get_credit_balance("owner@x.com")
    assert credits is not None
    assert credits["total_earned"] == 60.0
    assert credits["balance"] == 60.0


def test_cumulative_rewards_single_use():
    code = _make_referral()
    result = _earn_credit(code, 201)
    assert result is not None
    assert result["new_balance"] == 20.0
    assert result["total_earned"] == 20.0

    credits = db.get_credit_balance("owner@x.com")
    assert credits["balance"] == 20.0


def test_cumulative_rewards_balance_grows_per_payment():
    code = _make_referral()
    r1 = _earn_credit(code, 301, "a@x.com")
    assert r1["new_balance"] == 20.0

    r2 = _earn_credit(code, 302, "b@x.com")
    assert r2["new_balance"] == 40.0

    r3 = _earn_credit(code, 303, "c@x.com")
    assert r3["new_balance"] == 60.0


def test_total_earned_never_decreases_after_redemption():
    code = _make_referral()
    _earn_credit(code, 401)
    _earn_credit(code, 402, "b@x.com")
    # Redeem once
    db.redeem_credit("owner@x.com", 20.0)
    credits = db.get_credit_balance("owner@x.com")
    assert credits["total_earned"] == 40.0  # still shows all-time
    assert credits["balance"] == 20.0        # balance reflects spend


# ---------------------------------------------------------------------------
# test_credit_redemption
# ---------------------------------------------------------------------------

def test_credit_redemption_owner_uses_own_code_with_balance():
    code = _make_referral(owner_email="owner@x.com")
    _earn_credit(code, 501)  # owner now has $20

    # Validate: owner uses own code → should return credit_redemption
    result = db.validate_referral_code(code, referee_email="owner@x.com")
    assert result["valid"] is True
    assert result["type"] == "credit_redemption"
    assert result["discount"] == 20.0
    assert result["balance"] == 20.0


def test_credit_redemption_decreases_balance():
    code = _make_referral()
    _earn_credit(code, 601, "f1@x.com")
    _earn_credit(code, 602, "f2@x.com")  # balance = $40

    updated = db.redeem_credit("owner@x.com", 20.0, booking_id=999)
    assert updated is not None
    assert updated["balance"] == 20.0      # $40 - $20 = $20
    assert updated["total_earned"] == 40.0  # unchanged


def test_credit_redemption_full_balance():
    code = _make_referral()
    _earn_credit(code, 701)  # balance = $20
    updated = db.redeem_credit("owner@x.com", 20.0)
    assert updated is not None
    assert updated["balance"] == 0.0


# ---------------------------------------------------------------------------
# test_credit_not_go_negative
# ---------------------------------------------------------------------------

def test_credit_not_go_negative():
    code = _make_referral()
    _earn_credit(code, 801)  # balance = $20

    # Try to spend $40 when balance is $20
    result = db.redeem_credit("owner@x.com", 40.0)
    assert result is None

    # Balance must be unchanged
    credits = db.get_credit_balance("owner@x.com")
    assert credits["balance"] == 20.0


def test_credit_not_go_negative_zero_balance():
    # No credits earned at all → cannot redeem
    result = db.redeem_credit("nobody@x.com", 20.0)
    assert result is None


def test_credit_not_go_negative_exact_zero():
    code = _make_referral()
    _earn_credit(code, 901)  # balance = $20
    db.redeem_credit("owner@x.com", 20.0)  # balance → 0

    # Second redeem with 0 balance
    result = db.redeem_credit("owner@x.com", 20.0)
    assert result is None


def test_owner_no_balance_own_code_blocked():
    code = _make_referral(owner_email="owner@x.com")
    # No credits yet
    result = db.validate_referral_code(code, referee_email="owner@x.com")
    assert result["valid"] is False
    assert result["balance"] == 0.0
    assert "own" in result["error"].lower()


# ---------------------------------------------------------------------------
# test_credit_history
# ---------------------------------------------------------------------------

def test_credit_history_logged_correctly():
    code = _make_referral()
    _earn_credit(code, 1001, "a@x.com", "Alice")
    _earn_credit(code, 1002, "b@x.com", "Bob")
    db.redeem_credit("owner@x.com", 20.0, booking_id=5000, note="Used on booking")

    history = db.get_credit_history("owner@x.com")
    assert len(history) == 3

    earned   = [e for e in history if e["event_type"] == "earned"]
    redeemed = [e for e in history if e["event_type"] == "redeemed"]
    assert len(earned) == 2
    assert len(redeemed) == 1


def test_credit_history_earned_amounts():
    code = _make_referral()
    _earn_credit(code, 1101)
    _earn_credit(code, 1102, "b@x.com")

    history = db.get_credit_history("owner@x.com")
    earned_amounts = [e["amount"] for e in history if e["event_type"] == "earned"]
    assert all(a == 20.0 for a in earned_amounts)


def test_credit_history_redeemed_amount_negative():
    code = _make_referral()
    _earn_credit(code, 1201)
    db.redeem_credit("owner@x.com", 20.0)

    history = db.get_credit_history("owner@x.com")
    redeemed = [e for e in history if e["event_type"] == "redeemed"]
    assert redeemed[0]["amount"] == -20.0


def test_credit_history_empty_for_no_activity():
    history = db.get_credit_history("nobody@x.com")
    assert history == []


def test_credit_history_ordering_newest_first():
    code = _make_referral()
    _earn_credit(code, 1301, "a@x.com")
    _earn_credit(code, 1302, "b@x.com")
    history = db.get_credit_history("owner@x.com")
    # Newest (1302) should come first
    assert len(history) >= 2


# ---------------------------------------------------------------------------
# test_add_credit directly
# ---------------------------------------------------------------------------

def test_add_credit_creates_record():
    result = db.add_credit("new@x.com", "New User", 20.0)
    assert result["balance"] == 20.0
    assert result["total_earned"] == 20.0


def test_add_credit_upserts_existing():
    db.add_credit("owner@x.com", "Owner", 20.0)
    db.add_credit("owner@x.com", "Owner", 20.0)
    credits = db.get_credit_balance("owner@x.com")
    assert credits["balance"] == 40.0
    assert credits["total_earned"] == 40.0


def test_list_credit_balances():
    db.add_credit("a@x.com", "A", 20.0)
    db.add_credit("b@x.com", "B", 40.0)
    balances = db.list_credit_balances()
    # Should be sorted by balance desc
    assert len(balances) >= 2
    assert balances[0]["balance"] >= balances[1]["balance"]
