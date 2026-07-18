import json
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

import app as booking_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "index_v2.html"
REVIEWS = ROOT / "static" / "reviews.json"
AVATAR_DIR = ROOT / "static" / "images" / "review-avatars"


def test_homepage_review_avatars_are_small_local_webp():
    """Hydrated testimonials must never pull full-size remote review photos."""
    html = TEMPLATE.read_text()
    reviews = json.loads(REVIEWS.read_text())

    assert "i.wfolio.com" not in html
    assert "i.wfolio.com" not in REVIEWS.read_text()

    avatar_paths = {review["avatar"] for review in reviews if review.get("avatar")}
    avatar_paths.update(
        image["src"]
        for image in BeautifulSoup(html, "html.parser").select("img.tmt-avatar")
    )
    assert avatar_paths

    for avatar_path in avatar_paths:
        assert avatar_path.startswith("/static/images/review-avatars/")
        assert avatar_path.endswith(".webp")
        file_path = ROOT / avatar_path.removeprefix("/")
        assert file_path.is_file()
        assert file_path.stat().st_size <= 15 * 1024
        with Image.open(file_path) as image:
            assert image.format == "WEBP"
            assert image.width <= 96
            assert image.height <= 96


def test_homepage_session_cards_defer_width_bounded_media():
    """Every grid card should wait for the shared near-viewport hydrator."""
    html = TEMPLATE.read_text()

    assert "function cardMediaUrl(" in html
    assert "?w=720" in html
    assert "const eagerFirstPhoto = i < 3" not in html
    assert "data-bg=\"{{ event.photos[0] }}" in html
    assert "style=\"background-image:url('{{ event.photos[0] }}')\"" not in html
    assert 'data-bg="${escapeAttr(cardMediaUrl(firstPhoto))}"' in html
    assert "rootMargin: '220px 0px'" in html
    assert "hydrateCardSlides" in html


def test_jpeg_session_card_source_returns_runtime_webp(tmp_path, monkeypatch):
    """A legacy JPEG card URL should return a bounded WebP response at runtime."""
    monkeypatch.setattr(booking_app, "_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    client = booking_app.app.test_client()

    response = client.get("/images/family-ss-11.jpg?w=720")

    assert response.status_code == 200
    assert response.content_type == "image/webp"
    assert response.data[:4] == b"RIFF"
    assert response.data[8:12] == b"WEBP"
    output = tmp_path / "card.webp"
    output.write_bytes(response.data)
    with Image.open(output) as image:
        assert image.format == "WEBP"
        assert image.width <= 720


def test_homepage_img_elements_reserve_layout_space():
    """Literal and hydrated homepage images need dimensions before loading."""
    html = TEMPLATE.read_text()
    literal_images = BeautifulSoup(html, "html.parser").find_all("img")

    assert literal_images
    for image in literal_images:
        assert image.get("width")
        assert image.get("height")

    assert (
        '\'<img class="tmt-avatar" src="\'+safeAvatar+\'" '
        'alt="\'+safeName+\' review photo" width="48" height="48" loading="lazy">\''
        in html
    )
