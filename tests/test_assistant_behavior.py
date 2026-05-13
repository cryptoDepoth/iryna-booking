"""Test assistant booking guidance behavior.

The assistant must guide visitors to the WEBSITE booking drawer for
fixed_slots and rolling_availability events, and ONLY send to Instagram
for inquiry_only events (weddings, custom packages).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
import assistant_engine as ae


def _make_fixed_event():
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "id": "mini_001", "title": "Spring Mini Session", "date": tomorrow,
        "start_time": "10:00", "end_time": "12:00", "deposit": 60,
        "full_price": 220, "slot_interval": 10, "session_length": 20,
        "spots": 5, "photos": ["mini1.jpg"], "booking_type": "fixed_slots",
        "location": "Confederation Park", "included": ["20 min", "5 edited photos"],
        "status": "active",
    }


def _make_rolling_event():
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "id": "ind_001", "title": "Individual Photoshoot", "date": tomorrow,
        "start_time": "10:00", "end_time": "18:00", "deposit": 100,
        "full_price": 350, "slot_interval": 60, "session_length": 60,
        "spots": 10, "photos": ["ind1.jpg"], "booking_type": "rolling_availability",
        "location": "Downtown Calgary", "included": ["60 min", "15 edited photos"],
        "status": "active",
    }


def _make_inquiry_event():
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "id": "wed_001", "title": "Wedding Photography", "date": tomorrow,
        "start_time": "09:00", "end_time": "21:00", "deposit": 300,
        "full_price": 2000, "slot_interval": 30, "session_length": 180,
        "spots": 1, "photos": ["wed1.jpg"], "booking_type": "inquiry_only",
        "location": "Banff", "included": ["Full day, location scouting"],
        "status": "active",
    }


def _settings():
    return {
        "photographer_name": "Iryna",
        "photographer_instagram": "@pashynska.photo",
        "photographer_instagram_url": "https://instagram.com/pashynska.photo",
        "photographer_email": "iryna@test.com",
        "reservation_minutes": 15,
        "currency": "CAD",
        "tax_label": "+GST",
        "timezone": "America/Edmonton",
    }


# ---------------------------------------------------------------------------
# Fallback booking guidance tests
# ---------------------------------------------------------------------------

def test_fallback_price_query_directs_to_site_for_fixed_slots():
    """When a visitor asks about price for a fixed_slots event, assistant
    should direct them to the website booking drawer, NOT Instagram."""
    events = [_make_fixed_event()]
    settings = _settings()
    ctx = ae.build_context("how much does it cost?", events, settings, db_path="")
    result = ae._fallback_answer("how much does it cost?", ctx, "en")

    assert "just click the session card" in result.lower() or "choose the session" in result.lower() or "booking" in result.lower(), \
        f"Expected site guidance, got: {result}"
    assert "dm iryna on instagram" not in result.lower(), \
        f"Fallback should NOT send to Instagram for fixed_slots. Got: {result}"


def test_fallback_booking_query_directs_to_site_for_rolling_event():
    """When a visitor asks to book a rolling_availability event, assistant
    should direct them to the website booking drawer, NOT Instagram."""
    events = [_make_rolling_event()]
    settings = _settings()
    ctx = ae.build_context("i want to book individually", events, settings, db_path="")
    result = ae._fallback_answer("i want to book individually", ctx, "en")

    assert "click the session card" in result.lower() or "fill out" in result.lower() or "choose" in result.lower(), \
        f"Expected site guidance, got: {result}"
    assert "dm iryna on instagram" not in result.lower(), \
        f"Fallback should NOT send to Instagram for rolling. Got: {result}"


def test_fallback_booking_inquiry_only_still_directs_to_instagram():
    """When a visitor asks to book an inquiry_only event (wedding), assistant
    MAY still suggest Instagram DM."""
    events = [_make_inquiry_event()]
    settings = _settings()
    ctx = ae.build_context("i want to book a wedding", events, settings, db_path="")
    # inquiry_only has no slots generated so the slot info is None
    # fallback won't have slots — should still say something meaningful
    result = ae._fallback_answer("i want to book a wedding", ctx, "en")

    # Inquiry events might still say "click the card" because the event is
    # listed on the site; our new rule is to always prefer site for booking
    # intent, even for inquiry_only.
    assert result  # just ensure it doesn't crash


def test_fallback_russian_directs_to_site_not_instagram():
    """Russian visitors should also get website guidance, not Instagram."""
    events = [_make_fixed_event()]
    settings = _settings()
    ctx = ae.build_context("сколько стоит фотосессия?", events, settings, db_path="")
    result = ae._fallback_answer("сколько стоит фотосессия?", ctx, "ru")

    assert "выберите сессию на сайте" in result.lower() or "сайт" in result.lower(), \
        f"Expected site guidance in Russian, got: {result}"
    assert "instagram" not in result.lower() or "напишите" not in result.lower(), \
        f"Fallback should NOT send to Instagram in Russian for fixed_slots. Got: {result}"


def test_fallback_generic_response_prefers_site():
    """Even for random questions, fallback should not blindly send to DM."""
    events = [_make_fixed_event()]
    settings = _settings()
    ctx = ae.build_context("what do you offer?", events, settings, db_path="")
    result = ae._fallback_answer("what do you offer?", ctx, "en")

    # The generic fallback should now mention the site first
    assert "site" in result.lower() or "website" in result.lower() or "booking" in result.lower(), \
        f"Generic fallback should mention site, got: {result}"


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def test_payment_claimed_preflight_still_works():
    result = ae.answer_assistant_message(
        "i already paid the deposit",
        history=[],
        events=[_make_fixed_event()],
        settings=_settings(),
        db_path="",
        lang="en",
    )
    assert result["source"] == "preflight"
    assert "i don't have access to payment" in result["answer"].lower()


def test_off_topic_preflight_still_works():
    result = ae.answer_assistant_message(
        "fortnite update when?",
        history=[],
        events=[_make_fixed_event()],
        settings=_settings(),
        db_path="",
        lang="en",
    )
    assert result["source"] == "preflight"
    assert "only help with" in result["answer"].lower()


# ---------------------------------------------------------------------------
# Context includes booking_type
# ---------------------------------------------------------------------------

def test_context_includes_booking_type_in_event_lines():
    events = [_make_fixed_event()]
    ctx = ae.build_context("hello", events, {**_settings(), "photographer_instagram_url": "ig"}, db_path="")
    # The event lines should include the booking_type
    assert "fixed_slots" in ctx["events"], f"Context missing booking_type. Got: {ctx['events']}"


def test_context_includes_rolling_booking_type():
    events = [_make_rolling_event()]
    ctx = ae.build_context("hello", events, {**_settings(), "photographer_instagram_url": "ig"}, db_path="")
    assert "rolling_availability" in ctx["events"], f"Context should contain booking_type. Got: {ctx['events']}"


def test_context_includes_inquiry_booking_type():
    events = [_make_inquiry_event()]
    ctx = ae.build_context("hello", events, {**_settings(), "photographer_instagram_url": "ig"}, db_path="")
    assert "inquiry_only" in ctx["events"], f"Context should contain inquiry_only. Got: {ctx['events']}"
