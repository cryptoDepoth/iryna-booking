import hashlib
import io
import json
import os
import shutil
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import app as booking_app
import pytest
import yaml


def _headers():
    return {"X-Admin-Key": "test-admin-key"}


def _create_payload(title):
    return {
        "title": title,
        "date": "2026-10-10",
        "start_time": "10:00",
        "end_time": "12:00",
        "session_length": 20,
        "break_length": 10,
        "deposit": 100,
        "full_price": 300,
        "booking_type": "fixed_slots",
        "session_type": "mini",
    }


@pytest.fixture()
def event_admin(tmp_path, monkeypatch):
    events_path = tmp_path / "persistent" / "events.yaml"
    events_path.parent.mkdir()
    events_path.write_text(
        yaml.safe_dump(
            {
                "events": [
                    {
                        "id": "persistence-fixture",
                        "title": "Persistence Fixture",
                        "subtitle": "Sentinel event content",
                        "date": "2026-12-05",
                        "start_time": "10:00",
                        "end_time": "12:00",
                        "session_length": 20,
                        "break_length": 10,
                        "slot_interval": 30,
                        "deposit": 95,
                        "full_price": 250,
                        "booking_type": "fixed_slots",
                        "session_type": "mini",
                        "status": "active",
                        "featured": True,
                        "location": "Calgary sentinel location",
                        "included": ["Sentinel inclusion"],
                        "photos": [
                            "/images/persistence-existing-one.webp",
                            "/images/persistence-existing-two.webp",
                        ],
                        "persistence_sentinel": {
                            "nested": ["preserve", {"value": 7}],
                        },
                    },
                    {
                        "id": "persistence-sibling",
                        "title": "Untouched Sibling Event",
                        "date": "2026-12-06",
                        "start_time": "14:00",
                        "end_time": "15:00",
                        "session_length": 30,
                        "break_length": 0,
                        "slot_interval": 30,
                        "deposit": 80,
                        "full_price": 210,
                        "booking_type": "fixed_slots",
                        "session_type": "mini",
                        "status": "upcoming",
                        "photos": ["/images/persistence-sibling.webp"],
                        "persistence_sentinel": {
                            "nested": ["sibling", {"value": 13}],
                        },
                    },
                ],
                "settings": {
                    "photographer_email": "persistence@example.test",
                    "persistence_sentinel": {
                        "nested": ["settings", {"value": 11}],
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "bookings.db"
    monkeypatch.setattr(booking_app, "DB_PATH", str(db_path))
    monkeypatch.setattr(booking_app, "_EVENTS_PATH", str(events_path))
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(events_path))
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    booking_app.init_db()
    events, settings = booking_app._load_events()
    monkeypatch.setattr(booking_app, "EVENTS", events)
    monkeypatch.setattr(booking_app, "SETTINGS", settings)

    with booking_app.app.test_client() as client:
        yield client, events_path


def _assert_atomic_revision(events_path, previous_inode, previous_lastmod=None):
    assert events_path.stat().st_ino != previous_inode
    state_path = Path(f"{events_path}.revision.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    persisted = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    normalized = yaml.safe_dump(
        persisted,
        allow_unicode=True,
        sort_keys=True,
    ).encode("utf-8")
    assert state["sha256"] == hashlib.sha256(normalized).hexdigest()
    datetime.fromisoformat(state["lastmod"]).date()
    if previous_lastmod is not None:
        assert state["lastmod"] >= previous_lastmod
    return events_path.stat().st_ino, state["lastmod"]


def _seed_revision_floor(events_path, monkeypatch):
    revision_floor = "2026-07-18"
    persisted = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    normalized = yaml.safe_dump(
        persisted,
        allow_unicode=True,
        sort_keys=True,
    ).encode("utf-8")
    Path(f"{events_path}.revision.json").write_text(
        json.dumps({
            "lastmod": revision_floor,
            "sha256": hashlib.sha256(normalized).hexdigest(),
        }),
        encoding="utf-8",
    )

    original_getmtime = booking_app.os.path.getmtime
    earlier_mtime = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()

    def earlier_events_mtime(path):
        if os.path.abspath(path) == os.path.abspath(events_path):
            return earlier_mtime
        return original_getmtime(path)

    monkeypatch.setattr(
        booking_app.os.path,
        "getmtime",
        earlier_events_mtime,
    )
    return revision_floor


def _assert_complete_atomic_revision(
    events_path,
    expected_document,
    previous_inode,
    previous_lastmod=None,
):
    persisted = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    assert persisted == expected_document
    return _assert_atomic_revision(
        events_path,
        previous_inode,
        previous_lastmod,
    )


def _document_event(document, event_id):
    return next(event for event in document["events"] if event["id"] == event_id)


def test_admin_event_mutations_use_atomic_replacement_and_sync_revision(event_admin):
    client, events_path = event_admin
    inode = events_path.stat().st_ino
    lastmod = None

    created = client.post(
        "/admin/events/create",
        headers=_headers(),
        json=_create_payload("Atomic Persistence"),
    )
    assert created.status_code == 200
    event_id = created.get_json()["event_id"]
    inode, lastmod = _assert_atomic_revision(events_path, inode, lastmod)

    updated = client.post(
        f"/admin/events/{event_id}/update",
        headers=_headers(),
        json={"deposit": 125, "full_price": 325},
    )
    assert updated.status_code == 200
    inode, lastmod = _assert_atomic_revision(events_path, inode, lastmod)

    meta_updated = client.post(
        f"/admin/events/{event_id}/update-meta",
        headers=_headers(),
        json={"subtitle": "Revision metadata remains synchronized"},
    )
    assert meta_updated.status_code == 200
    inode, lastmod = _assert_atomic_revision(events_path, inode, lastmod)

    duplicated = client.post(
        f"/admin/events/{event_id}/duplicate",
        headers=_headers(),
    )
    assert duplicated.status_code == 200
    duplicate_id = duplicated.get_json()["new_event_id"]
    inode, lastmod = _assert_atomic_revision(events_path, inode, lastmod)

    deleted_duplicate = client.post(
        f"/admin/events/{duplicate_id}/delete",
        headers=_headers(),
        json={},
    )
    assert deleted_duplicate.status_code == 200
    inode, lastmod = _assert_atomic_revision(events_path, inode, lastmod)

    deleted_created = client.post(
        f"/admin/events/{event_id}/delete",
        headers=_headers(),
        json={},
    )
    assert deleted_created.status_code == 200
    _assert_atomic_revision(events_path, inode, lastmod)


def test_photo_mutation_endpoints_preserve_complete_yaml_and_revision(
    event_admin, monkeypatch, tmp_path
):
    client, events_path = event_admin
    photos_dir = tmp_path / "photos"
    bundled_dir = tmp_path / "bundled"
    photos_dir.mkdir()
    bundled_dir.mkdir()
    monkeypatch.setattr(booking_app, "PHOTOS_DIR", str(photos_dir))
    monkeypatch.setattr(booking_app, "_BUNDLED_IMAGES_DIR", str(bundled_dir))

    generated_urls = iter(
        f"/images/persistence-photo-{index}.webp" for index in range(1, 5)
    )

    def save_test_photo(_event_id, _file_storage):
        url = next(generated_urls)
        saved_path = photos_dir / os.path.basename(url)
        saved_path.write_bytes(b"complete-test-webp")
        return url, str(saved_path), {
            "width": 1200,
            "height": 800,
            "bytes": saved_path.stat().st_size,
        }

    monkeypatch.setattr(
        booking_app,
        "_save_optimized_admin_photo",
        save_test_photo,
    )

    expected = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    event_id = expected["events"][0]["id"]
    expected_event = _document_event(expected, event_id)
    original_photo_count = len(expected_event.get("photos") or [])
    inode = events_path.stat().st_ino
    lastmod = _seed_revision_floor(events_path, monkeypatch)

    added = client.post(
        f"/admin/photos/{event_id}/upload",
        headers=_headers(),
        data={"photo": (io.BytesIO(b"photo-add"), "add.jpg")},
        content_type="multipart/form-data",
    )
    assert added.status_code == 200, added.get_data(as_text=True)
    added_url = added.get_json()["url"]
    expected_event.setdefault("photos", []).append(added_url)
    inode, lastmod = _assert_complete_atomic_revision(
        events_path, expected, inode, lastmod
    )

    replaced = client.post(
        f"/admin/photos/{event_id}/upload",
        headers=_headers(),
        data={
            "slot_index": str(original_photo_count),
            "photo": (io.BytesIO(b"photo-replace"), "replace.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert replaced.status_code == 200, replaced.get_data(as_text=True)
    replacement_url = replaced.get_json()["url"]
    expected_event["photos"][original_photo_count] = replacement_url
    inode, lastmod = _assert_complete_atomic_revision(
        events_path, expected, inode, lastmod
    )

    batch = client.post(
        f"/admin/photos/{event_id}/upload-batch",
        headers=_headers(),
        data={
            "photos": [
                (io.BytesIO(b"photo-batch-one"), "batch-one.jpg"),
                (io.BytesIO(b"photo-batch-two"), "batch-two.jpg"),
            ]
        },
        content_type="multipart/form-data",
    )
    assert batch.status_code == 200, batch.get_data(as_text=True)
    expected_event["photos"].extend(batch.get_json()["urls"])
    inode, lastmod = _assert_complete_atomic_revision(
        events_path, expected, inode, lastmod
    )

    deleted = client.post(
        f"/admin/photos/{event_id}/delete",
        headers=_headers(),
        json={"slot_index": original_photo_count},
    )
    assert deleted.status_code == 200, deleted.get_data(as_text=True)
    expected_event["photos"].pop(original_photo_count)
    _assert_complete_atomic_revision(events_path, expected, inode, lastmod)


def test_custom_slot_mutations_preserve_complete_yaml_and_revision(
    event_admin, monkeypatch
):
    client, events_path = event_admin
    expected = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    event_id = expected["events"][0]["id"]
    expected_event = _document_event(expected, event_id)
    original_event = deepcopy(expected_event)
    custom_time = "23:43"
    assert all(
        str(slot.get("time")) != custom_time
        for slot in expected_event.get("custom_slots") or []
        if isinstance(slot, dict)
    )
    inode = events_path.stat().st_ino
    lastmod = _seed_revision_floor(events_path, monkeypatch)

    added = client.post(
        f"/admin/api/event/{event_id}/custom-slot",
        headers=_headers(),
        json={
            "time": custom_time,
            "session_length": 15,
            "break_length": 10,
            "allow_back_to_back": True,
        },
    )
    assert added.status_code == 200, added.get_data(as_text=True)
    custom_slot = {
        "time": custom_time,
        "session_length": 15,
        "break_length": 0,
        "allow_back_to_back": True,
    }
    expected_slots = list(expected_event.get("custom_slots") or [])
    expected_slots.append(custom_slot)
    expected_slots.sort(
        key=lambda item: booking_app._time_minutes(item.get("time")) or 0
    )
    expected_event["custom_slots"] = expected_slots
    inode, lastmod = _assert_complete_atomic_revision(
        events_path, expected, inode, lastmod
    )

    removed = client.delete(
        f"/admin/api/event/{event_id}/custom-slot",
        headers=_headers(),
        json={"time": custom_time},
    )
    assert removed.status_code == 200, removed.get_data(as_text=True)
    expected["events"][0] = original_event
    _assert_complete_atomic_revision(events_path, expected, inode, lastmod)


def test_private_session_writer_preserves_complete_yaml_and_revision(
    event_admin, monkeypatch
):
    client, events_path = event_admin
    monkeypatch.setattr(booking_app, "_notify_admin", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(booking_app, "sync_client", lambda *_args, **_kwargs: None)

    expected = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    inode = events_path.stat().st_ino
    lastmod = _seed_revision_floor(events_path, monkeypatch)

    created = client.post(
        "/admin/api/private-session",
        headers=_headers(),
        json={
            "date": "2026-11-12",
            "start_time": "13:15",
            "end_time": "14:30",
            "client_name": "Persistence Client",
            "email": "",
            "instagram": "@persistence.client",
            "price": 410,
            "deposit": 120,
            "photos": 33,
            "already_paid": True,
            "send_email": False,
        },
    )
    assert created.status_code == 200, created.get_data(as_text=True)
    body = created.get_json()
    expected_event = {
        "id": body["event_id"],
        "title": "Individual Photoshoot — Persistence Client",
        "subtitle": "Individual photoshoot (hidden from public website)",
        "date": "2026-11-12",
        "start_time": "13:15",
        "end_time": "14:30",
        "session_length": 75,
        "break_length": 0,
        "slot_interval": 75,
        "deposit": 120.0,
        "full_price": 410.0,
        "location": "Calgary — exact spot sent after booking",
        "session_type": "private",
        "edited_photos": 33,
        "featured": False,
        "hidden": True,
        "included": [
            "75 min individual photoshoot",
            "33 professionally edited photos",
            "All original photos included",
        ],
        "photos": ["/images/placeholder.jpg"],
    }
    expected["events"].append(expected_event)

    _assert_complete_atomic_revision(
        events_path,
        expected,
        inode,
        lastmod,
    )


def test_concurrent_sitemap_read_waits_for_complete_admin_event_write(
    event_admin, monkeypatch
):
    _client, events_path = event_admin
    old_sha256 = booking_app._normalized_yaml_sha256(str(events_path))
    write_started = threading.Event()
    release_write = threading.Event()
    reader_done = threading.Event()
    writer_result = {}
    reader_result = {}
    original_dump = booking_app.yaml.dump

    def slow_dump(data, stream=None, *args, **kwargs):
        if stream is None:
            return original_dump(data, stream, *args, **kwargs)
        rendered = original_dump(data, None, *args, **kwargs)
        midpoint = max(1, len(rendered) // 2)
        stream.write(rendered[:midpoint])
        stream.flush()
        write_started.set()
        if not release_write.wait(3):
            raise RuntimeError("test timed out waiting to finish YAML write")
        stream.write(rendered[midpoint:])
        return None

    monkeypatch.setattr(booking_app.yaml, "dump", slow_dump)

    def write_event():
        with booking_app.app.test_client() as client:
            response = client.post(
                "/admin/events/create",
                headers=_headers(),
                json=_create_payload("Concurrent Atomic Write"),
            )
            writer_result["status"] = response.status_code

    def read_sitemap():
        try:
            with booking_app.app.test_client() as client:
                response = client.get("/sitemap.xml")
                reader_result["status"] = response.status_code
                reader_result["body"] = response.get_data(as_text=True)
        except Exception as exc:  # pragma: no cover - captured for assertion
            reader_result["error"] = exc
        finally:
            reader_done.set()

    writer = threading.Thread(target=write_event)
    reader = threading.Thread(target=read_sitemap)
    writer.start()
    assert write_started.wait(2)
    reader.start()

    try:
        assert not reader_done.wait(0.1)
    finally:
        release_write.set()

    writer.join(3)
    reader.join(3)
    assert not writer.is_alive()
    assert not reader.is_alive()
    assert writer_result == {"status": 200}
    assert reader_result["status"] == 200
    new_sha256 = booking_app._normalized_yaml_sha256(str(events_path))
    revision = json.loads(
        Path(f"{events_path}.revision.json").read_text(encoding="utf-8")
    )
    assert new_sha256 != old_sha256
    assert revision["sha256"] == new_sha256
    assert f"<lastmod>{revision['lastmod']}</lastmod>" in reader_result["body"]


def test_concurrent_admin_event_creates_serialize_without_lost_updates(
    event_admin, monkeypatch
):
    _client, events_path = event_admin
    first_write_started = threading.Event()
    release_first_write = threading.Event()
    second_done = threading.Event()
    results = {}
    original_dump = booking_app.yaml.dump

    def pause_first_dump(data, stream=None, *args, **kwargs):
        if stream is None or first_write_started.is_set():
            return original_dump(data, stream, *args, **kwargs)
        rendered = original_dump(data, None, *args, **kwargs)
        midpoint = max(1, len(rendered) // 2)
        stream.write(rendered[:midpoint])
        stream.flush()
        first_write_started.set()
        if not release_first_write.wait(3):
            raise RuntimeError("test timed out waiting to finish first YAML write")
        stream.write(rendered[midpoint:])
        return None

    monkeypatch.setattr(booking_app.yaml, "dump", pause_first_dump)

    def create_event(name, done=None):
        with booking_app.app.test_client() as client:
            response = client.post(
                "/admin/events/create",
                headers=_headers(),
                json=_create_payload(name),
            )
            results[name] = response.status_code
        if done:
            done.set()

    first = threading.Thread(target=create_event, args=("Concurrent First",))
    second = threading.Thread(
        target=create_event,
        args=("Concurrent Second", second_done),
    )
    first.start()
    assert first_write_started.wait(2)
    second.start()

    try:
        assert not second_done.wait(0.1)
    finally:
        release_first_write.set()

    first.join(3)
    second.join(3)
    assert not first.is_alive()
    assert not second.is_alive()
    assert results == {
        "Concurrent First": 200,
        "Concurrent Second": 200,
    }
    saved_titles = {
        event["title"]
        for event in yaml.safe_load(
            events_path.read_text(encoding="utf-8")
        )["events"]
    }
    assert {"Concurrent First", "Concurrent Second"} <= saved_titles


def test_failed_revision_sync_rolls_back_event_yaml(event_admin, monkeypatch):
    _client, events_path = event_admin
    original_yaml = events_path.read_bytes()
    revision_path = Path(f"{events_path}.revision.json")
    original_revision = (
        revision_path.read_bytes() if revision_path.exists() else None
    )
    data = booking_app._load_events_yaml_doc()
    data.setdefault("events", []).append({
        "id": "rollback-test",
        "title": "Rollback Test",
        "date": "2026-10-11",
    })

    def fail_revision_sync(*_args, **_kwargs):
        raise OSError("simulated sidecar failure")

    monkeypatch.setattr(
        booking_app,
        "_sync_events_revision_state",
        fail_revision_sync,
    )

    with pytest.raises(OSError, match="simulated sidecar failure"):
        booking_app._write_events_yaml_doc(data)

    assert events_path.read_bytes() == original_yaml
    if original_revision is None:
        assert not revision_path.exists()
    else:
        assert revision_path.read_bytes() == original_revision


def test_event_revision_does_not_move_backward_after_bundled_content_revert(
    tmp_path, monkeypatch
):
    bundled_path = Path(booking_app.app.root_path) / "events.yaml"
    events_path = tmp_path / "events.yaml"
    shutil.copy2(bundled_path, events_path)
    monkeypatch.setattr(booking_app, "_EVENTS_PATH", str(events_path))
    monkeypatch.setattr(booking_app, "EVENTS_YAML_PATH", str(events_path))

    revisions = booking_app._load_sitemap_content_revisions()
    assert booking_app._events_content_lastmod(revisions) == revisions["events.yaml"]["lastmod"]

    changed = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    changed.setdefault("settings", {})["revision_revert_test"] = True
    events_path.write_text(
        yaml.safe_dump(changed, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    changed_mtime = datetime(2026, 7, 17, tzinfo=timezone.utc).timestamp()
    os.utime(events_path, (changed_mtime, changed_mtime))
    changed_lastmod = booking_app._events_content_lastmod(revisions)
    assert changed_lastmod == "2026-07-17"

    shutil.copyfile(bundled_path, events_path)
    reverted_mtime = datetime(2026, 7, 18, tzinfo=timezone.utc).timestamp()
    os.utime(events_path, (reverted_mtime, reverted_mtime))
    reverted_lastmod = booking_app._events_content_lastmod(revisions)

    state = json.loads(
        Path(f"{events_path}.revision.json").read_text(encoding="utf-8")
    )
    assert reverted_lastmod >= changed_lastmod
    assert state == {
        "lastmod": reverted_lastmod,
        "sha256": revisions["events.yaml"]["sha256"],
    }
