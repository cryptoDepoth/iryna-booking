import io
import os

import pytest
import yaml
from PIL import Image

import app as booking_app


def _headers():
    return {"X-Admin-Key": "test-admin-key"}


def _image_upload(name="photo.jpg", size=(2400, 1800), color=(205, 132, 112), fmt="JPEG"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt, quality=95)
    buf.seek(0)
    return buf, name


@pytest.fixture()
def photo_admin_client(tmp_path, monkeypatch):
    original_events_path = booking_app._EVENTS_PATH
    original_events_yaml_path = booking_app.EVENTS_YAML_PATH
    original_photos_dir = booking_app.PHOTOS_DIR
    original_bundled_dir = booking_app._BUNDLED_IMAGES_DIR
    original_admin_key = booking_app.ADMIN_KEY
    original_admin_password = booking_app.ADMIN_PASSWORD

    events_path = tmp_path / "events.yaml"
    photos_dir = tmp_path / "images"
    bundled_dir = tmp_path / "bundled-images"
    photos_dir.mkdir()
    bundled_dir.mkdir()
    (photos_dir / "existing.webp").write_bytes(b"old")
    events_path.write_text(yaml.safe_dump({
        "events": [{
            "id": "qa-photo",
            "title": "QA Photo Session",
            "date": "2026-07-20",
            "start_time": "10:00",
            "end_time": "11:00",
            "session_length": 20,
            "break_length": 10,
            "slot_interval": 30,
            "deposit": 100,
            "full_price": 200,
            "status": "active",
            "photos": ["/images/existing.webp"],
        }],
        "settings": {},
    }, sort_keys=False))

    monkeypatch.setattr(booking_app, "_EVENTS_PATH", str(events_path), raising=False)
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(events_path), raising=False)
    monkeypatch.setattr(booking_app, "PHOTOS_DIR", str(photos_dir), raising=False)
    monkeypatch.setattr(booking_app, "_BUNDLED_IMAGES_DIR", str(bundled_dir), raising=False)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key", raising=False)
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key", raising=False)
    booking_app._reload_events_globals()
    booking_app.app.config["TESTING"] = True

    with booking_app.app.test_client() as client:
        yield client, events_path, photos_dir

    booking_app._EVENTS_PATH = original_events_path
    booking_app.EVENTS_YAML_PATH = original_events_yaml_path
    booking_app.PHOTOS_DIR = original_photos_dir
    booking_app._BUNDLED_IMAGES_DIR = original_bundled_dir
    booking_app.ADMIN_KEY = original_admin_key
    booking_app.ADMIN_PASSWORD = original_admin_password
    booking_app._reload_events_globals()


def _event_photos(events_path):
    return yaml.safe_load(events_path.read_text())["events"][0]["photos"]


def test_batch_upload_accepts_up_to_five_and_optimizes_to_webp(photo_admin_client):
    client, events_path, photos_dir = photo_admin_client
    resp = client.post(
        "/admin/photos/qa-photo/upload-batch",
        headers=_headers(),
        data={
            "photos": [
                _image_upload("one.jpg", color=(210, 120, 120)),
                _image_upload("two.png", color=(120, 160, 210), fmt="PNG"),
            ]
        },
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is True
    assert body["count"] == 2
    assert all(url.endswith(".webp") for url in body["urls"])

    photos = _event_photos(events_path)
    assert photos == ["/images/existing.webp", *body["urls"]]
    assert booking_app.get_event_by_id("qa-photo")["photos"] == photos

    for url in body["urls"]:
        path = photos_dir / os.path.basename(url)
        assert path.exists()
        assert path.stat().st_size < 600_000
        with Image.open(path) as img:
            assert img.format == "WEBP"
            assert max(img.size) <= booking_app.PHOTO_MAX_DIMENSION


def test_batch_upload_rejects_more_than_five_without_writing_files(photo_admin_client):
    client, events_path, photos_dir = photo_admin_client
    before = sorted(p.name for p in photos_dir.iterdir())
    resp = client.post(
        "/admin/photos/qa-photo/upload-batch",
        headers=_headers(),
        data={"photos": [_image_upload(f"{i}.jpg") for i in range(6)]},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert "up to 5" in resp.get_json()["error"]
    assert _event_photos(events_path) == ["/images/existing.webp"]
    assert sorted(p.name for p in photos_dir.iterdir()) == before


def test_replace_photo_validates_slot_before_saving(photo_admin_client):
    client, events_path, photos_dir = photo_admin_client
    before = sorted(p.name for p in photos_dir.iterdir())
    resp = client.post(
        "/admin/photos/qa-photo/upload",
        headers=_headers(),
        data={"slot_index": "99", "photo": _image_upload("replace.jpg")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Photo slot not found"
    assert _event_photos(events_path) == ["/images/existing.webp"]
    assert sorted(p.name for p in photos_dir.iterdir()) == before


def test_replace_photo_optimizes_new_file_and_removes_old_file(photo_admin_client):
    client, events_path, photos_dir = photo_admin_client
    resp = client.post(
        "/admin/photos/qa-photo/upload",
        headers=_headers(),
        data={"slot_index": "0", "photo": _image_upload("replace.jpg")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    photos = _event_photos(events_path)
    assert photos == [body["url"]]
    assert not (photos_dir / "existing.webp").exists()
    assert (photos_dir / os.path.basename(body["url"])).exists()


def test_images_route_uses_long_cache_header(photo_admin_client):
    client, _events_path, _photos_dir = photo_admin_client
    resp = client.get("/images/existing.webp")

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_oversized_photo_batch_returns_json_error(photo_admin_client):
    client, _events_path, _photos_dir = photo_admin_client
    old_limit = booking_app.app.config["MAX_CONTENT_LENGTH"]
    booking_app.app.config["MAX_CONTENT_LENGTH"] = 100
    try:
        resp = client.post(
            "/admin/photos/qa-photo/upload-batch",
            headers=_headers(),
            data={"photos": [_image_upload("too-big.jpg", size=(300, 300))]},
            content_type="multipart/form-data",
        )
    finally:
        booking_app.app.config["MAX_CONTENT_LENGTH"] = old_limit

    assert resp.status_code == 413
    assert resp.is_json
    assert "under 30 MB" in resp.get_json()["error"]
