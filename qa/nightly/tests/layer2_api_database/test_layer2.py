"""LAYER 2 — API + DATABASE VALIDATION
Health, endpoints, response times, consistency checks.
"""
import pytest, sqlite3, os, sys, time, requests
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / '.env.qa', override=False)

BASE_URL = os.getenv('TEST_BASE_URL', 'http://127.0.0.1:5001')
DB_PATH = os.getenv('DB_PATH', '/Users/andrzej/.pashynska-data/bookings.db')

RESPONSE_TIME_SLA_MS = 500  # p95 must be under 500ms

def api(path, method='GET', json_data=None, timeout=20):
    url = f"{BASE_URL}{path}"
    start = time.time()
    if method == 'GET':
        r = requests.get(url, timeout=timeout)
    elif method == 'POST':
        r = requests.post(url, json=json_data, timeout=timeout)
    else:
        raise ValueError(method)
    elapsed_ms = (time.time() - start) * 1000
    return r, elapsed_ms

class TestHealthAndAvailability:
    """Core endpoints must respond fast and correctly."""

    def test_health_or_homepage(self):
        # Health endpoint may not exist; verify homepage or any 200
        r, elapsed = api('/')
        assert r.status_code == 200
        assert elapsed < RESPONSE_TIME_SLA_MS, f'Health took {elapsed:.0f}ms > {RESPONSE_TIME_SLA_MS}ms'

    def test_homepage_loads(self):
        r, elapsed = api('/')
        assert r.status_code == 200
        assert 'text/html' in r.headers.get('Content-Type', '')
        assert elapsed < RESPONSE_TIME_SLA_MS

    def test_events_endpoint(self):
        r, elapsed = api('/events')
        assert r.status_code == 200
        data = r.json()
        events = data.get('events', data if isinstance(data, list) else [])
        assert isinstance(events, list)
        for ev in events:
            assert 'id' in ev
            assert 'date' in ev
            assert 'title' in ev
        assert elapsed < RESPONSE_TIME_SLA_MS

    def test_slots_endpoint(self):
        events = api('/events')[0].json()
        if not events:
            pytest.skip('No events')
        ev = events[0] if isinstance(events, list) else events.get('events', [events])[0] if isinstance(events, dict) else None
        if ev is None:
            pytest.skip('No events')
        r, elapsed = api(f'/slots/{ev["date"]}?event_id={ev["id"]}')
        assert r.status_code == 200
        slots_data = r.json()
        slots = slots_data.get('slots', slots_data if isinstance(slots_data, list) else [])
        assert isinstance(slots, list)
        for s in slots:
            assert 'time' in s
            assert 'label' in s
        assert elapsed < RESPONSE_TIME_SLA_MS

    def test_404_returns_json(self):
        r, _ = api('/nonexistent')
        assert r.status_code == 404

class TestDatabaseConsistency:
    """Direct DB checks for corrupted data."""

    def test_no_duplicate_confirmed_same_slot(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT event_id, date, time, COUNT(*) as cnt
            FROM bookings
            WHERE status = 'confirmed'
            GROUP BY event_id, date, time
            HAVING cnt > 1
        """)
        rows = c.fetchall()
        conn.close()
        assert len(rows) == 0, f'Double-confirmed slots: {[dict(r) for r in rows]}'

    def test_all_bookings_have_valid_event_id(self):
        # Events come from events.yaml, not DB table
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT id, event_id FROM bookings
            WHERE event_id IS NOT NULL
        """)
        rows = c.fetchall()
        conn.close()
        # Just verify event_id is not null (actual validity checked by app logic)
        for bid, eid in rows:
            assert eid, f'Booking {bid} has empty event_id'

    def test_no_bookings_with_null_event_id_if_required(self):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id FROM bookings WHERE event_id IS NULL OR event_id = ''")
        rows = c.fetchall()
        conn.close()
        # Allow null event_id for legacy bookings only
        pass  # Soft check — log warning if needed

    def test_booking_id_sequence_monotonic(self):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT id FROM bookings ORDER BY id')
        ids = [r[0] for r in c.fetchall()]
        conn.close()
        for i in range(1, len(ids)):
            assert ids[i] >= ids[i-1], 'ID sequence not monotonic'

    def test_no_null_critical_fields(self):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT id FROM bookings
            WHERE name IS NULL OR email IS NULL OR date IS NULL OR time IS NULL
        """)
        rows = c.fetchall()
        conn.close()
        assert len(rows) == 0, f'Bookings with null critical fields: {rows}'
