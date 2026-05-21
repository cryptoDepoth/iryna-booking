"""Safe A/B booking-flow scaffold tests.

The experiment must be conservative: default users stay on the current proven
3-step flow, and the one-step variant is only enabled by explicit query/cookie
or environment flag. No test here changes booking semantics.
"""

import importlib

import app as booking_app


def test_booking_flow_variant_defaults_to_control_without_flag(monkeypatch):
    monkeypatch.delenv("BOOKING_FLOW_EXPERIMENT", raising=False)
    assert booking_app._select_booking_flow_variant(None, None) == "control"


def test_booking_flow_variant_can_be_forced_by_query_when_experiment_enabled(monkeypatch):
    monkeypatch.setenv("BOOKING_FLOW_EXPERIMENT", "1")
    assert booking_app._select_booking_flow_variant("one_step", None) == "one_step"
    assert booking_app._select_booking_flow_variant("control", "one_step") == "control"


def test_booking_flow_variant_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("BOOKING_FLOW_EXPERIMENT", "1")
    assert booking_app._select_booking_flow_variant("bad", "also-bad") == "control"


def test_homepage_exposes_control_variant_by_default(monkeypatch):
    monkeypatch.delenv("BOOKING_FLOW_EXPERIMENT", raising=False)
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as client:
        response = client.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "const BOOKING_FLOW_VARIANT = \"control\"" in html
    assert "data-ab-booking-flow=\"control\"" in html


def test_homepage_can_render_one_step_variant_when_explicitly_forced(monkeypatch):
    monkeypatch.setenv("BOOKING_FLOW_EXPERIMENT", "1")
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as client:
        response = client.get("/?flow=one_step")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "const BOOKING_FLOW_VARIANT = \"one_step\"" in html
    assert "data-ab-booking-flow=\"one_step\"" in html
    assert "One-step booking preview" in html
