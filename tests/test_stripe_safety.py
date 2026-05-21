"""Stripe payment safety regression tests."""

from app import _stripe_checkout_idempotency_key


def test_stripe_checkout_idempotency_key_is_stable_for_same_booking():
    booking = {
        "id": 123,
        "event_id": "lilac-jun7",
        "date": "2026-06-07",
        "time": "10:00",
        "email": "Client@Example.com",
    }

    first = _stripe_checkout_idempotency_key(booking)
    second = _stripe_checkout_idempotency_key(dict(booking))

    assert first == second
    assert first.startswith("checkout-123-")


def test_stripe_checkout_idempotency_key_changes_between_bookings():
    booking_a = {"id": 123, "event_id": "a", "date": "2026-06-07", "time": "10:00", "email": "a@example.com"}
    booking_b = {"id": 124, "event_id": "a", "date": "2026-06-07", "time": "10:00", "email": "a@example.com"}

    assert _stripe_checkout_idempotency_key(booking_a) != _stripe_checkout_idempotency_key(booking_b)


def test_stripe_checkout_idempotency_key_does_not_expose_email_or_token():
    booking = {
        "id": 123,
        "event_id": "secret-event",
        "date": "2026-06-07",
        "time": "10:00",
        "email": "private@example.com",
        "confirmation_token": "super-secret-token",
    }

    key = _stripe_checkout_idempotency_key(booking)

    assert "private@example.com" not in key
    assert "super-secret-token" not in key
    assert len(key) < 80
