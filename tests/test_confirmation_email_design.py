"""Confirmation email design/content regression tests.

The confirmation email is revenue/customer-experience critical. It should tell
clients exactly what happens next after the deposit is confirmed, without
changing the booking confirmation flow.
"""
from types import SimpleNamespace

import app as booking_app


def _capture_confirmation_email(monkeypatch, location="Confederation Park, Calgary", location_url=None):
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    booking_app._send_client_email(
        to_email="client@example.com",
        client_name="Anna Client",
        event_date="2026-05-20",
        slot_time="10:00 AM",
        event_title="Motherhood Mini Session",
        booking_id=4242,
        location=location,
        location_url=location_url,
    )
    return captured["input"]


def _capture_confirmation_email_default(monkeypatch):
    return _capture_confirmation_email(monkeypatch)


def test_confirmation_email_has_next_steps_timeline(monkeypatch):
    email = _capture_confirmation_email_default(monkeypatch)

    assert "What happens next" in email
    assert "May 20, 2026" in email
    assert "10:00 AM" in email
    assert "Confederation Park, Calgary" in email
    assert "remaining balance" in email.lower()
    assert "after the photo session" in email
    # Step 4 used to be "edited gallery within one week"; per Iryna's brand
    # update the deliverable is now described as the unedited original
    # photos (see Step 4 in _send_client_email plain + HTML branches).
    assert "original photos" in email.lower()
    assert "unedited" in email.lower()
    assert "Wfolio" in email
    assert "download" in email.lower()
    assert "1–2 months" in email or "1-2 months" in email


def test_confirmation_email_uses_premium_card_timeline_design(monkeypatch):
    email = _capture_confirmation_email_default(monkeypatch)

    assert "timeline-card" in email
    assert "timeline-step" in email
    assert "linear-gradient" in email
    assert "border-radius:24px" in email or "border-radius: 24px" in email
    assert "box-shadow" in email
    assert "Deposit confirmed" in email


def test_confirmation_email_uses_current_default_delivery_promise(monkeypatch):
    email = _capture_confirmation_email_default(monkeypatch)

    assert "Quick turnaround (within 48 hours)" not in email
    assert "All original photos" in email
    assert "within 6–7 business days after your session" in email
    assert "Select the photos you want professionally retouched" in email
    assert "After you submit your selections" in email
    assert "within an additional 6–7 business days" in email
    assert "complete gallery within 14 calendar days" not in email


def test_confirmation_email_has_email_safe_21st_inspired_blocks(monkeypatch):
    email = _capture_confirmation_email_default(monkeypatch)

    assert "email-safe-21st" in email
    assert "Deposit confirmed" in email
    assert "Session reserved" in email
    assert "Before your session" in email
    assert "Arrive 5–10 minutes early" in email or "Arrive 5-10 minutes early" in email
    assert "Message me on Instagram" in email
    # Keep it email-safe: no scripts/animations/unsupported interactive widgets.
    assert "<script" not in email.lower()
    assert "animation:" not in email.lower()
    assert "@keyframes" not in email.lower()


def test_confirmation_email_escapes_client_supplied_html(monkeypatch):
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        captured["input"] = input
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    booking_app._send_client_email(
        to_email="client@example.com",
        client_name='<img src=x onerror=alert(1)>',
        event_date="2026-05-20",
        slot_time="10:00 AM",
        event_title='<script>alert(1)</script>',
        booking_id=99,
        location='<b onclick="bad()">Park</b>',
    )
    email = captured["input"]

    assert "<script>" not in email
    assert "onerror=" not in email
    assert "onclick=" not in email
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in email


def test_confirmation_email_has_location_maps_card_and_calendar_button(monkeypatch):
    email = _capture_confirmation_email(
        monkeypatch,
        location="River Park",
        location_url="https://maps.app.goo.gl/zcJ2vE8z2DSBRa5C9"
    )

    assert "Open in Google Maps" in email
    assert "Open in Apple Maps" in email
    assert "maps.app.goo.gl" in email or "google.com/maps" in email
    assert "Add to Calendar" in email
    assert "calendar.google.com" in email
    assert "Arrive 5–10 minutes early" in email or "Arrive 5-10 minutes early" in email
    assert "your session starts at" in email.lower() or "session time" in email.lower()


def test_confirmation_email_plain_text_includes_location_url_and_arrival(monkeypatch):
    email = _capture_confirmation_email(
        monkeypatch,
        location="River Park",
        location_url="https://maps.app.goo.gl/zcJ2vE8z2DSBRa5C9"
    )

    # Plain-text part is after the text/plain boundary
    plain_part = ""
    if "Content-Type: text/plain" in email:
        plain_part = email.split("Content-Type: text/plain")[1].split("--====boundary")[0]
    else:
        plain_part = email

    assert "River Park" in plain_part
    assert "maps.app.goo.gl" in plain_part or "google.com/maps" in plain_part
    assert "Arrive 5–10 minutes early" in plain_part or "Arrive 5-10 minutes early" in plain_part
    assert "Add to Calendar" in plain_part


def test_confirmation_email_includes_addons_amounts_consent_and_questionnaire(monkeypatch):
    captured = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        captured["input"] = input
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    booking_app._send_client_email(
        to_email="client@example.com",
        client_name="Anna Client",
        event_date="2026-08-10",
        slot_time="10:00",
        event_title="Individual Portraits",
        booking_id=99,
        location="River Park",
        selected_addons=[
            {"title": "<script>Bad</script>Short Vertical Behind-the-Scenes Reel", "price": 50.0},
        ],
        addons_total=50.0,
        total_price=300.0,
        amount_due_today=100.0,
        remaining_balance=200.0,
        marketing_consent="no",
        questionnaire_url="https://book.test/questionnaire?booking_id=99&token=abc",
    )
    email = captured["input"]

    assert "Selected add-ons" in email
    assert "Short Vertical Behind-the-Scenes Reel" in email
    assert "Selected add-ons: $50.00 CAD" in email
    assert "Amount due today: $100.00 CAD" in email
    assert "Remaining balance: $200.00 CAD" in email
    assert "keep my gallery private" in email.lower()
    assert "Optional session questionnaire" in email
    assert "https://book.test/questionnaire" in email
    assert "<script>" not in email
