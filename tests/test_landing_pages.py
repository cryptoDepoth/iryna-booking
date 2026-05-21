"""
Tests for new landing pages (/wedding, /family, /maternity, /book)
Add to your test suite or nightly cron job.
"""
import pytest
from app import app

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

class TestLandingPages:
    """SEO landing pages for each photography direction."""

    def test_wedding_page_200(self, client):
        r = client.get("/wedding")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "Calgary Wedding Photographer" in html
        assert "schema.org" in html
        assert '"@type": "Service"' in html
        assert '"@type": "FAQPage"' in html
        assert 'rel="canonical"' in html
        assert "wedding" in html.lower()

    def test_family_page_200(self, client):
        r = client.get("/family")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "Calgary Family Photographer" in html
        assert '"@type": "Service"' in html
        assert '"@type": "FAQPage"' in html

    def test_maternity_page_200(self, client):
        r = client.get("/maternity")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "Calgary Maternity Photographer" in html
        assert '"@type": "Service"' in html
        assert '"@type": "FAQPage"' in html

    def test_booking_page_200(self, client):
        r = client.get("/book")
        assert r.status_code == 200
        assert "text/html" in r.content_type
        html = r.data.decode("utf-8")
        assert "Sessions" in html or "session" in html.lower()
        assert "Pashynska" in html

    def test_booking_page_with_type_filter(self, client):
        r = client.get("/book?type=wedding")
        assert r.status_code == 200
        assert "text/html" in r.content_type

    def test_wedding_booking_filter_shows_inquiry_not_unrelated_mini_sessions(self, client):
        """Wedding CTA must not land clients on unrelated mini-session booking cards.

        Wedding/custom work is inquiry-first. If no active wedding event is configured,
        /book?type=wedding should show a wedding-specific inquiry destination instead
        of the default mini-session list.
        """
        r = client.get("/book?type=wedding")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "Wedding inquiry" in html
        assert "Tell Iryna about your wedding" in html
        assert "Lilac Mini Sessions" not in html
        assert "Reserve in 2 min" not in html
        assert "Pay deposit by e-Transfer" not in html

    def test_events_api_still_json(self, client):
        """The /events endpoint must remain JSON for the booking frontend."""
        r = client.get("/events")
        assert r.status_code == 200
        assert "application/json" in r.content_type
        data = r.get_json()
        assert "events" in data

    def test_landing_pages_have_no_broken_links(self, client):
        """All CTA buttons should point to /book, not /events."""
        for path in ["/wedding", "/family", "/maternity"]:
            r = client.get(path)
            html = r.data.decode("utf-8")
            # Should NOT have raw /events?type= links
            assert '/events?' not in html, f"{path} still has /events links"
            # Should have /book links
            assert '/book' in html, f"{path} missing /book links"

    def test_canonical_urls(self, client):
        """Each landing page has correct canonical URL."""
        pages = {
            "/wedding": "https://book.pashynskaphoto.com/wedding",
            "/family": "https://book.pashynskaphoto.com/family",
            "/maternity": "https://book.pashynskaphoto.com/maternity",
        }
        for path, expected in pages.items():
            r = client.get(path)
            html = r.data.decode("utf-8")
            assert expected in html, f"{path} missing canonical {expected}"

    def test_cross_links_between_landings(self, client):
        """Landing pages link to each other."""
        r = client.get("/wedding")
        html = r.data.decode("utf-8")
        assert "/family" in html
        assert "/maternity" in html

    def test_schema_org_localbusiness_present(self, client):
        """Main page still has LocalBusiness schema."""
        r = client.get("/")
        html = r.data.decode("utf-8")
        assert '"@type": "LocalBusiness"' in html
        assert '"@type": "Person"' in html
