import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def test_homepage_google_fonts_request_contains_only_rendered_variants(client):
    response = client.get("/")
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    font_link = soup.find(
        "link",
        rel="stylesheet",
        href=lambda value: value and "fonts.googleapis.com/css2" in value,
    )

    assert font_link is not None
    requested_families = parse_qs(urlparse(font_link["href"]).query)["family"]
    assert requested_families == [
        "Inter:wght@400;500;600;700",
        "Playfair Display:ital,wght@0,400;0,600;1,400",
    ]

    glass_css = (ROOT / "static/css/booking-glass.css").read_text()
    assert "--font-d: 'Playfair Display', Georgia, serif;" in glass_css
    assert "--font-h: 'Playfair Display', Georgia, serif;" in glass_css
    assert "--font-b: 'Inter', system-ui, -apple-system, sans-serif;" in glass_css


def test_homepage_open_graph_image_is_share_card_sized_and_lightweight():
    image_path = ROOT / "static/og-image.jpg"

    assert image_path.stat().st_size <= 200_000
    with Image.open(image_path) as image:
        assert image.format == "JPEG"
        assert 1_150 <= image.width <= 1_250
        assert 600 <= image.height <= 660


def test_public_canonicals_and_homepage_local_business_schema_remain_valid(client):
    expected_canonicals = {
        "/": "https://book.pashynskaphoto.com/",
        "/family": "https://book.pashynskaphoto.com/family",
        "/maternity": "https://book.pashynskaphoto.com/maternity",
    }

    for path, expected in expected_canonicals.items():
        response = client.get(path)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        assert soup.find("link", rel="canonical")["href"] == expected

        if path == "/":
            schemas = [
                json.loads(node.string)
                for node in soup.find_all("script", type="application/ld+json")
            ]
            graph_items = [
                item
                for schema in schemas
                if isinstance(schema, dict)
                for item in schema.get("@graph", [])
            ]
            assert any(
                item.get("@type") == "LocalBusiness"
                and item.get("@id") == "https://book.pashynskaphoto.com/#organization"
                for item in graph_items
            )
