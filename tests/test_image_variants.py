from pathlib import Path

from PIL import Image

import app as booking_app


def test_responsive_image_variant_is_bounded_cached_webp(tmp_path, monkeypatch):
    source = tmp_path / "portrait.jpg"
    Image.new("RGB", (1200, 1800), (190, 130, 110)).save(source, "JPEG", quality=95)
    cache = tmp_path / "cache"
    monkeypatch.setattr(booking_app, "_IMAGE_CACHE_DIR", str(cache))

    variant = booking_app._optimized_image_cache_path(str(source), target_width=700)

    assert variant
    assert ".w720.webp" in variant
    assert Path(variant).is_file()
    with Image.open(variant) as image:
        assert image.format == "WEBP"
        assert image.width <= 720
        assert image.height <= 1440

    # Repeated requests must reuse the stable cache file.
    assert booking_app._optimized_image_cache_path(str(source), target_width=700) == variant
