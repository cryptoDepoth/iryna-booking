"""Regression tests for low-risk conversion and safety improvements."""

from app import app


def test_landing_pages_show_real_testimonials():
    app.config["TESTING"] = True
    with app.test_client() as client:
        for path in ("/wedding", "/family", "/maternity"):
            response = client.get(path)
            assert response.status_code == 200
            html = response.data.decode("utf-8")
            assert "What Calgary clients say" in html
            assert "testimonial-card" in html
            assert "Calgary" in html


def test_events_api_is_not_indexed_by_search_engines():
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/events")
        assert response.status_code == 200
        assert response.headers.get("X-Robots-Tag") == "noindex, nofollow"
