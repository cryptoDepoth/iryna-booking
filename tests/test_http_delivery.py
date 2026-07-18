import gzip
import io

import brotli
from PIL import Image

import app as booking_app


def _write_image(path, fmt, color):
    Image.new("RGB", (48, 32), color).save(path, fmt)


def test_images_route_reports_mimetype_from_returned_bytes(tmp_path, monkeypatch):
    photos_dir = tmp_path / "images"
    bundled_dir = tmp_path / "bundled"
    cache_dir = tmp_path / "cache"
    photos_dir.mkdir()
    bundled_dir.mkdir()

    _write_image(photos_dir / "jpeg-bytes.webp", "JPEG", (190, 120, 90))
    _write_image(photos_dir / "webp-bytes.jpg", "WEBP", (90, 150, 190))
    _write_image(photos_dir / "variant-source.jpg", "JPEG", (140, 100, 180))

    monkeypatch.setattr(booking_app, "PHOTOS_DIR", str(photos_dir))
    monkeypatch.setattr(booking_app, "_BUNDLED_IMAGES_DIR", str(bundled_dir))
    monkeypatch.setattr(booking_app, "_IMAGE_CACHE_DIR", str(cache_dir))

    with booking_app.app.test_client() as client:
        jpeg_response = client.get("/images/jpeg-bytes.webp")
        webp_response = client.get("/images/webp-bytes.jpg")
        variant_response = client.get("/images/variant-source.jpg?w=480")

    assert jpeg_response.status_code == 200
    assert jpeg_response.mimetype == "image/jpeg"
    assert jpeg_response.data.startswith(b"\xff\xd8\xff")

    assert webp_response.status_code == 200
    assert webp_response.mimetype == "image/webp"
    assert webp_response.data[:4] == b"RIFF"
    assert webp_response.data[8:12] == b"WEBP"

    assert variant_response.status_code == 200
    assert variant_response.mimetype == "image/webp"
    assert variant_response.data[:4] == b"RIFF"
    assert variant_response.data[8:12] == b"WEBP"


def test_homepage_supports_brotli_and_gzip_without_losing_security_headers():
    with booking_app.app.test_client() as client:
        identity_response = client.get("/", headers={"Accept-Encoding": "identity"})
        brotli_response = client.get("/", headers={"Accept-Encoding": "br"})
        gzip_response = client.get("/", headers={"Accept-Encoding": "gzip"})
        secure_response = client.get(
            "/",
            headers={
                "Accept-Encoding": "br",
                "X-Forwarded-Proto": "https",
            },
        )

    assert brotli_response.headers["Content-Encoding"] == "br"
    assert "accept-encoding" in brotli_response.headers["Vary"].lower()
    assert len(brotli_response.data) <= 80 * 1024
    assert brotli.decompress(brotli_response.data) == identity_response.data

    assert gzip_response.headers["Content-Encoding"] == "gzip"
    assert "accept-encoding" in gzip_response.headers["Vary"].lower()
    assert gzip.decompress(gzip_response.data) == identity_response.data

    for header in (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    ):
        assert brotli_response.headers.get(header) == identity_response.headers.get(header)
    assert secure_response.headers["Strict-Transport-Security"].startswith("max-age=")


def test_only_versioned_static_assets_receive_immutable_caching():
    with booking_app.app.test_client() as client:
        css_response = client.get("/static/css/booking-glass.css?v=glass-1")
        logo_response = client.get(
            "/static/brand/pashynska-logo-wfolio-dark.png?v=dark-20260518"
        )
        unversioned_response = client.get("/static/css/booking-glass.css")
        html_response = client.get("/", headers={"Accept-Encoding": "identity"})

    for response in (css_response, logo_response):
        cache_control = response.headers.get("Cache-Control", "")
        assert "public" in cache_control
        assert "max-age=31536000" in cache_control
        assert "immutable" in cache_control

    assert "immutable" not in unversioned_response.headers.get("Cache-Control", "")
    assert "immutable" not in html_response.headers.get("Cache-Control", "")


def test_versioned_static_conditional_304_retains_immutable_caching():
    """Browser revalidation must not downgrade the versioned asset cache policy."""
    path = "/static/brand/pashynska-logo-wfolio-dark.png?v=dark-20260518"
    with booking_app.app.test_client() as client:
        initial_response = client.get(path)
        conditional_response = client.get(
            path,
            headers={"If-None-Match": initial_response.headers["ETag"]},
        )

    assert initial_response.status_code == 200
    assert conditional_response.status_code == 304
    assert conditional_response.headers["Cache-Control"] == (
        "public, max-age=31536000, immutable"
    )


def test_public_html_requires_revalidation():
    """Public HTML must not become a stale shared cache entry."""
    with booking_app.app.test_client() as client:
        response = client.get("/", headers={"Accept-Encoding": "identity"})

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"


def test_conventional_favicon_returns_a_valid_image():
    with booking_app.app.test_client() as client:
        response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data
    with Image.open(io.BytesIO(response.data)) as image:
        assert image.format == "PNG"
        assert image.width > 0
        assert image.height > 0
