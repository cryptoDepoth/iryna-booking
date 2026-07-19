import json
import os
import shutil
import threading
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
    shutil.copy2(Path(booking_app.app.root_path) / "events.yaml", events_path)

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
    assert state["sha256"] == booking_app._normalized_yaml_sha256(str(events_path))
    datetime.fromisoformat(state["lastmod"]).date()
    if previous_lastmod is not None:
        assert state["lastmod"] >= previous_lastmod
    return events_path.stat().st_ino, state["lastmod"]


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
