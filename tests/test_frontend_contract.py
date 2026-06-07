"""Frontend/backend contract regression tests for the v2 drawer UI."""
from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index_v2.html"


def test_drawer_requests_slots_by_date_and_event_id():
    """The drawer must call /slots/<date>?event_id=<id>.

    Regression: API.slots used to accept one argument named eventId but callers
    passed e.date. That generated /slots/2026-06-07?event_id=2026-06-07,
    so the backend could not resolve the event and the UI showed
    "No slots available" even while /events and /slots had inventory.
    """
    html = TEMPLATE.read_text()

    assert "slots: async (date, eventId)" in html
    assert "API.slots(" in html
    assert ", e.id)" in html
    assert "?event_id=${encodeURIComponent(eventId)}" in html

    # Do not reintroduce the old one-argument contract.
    assert "slots: async (eventId)" not in html
    assert "API.slots(e.date);" not in html
    assert "fetch(`/slots/${eventId}?event_id=${eventId}`)" not in html


def test_drawer_uses_available_slots_returned_by_api_without_fake_status_filter():
    """Backend already returns only available slots; slot objects have no status field."""
    html = TEMPLATE.read_text()

    assert "const availableSlots = slotsData.slots || [];" in html
    assert "status === 'available'" not in html
    assert 'status === "available"' not in html


def test_public_assistant_widget_posts_to_assistant_chat_endpoint():
    """The public assistant should remain wired to the Flask assistant endpoint."""
    html = TEMPLATE.read_text()

    assert 'id="assistantWidget"' in html
    assert "fetch('/assistant/chat'" in html
    assert "updateAssistantLang" in html


def test_drawer_contains_addons_agreement_and_amount_summary_hooks():
    """Session-inspired upgrades must remain wired through the current drawer."""
    html = TEMPLATE.read_text()

    assert "renderAddonsSection" in html
    assert "getSelectedAddons" in html
    assert "updateBookingSummary" in html
    assert "booking-addon" in html
    assert "marketing_consent" in html
    assert "terms_accepted" in html
    assert "agreement_name" in html
    assert "Amount due today" in html
    assert "Remaining balance" in html
    assert "Change time" in html
    assert "questionnaire" not in html.lower() or "after_confirmed_payment" in html


def test_meta_campaign_landing_cue_guides_ad_clicks_to_booking_drawer():
    """Meta ad traffic should see a booking-site cue, not be pushed back to DM."""
    html = TEMPLATE.read_text()

    assert 'id="campaignCue"' in html
    assert "getCampaignLandingCue" in html
    assert "renderCampaignLandingCue" in html
    assert "openCampaignCueSession" in html
    assert "campaign_cue_view" in html
    assert "campaign_cue_cta_click" in html
    assert "utm_campaign" in html
    assert "fbclid" in html
    assert "openDrawer(event.id)" in html
    assert "Mountain photos near Calgary" in html
    assert "What’s included" in html
    assert "renderIncludedSection" in html


def test_mountain_campaign_does_not_match_generic_mini_copy_or_sold_out_events():
    """A Mountain Meta click should route to a bookable mountain/outdoor event, not any generic mini."""
    html = TEMPLATE.read_text()

    assert "function pickHeroEvent()" in html
    assert "visibleEvents().filter(isBookableEvent)" in html
    assert "Number(e.spots_left || 0) > 0" in html
    assert "mountain|mountains|quarry|bragg|kananaskis|outdoor" in html
    assert "mountain|mountains|mini|bragg|kananaskis|outdoor" not in html
    assert "const featured = pickHeroEvent();" in html
