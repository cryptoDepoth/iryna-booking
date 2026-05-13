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
