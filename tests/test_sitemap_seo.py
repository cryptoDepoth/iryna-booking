import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import app as booking_app
import pytest
import yaml


PUBLIC_CANONICAL_URLS = {
    'https://book.pashynskaphoto.com/',
    'https://book.pashynskaphoto.com/book',
    'https://book.pashynskaphoto.com/wedding',
    'https://book.pashynskaphoto.com/family',
    'https://book.pashynskaphoto.com/maternity',
    'https://book.pashynskaphoto.com/privacy',
}
PUBLIC_SITEMAP_METADATA = {
    'https://book.pashynskaphoto.com/': ('daily', '1.0'),
    'https://book.pashynskaphoto.com/book': ('daily', '0.9'),
    'https://book.pashynskaphoto.com/wedding': ('weekly', '0.8'),
    'https://book.pashynskaphoto.com/family': ('weekly', '0.8'),
    'https://book.pashynskaphoto.com/maternity': ('weekly', '0.8'),
    'https://book.pashynskaphoto.com/privacy': ('monthly', '0.3'),
}
SITEMAP_NS = {'sitemap': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
BUNDLED_SITEMAP_SOURCES = (
    'templates/index_v2.html',
    'static/css/booking-glass.css',
    'static/reviews.json',
    'static/og-image.jpg',
    'templates/events_landing.html',
    'templates/landing_wedding_v5.html',
    'templates/landing_family_v2.html',
    'templates/landing_maternity_v2.html',
    'templates/privacy.html',
)
REVISION_MANIFEST = 'sitemap_content_revisions.json'


def _sitemap_entries(response):
    root = ElementTree.fromstring(response.data)
    return root.findall('sitemap:url', SITEMAP_NS)


def _lastmods_by_url(response):
    return {
        entry.findtext('sitemap:loc', namespaces=SITEMAP_NS):
        entry.findtext('sitemap:lastmod', namespaces=SITEMAP_NS)
        for entry in _sitemap_entries(response)
    }


def _copy_checkout(source_root, destination, mtime):
    for relative_path in BUNDLED_SITEMAP_SOURCES:
        source = source_root / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        os.utime(target, (mtime, mtime))

    revision_manifest = source_root / REVISION_MANIFEST
    shutil.copy2(revision_manifest, destination / revision_manifest.name)
    os.utime(destination / revision_manifest.name, (mtime, mtime))


@pytest.fixture
def sitemap_events_path(tmp_path, monkeypatch):
    events_path = tmp_path / 'persistent' / 'events.yaml'
    events_path.parent.mkdir()
    shutil.copy2(Path(booking_app.app.root_path) / 'events.yaml', events_path)
    monkeypatch.setattr(booking_app, '_EVENTS_PATH', str(events_path))
    monkeypatch.setattr(booking_app, 'EVENTS_YAML_PATH', str(events_path))
    return events_path


def test_sitemap_revision_manifest_matches_bundled_content():
    root = Path(booking_app.app.root_path)
    revisions = json.loads((root / REVISION_MANIFEST).read_text(encoding='utf-8'))

    assert set(revisions) == {*BUNDLED_SITEMAP_SOURCES, 'events.yaml'}
    for source_path, revision in revisions.items():
        source = root / source_path
        if revision.get('digest_format') == 'normalized-yaml':
            data = yaml.safe_load(source.read_text(encoding='utf-8'))
            content = yaml.safe_dump(
                data,
                allow_unicode=True,
                sort_keys=True,
            ).encode('utf-8')
        else:
            content = source.read_bytes()
        revision_date = datetime.fromisoformat(revision['lastmod']).date()

        assert revision['sha256'] == hashlib.sha256(content).hexdigest()
        assert revision_date <= datetime.now(timezone.utc).date()


def test_public_sitemap_has_only_canonical_urls_and_meaningful_lastmods(
    client, sitemap_events_path
):
    resp = client.get('/sitemap.xml')
    entries = _sitemap_entries(resp)
    urls = {
        entry.findtext('sitemap:loc', namespaces=SITEMAP_NS)
        for entry in entries
    }
    lastmods = [
        entry.findtext('sitemap:lastmod', namespaces=SITEMAP_NS)
        for entry in entries
    ]
    metadata = {
        entry.findtext('sitemap:loc', namespaces=SITEMAP_NS): (
            entry.findtext('sitemap:changefreq', namespaces=SITEMAP_NS),
            entry.findtext('sitemap:priority', namespaces=SITEMAP_NS),
        )
        for entry in entries
    }
    today = datetime.now(timezone.utc).date()

    assert resp.status_code == 200
    assert resp.mimetype == 'application/xml'
    assert urls == PUBLIC_CANONICAL_URLS
    assert metadata == PUBLIC_SITEMAP_METADATA
    assert all(lastmods)
    assert '2026-05-14' not in lastmods
    assert all(datetime.fromisoformat(value).date() <= today for value in lastmods)


def test_sitemap_lastmods_are_stable_across_fresh_checkout_rebuilds(
    client, monkeypatch, tmp_path, sitemap_events_path
):
    source_root = Path(booking_app.app.root_path)
    events_mtime = datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp()
    os.utime(sitemap_events_path, (events_mtime, events_mtime))

    observed = []
    for checkout_name, rebuild_date in (
        ('checkout-a', '2026-07-16'),
        ('checkout-b', '2026-07-17'),
    ):
        checkout_root = tmp_path / checkout_name
        rebuild_mtime = (
            datetime.fromisoformat(rebuild_date)
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
        _copy_checkout(source_root, checkout_root, rebuild_mtime)
        monkeypatch.setattr(booking_app.app, 'root_path', str(checkout_root))
        observed.append(_lastmods_by_url(client.get('/sitemap.xml')))

    assert observed[0] == observed[1]


def test_bundled_revision_changes_only_urls_mapped_to_that_source(
    client, monkeypatch, tmp_path, sitemap_events_path
):
    source_root = Path(booking_app.app.root_path)
    checkout_root = tmp_path / 'checkout'
    checkout_root.mkdir()
    revisions = json.loads(
        (source_root / REVISION_MANIFEST).read_text(encoding='utf-8')
    )
    (checkout_root / REVISION_MANIFEST).write_text(
        json.dumps(revisions),
        encoding='utf-8',
    )
    monkeypatch.setattr(booking_app.app, 'root_path', str(checkout_root))
    before = _lastmods_by_url(client.get('/sitemap.xml'))

    revisions['templates/landing_family_v2.html']['lastmod'] = '2026-07-15'
    (checkout_root / REVISION_MANIFEST).write_text(
        json.dumps(revisions),
        encoding='utf-8',
    )
    after = _lastmods_by_url(client.get('/sitemap.xml'))

    changed_urls = {url for url in before if before[url] != after[url]}
    assert changed_urls == {'https://book.pashynskaphoto.com/family'}


def test_persisted_event_revision_affects_only_home_and_booking(
    client, monkeypatch, tmp_path, sitemap_events_path
):
    source_root = Path(booking_app.app.root_path)
    checkout_root = tmp_path / 'checkout'
    checkout_root.mkdir()
    revisions = json.loads(
        (source_root / REVISION_MANIFEST).read_text(encoding='utf-8')
    )
    for source_path, revision in revisions.items():
        revision['lastmod'] = (
            '2026-06-15' if source_path == 'events.yaml' else '2026-06-01'
        )
    (checkout_root / REVISION_MANIFEST).write_text(
        json.dumps(revisions),
        encoding='utf-8',
    )
    monkeypatch.setattr(booking_app.app, 'root_path', str(checkout_root))
    before = _lastmods_by_url(client.get('/sitemap.xml'))

    events_data = yaml.safe_load(
        sitemap_events_path.read_text(encoding='utf-8')
    )
    events_data.setdefault('settings', {})['sitemap_test_revision'] = True
    sitemap_events_path.write_text(
        yaml.safe_dump(events_data, allow_unicode=True, sort_keys=False),
        encoding='utf-8',
    )
    changed_mtime = (
        datetime(2026, 7, 2, tzinfo=timezone.utc).timestamp()
    )
    os.utime(sitemap_events_path, (changed_mtime, changed_mtime))
    after = _lastmods_by_url(client.get('/sitemap.xml'))

    changed_urls = {url for url in before if before[url] != after[url]}
    assert changed_urls == {
        'https://book.pashynskaphoto.com/',
        'https://book.pashynskaphoto.com/book',
    }
    assert after['https://book.pashynskaphoto.com/'] == '2026-07-02'
    assert after['https://book.pashynskaphoto.com/book'] == '2026-07-02'

    revision_state = json.loads(
        Path(f'{sitemap_events_path}.revision.json').read_text(encoding='utf-8')
    )
    assert revision_state['lastmod'] == '2026-07-02'

    later_mtime = datetime(2026, 7, 9, tzinfo=timezone.utc).timestamp()
    os.utime(sitemap_events_path, (later_mtime, later_mtime))
    assert _lastmods_by_url(client.get('/sitemap.xml')) == after
