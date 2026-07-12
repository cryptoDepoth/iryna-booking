"""Direct event / GBP product deeplink tests."""

import app as booking_app


TEST_EVENTS = [
    {
        "id": "canoe-mini-session-2026-07-04",
        "title": "Canoe Mini Session",
        "date": "2026-07-04",
        "status": "active",
        "session_type": "mini",
        "type": "mini",
    },
    {
        "id": "canoe-mini-session-2026-07-26",
        "title": "Canoe Mini Session",
        "date": "2026-07-26",
        "status": "active",
        "session_type": "mini",
        "type": "mini",
    },
    {
        "id": "boho-swing-mini-sessions-2026-07-12",
        "title": "Boho Swing Mini Sessions",
        "date": "2026-07-12",
        "status": "active",
        "session_type": "mini",
        "type": "mini",
    },
]


import datetime as _dt

# Freeze the clock after the ended July 4 event but before the July 12/26
# fixtures. This keeps the alias test deterministic while still verifying that
# the resolver skips past events and opens the next matching canoe session.
_FROZEN_NOW = _dt.datetime(2026, 7, 10, 10, 0, tzinfo=_dt.timezone.utc)


def _patch_events(monkeypatch):
    monkeypatch.setattr(booking_app, "_local_now", lambda: _FROZEN_NOW.astimezone(booking_app._tz))
    monkeypatch.setattr(booking_app, "EVENTS", [dict(e) for e in TEST_EVENTS])
    monkeypatch.setattr(booking_app, "_public_events_payload", lambda: [dict(e, spots_left=3, total_spots=4) for e in TEST_EVENTS])
    monkeypatch.setattr(booking_app, "_past_events_payload", lambda: [])


def test_gbp_utm_content_canoe_mini_auto_opens_next_canoe_event(client, monkeypatch):
    _patch_events(monkeypatch)

    resp = client.get("/?utm_source=google&utm_medium=organic&utm_campaign=gbp_products&utm_content=canoe_mini")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'openDrawer("canoe-mini-session-2026-07-26")' in html


def test_exact_event_id_alias_auto_opens_drawer(client, monkeypatch):
    _patch_events(monkeypatch)

    resp = client.get("/?event_id=boho-swing-mini-sessions-2026-07-12")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'openDrawer("boho-swing-mini-sessions-2026-07-12")' in html


def test_unknown_explicit_direct_event_still_404(client, monkeypatch):
    _patch_events(monkeypatch)

    resp = client.get("/?event=does-not-exist")

    assert resp.status_code == 404


def test_unknown_utm_content_keeps_homepage(client, monkeypatch):
    _patch_events(monkeypatch)

    resp = client.get("/?utm_source=google&utm_content=generic_profile_link")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "setTimeout(() => openDrawer" not in html


def test_short_alias_resolver_chooses_next_matching_event(monkeypatch):
    _patch_events(monkeypatch)

    ev = booking_app.resolve_event_deeplink("boho_swing")

    assert ev is not None
    assert ev["id"] == "boho-swing-mini-sessions-2026-07-12"


def test_boho_event_copy_and_included_duration_are_consistent():
    event = {
        "id": "boho-swing-mini-sessions-2026-07-12",
        "title": "Boho Swing Mini Sessions",
        "date": "2026-07-12",
        "session_length": 20,
        "subtitle": "Perfect for families, maternity, couples, children, or",
        "included": ["⏱ 30-minute photo session", "📸 All original photos included"],
        "total_spots": 12,
    }

    enriched = booking_app._enrich_event_for_landing(event)

    assert enriched["subtitle"].endswith("children.")
    assert "⏱ 20-minute photo session" in enriched["included"]
    assert "⏱ 30-minute photo session" not in enriched["included"]
