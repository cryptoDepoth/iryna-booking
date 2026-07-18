from datetime import datetime, timezone
from xml.etree import ElementTree

import app as booking_app


PUBLIC_CANONICAL_URLS = {
    'https://book.pashynskaphoto.com/',
    'https://book.pashynskaphoto.com/book',
    'https://book.pashynskaphoto.com/wedding',
    'https://book.pashynskaphoto.com/family',
    'https://book.pashynskaphoto.com/maternity',
    'https://book.pashynskaphoto.com/privacy',
}
SITEMAP_NS = {'sitemap': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


def _sitemap_entries(response):
    root = ElementTree.fromstring(response.data)
    return root.findall('sitemap:url', SITEMAP_NS)


def test_public_sitemap_has_only_canonical_urls_and_meaningful_lastmods(client):
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
    today = datetime.now(timezone.utc).date()

    assert resp.status_code == 200
    assert resp.mimetype == 'application/xml'
    assert urls == PUBLIC_CANONICAL_URLS
    assert all(lastmods)
    assert '2026-05-14' not in lastmods
    assert all(datetime.fromisoformat(value).date() <= today for value in lastmods)


def test_sitemap_lastmods_follow_each_page_content_sources(client, monkeypatch):
    source_dates = {
        'events.yaml': '2026-07-02',
        'landing_wedding_v5.html': '2026-06-03',
        'landing_family_v2.html': '2026-06-04',
        'landing_maternity_v2.html': '2026-06-05',
        'privacy.html': '2026-06-06',
    }
    fallback_date = '2026-06-01'

    def fake_getmtime(path):
        date_value = next(
            (value for suffix, value in source_dates.items() if str(path).endswith(suffix)),
            fallback_date,
        )
        return datetime.fromisoformat(date_value).replace(tzinfo=timezone.utc).timestamp()

    monkeypatch.setattr(booking_app.os.path, 'getmtime', fake_getmtime)
    monkeypatch.setattr(
        booking_app.time,
        'time',
        lambda: datetime(2026, 7, 18, tzinfo=timezone.utc).timestamp(),
    )

    resp = client.get('/sitemap.xml')
    lastmod_by_url = {
        entry.findtext('sitemap:loc', namespaces=SITEMAP_NS):
        entry.findtext('sitemap:lastmod', namespaces=SITEMAP_NS)
        for entry in _sitemap_entries(resp)
    }

    assert lastmod_by_url == {
        'https://book.pashynskaphoto.com/': '2026-07-02',
        'https://book.pashynskaphoto.com/book': '2026-07-02',
        'https://book.pashynskaphoto.com/wedding': '2026-06-03',
        'https://book.pashynskaphoto.com/family': '2026-06-04',
        'https://book.pashynskaphoto.com/maternity': '2026-06-05',
        'https://book.pashynskaphoto.com/privacy': '2026-06-06',
    }
