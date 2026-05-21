"""LAYER 1 — CRITICAL BUSINESS FLOW TESTS
Highest priority. Must be stable and deterministic.
Tests the core booking engine: reserve, confirm, cancel, expire, duplicate prevention.
"""
import pytest, sqlite3, time, os, sys, json, requests
from pathlib import Path
from datetime import datetime, timedelta

# ── Load environment FIRST (before any variable definitions) ───────────────
BASE = Path(__file__).resolve().parents[3]
from dotenv import load_dotenv
load_dotenv(BASE / '.env.qa', override=False)

# ── Configuration ──────────────────────────────────────────────────────────
BASE_URL = os.getenv('TEST_BASE_URL', 'http://127.0.0.1:5001')
DB_PATH = os.getenv('DB_PATH', '/Users/andrzej/.pashynska-data/bookings.db')
QA_EMAIL = os.getenv('QA_EMAIL_PREFIX', 'qa-test') + '@example.com'
QA_NAME = os.getenv('QA_NAME_PREFIX', 'QA Test') + ' Client'
QA_PHONE = os.getenv('QA_PHONE', '403-555-0100')

_api_counter = 0

def api(path, method='GET', json_data=None, timeout=20):
    """HTTP helper with deterministic per-request QA IPs.

    Layer 1 intentionally exercises multiple /reserve calls in one run. The app's
    real production limiter is 5 reserve attempts per 10 minutes per IP, so a
    local autonomous run can otherwise turn later business-logic assertions into
    skips. We keep the limiter enabled but send synthetic Cloudflare-style client
    IPs for /reserve only.
    """
    global _api_counter
    url = f"{BASE_URL}{path}"
    headers = None
    if path.startswith('/reserve'):
        _api_counter += 1
        headers = {'X-Forwarded-For': f'10.215.{os.getpid() % 250}.{_api_counter}'}
    if method == 'GET':
        r = requests.get(url, timeout=timeout, headers=headers)
    elif method == 'POST':
        r = requests.post(url, json=json_data, timeout=timeout, headers=headers)
    else:
        raise ValueError(method)
    return r

def db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(sql, params)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def cleanup_qa_bookings():
    """Delete all QA test bookings by email pattern."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM bookings WHERE email LIKE ?", (f'%{os.getenv("QA_EMAIL_PREFIX","qa-test")}%',))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return deleted
    except sqlite3.OperationalError:
        return 0

# ── fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture
def fresh_qa_data():
    """Guarantee no QA bookings exist at test start and end."""
    cleanup_qa_bookings()
    yield
    cleanup_qa_bookings()

# ── LAYER 1 TESTS ────────────────────────────────────────────────────────────

class TestSlotReservation:
    """Reserve a slot → slot becomes unavailable."""

    def test_reserve_slot(self, fresh_qa_data):
        # 1. Get events
        r = api('/events')
        assert r.status_code == 200
        data = r.json()
        events = data.get('events', data if isinstance(data, list) else [])
        assert len(events) > 0, 'No active events'
        event = events[0]
        event_id = event['id']
        event_date = event['date']

        # 2. Get slots
        r = api(f'/slots/{event_date}?event_id={event_id}')
        assert r.status_code == 200
        slots_data = r.json()
        slots = slots_data.get('slots', slots_data if isinstance(slots_data, list) else [])
        assert len(slots) > 0, 'No slots available'
        slot = slots[0]
        time_str = slot['time']

        # 3. Reserve
        r = api('/reserve', 'POST', {
            'event_id': event_id,
            'time': time_str,
            'date': event_date,
            'name': QA_NAME,
            'email': QA_EMAIL,
            'phone': QA_PHONE,
            'instagram': '@qatest',
        })
        # Rate limit check
        if r.status_code == 429:
            pytest.skip(f'Rate limited: {r.json().get("error")}')
        assert r.status_code == 200, f'Reserve failed: {r.text}'
        data = r.json()
        assert data.get('success') is True
        booking_id = data.get('booking_id')
        token = data.get('confirmation_token')
        assert booking_id and token

        # 4. Verify slot is now unavailable
        r = api(f'/slots/{event_date}?event_id={event_id}')
        slots_after = r.json()
        if isinstance(slots_after, dict):
            slots_after = slots_after.get('slots', [])
        taken = [s for s in slots_after if s['time'] == time_str and s.get('available') is False]
        assert len(taken) > 0 or len([s for s in slots_after if s['time'] == time_str]) == 0, \
            'Slot still available after reservation'

        # 5. Verify DB record
        rows = db_query('SELECT * FROM bookings WHERE id=?', (booking_id,))
        assert len(rows) == 1
        b = rows[0]
        assert b['status'] in ('reserved', 'pending_payment')
        assert b['email'] == QA_EMAIL
        assert b['name'] == QA_NAME

    def test_cannot_double_book_same_slot(self, fresh_qa_data):
        # Reserve first
        r = api('/events')
        events = r.json().get('events', [])
        event = events[0]
        r = api(f'/slots/{event["date"]}?event_id={event["id"]}')
        slots = r.json().get('slots', [])
        slot = slots[0]

        r1 = api('/reserve', 'POST', {
            'event_id': event['id'], 'time': slot['time'], 'date': event['date'],
            'name': QA_NAME, 'email': QA_EMAIL, 'phone': QA_PHONE,
        })
        # Rate limit or success
        assert r1.status_code in (200, 429), f'First reserve failed: {r1.status_code}'
        if r1.status_code == 429:
            pytest.skip('Rate limited — cannot test double booking')
        assert r1.json()['success'] is True

        # Try second reservation for same slot
        r2 = api('/reserve', 'POST', {
            'event_id': event['id'], 'time': slot['time'], 'date': event['date'],
            'name': QA_NAME + ' Two', 'email': 'qa-test-two@example.com', 'phone': QA_PHONE,
        })
        # Should fail or return a waitlist/alert
        assert r2.status_code in (200, 409, 422, 429), f'Unexpected status: {r2.status_code}'
        if r2.status_code == 200:
            data = r2.json()
            assert data.get('success') is False or data.get('waitlisted') is True, \
                'Double booking allowed — CRITICAL BUG'


class TestBookingConfirmation:
    """Client confirms payment → admin sees confirmed booking."""

    def test_confirm_and_verify_status(self, fresh_qa_data):
        # 1. Reserve
        events = api('/events').json().get('events', [])
        event = events[0]
        slots = api(f'/slots/{event["date"]}?event_id={event["id"]}').json().get('slots', [])
        slot = slots[0]

        r = api('/reserve', 'POST', {
            'event_id': event['id'], 'time': slot['time'], 'date': event['date'],
            'name': QA_NAME, 'email': QA_EMAIL, 'phone': QA_PHONE,
        })
        if r.status_code == 429:
            pytest.skip(f'Rate limited: {r.json().get("error")}')
        data = r.json()
        booking_id = data['booking_id']
        token = data['confirmation_token']

        # 2. Confirm (simulate e-Transfer sent)
        r = api('/confirm', 'POST', {
            'booking_id': booking_id,
            'confirmation_token': token,
        })
        assert r.status_code == 200
        assert r.json()['success'] is True

        # 3. Verify DB status
        rows = db_query('SELECT * FROM bookings WHERE id=?', (booking_id,))
        assert len(rows) == 1
        b = rows[0]
        assert b['status'] == 'pending_payment'
        assert b['reserved_until'] is not None

        # 4. Simulate admin confirm via DB
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE bookings SET status='confirmed', confirmed=1, paid=1 WHERE id=?", (booking_id,))
        conn.commit()
        conn.close()

        rows = db_query('SELECT * FROM bookings WHERE id=?', (booking_id,))
        assert rows[0]['status'] == 'confirmed'
        assert rows[0]['confirmed'] == 1
        assert rows[0]['paid'] == 1


class TestCancellationAndSlotRelease:
    """Cancel booking → slot becomes available again."""

    def test_cancel_frees_slot(self, fresh_qa_data):
        # 1. Reserve
        events = api('/events').json().get('events', [])
        event = events[0]
        slots = api(f'/slots/{event["date"]}?event_id={event["id"]}').json().get('slots', [])
        slot = slots[0]
        time_str = slot['time']

        r = api('/reserve', 'POST', {
            'event_id': event['id'], 'time': time_str, 'date': event['date'],
            'name': QA_NAME, 'email': QA_EMAIL, 'phone': QA_PHONE,
        })
        if r.status_code == 429:
            pytest.skip(f'Rate limited: {r.json().get("error")}')
        data = r.json()
        booking_id = data['booking_id']
        token = data['confirmation_token']

        # 2. Cancel via client API
        r = api('/cancel-reservation', 'POST', {
            'booking_id': booking_id,
            'token': token,
        })
        assert r.status_code == 200
        assert r.json()['success'] is True

        # 3. Verify DB
        rows = db_query('SELECT * FROM bookings WHERE id=?', (booking_id,))
        assert rows[0]['status'] == 'cancelled'

        # 4. Verify slot is available again
        r = api(f'/slots/{event["date"]}?event_id={event["id"]}')
        slots_after = r.json()
        if isinstance(slots_after, dict):
            slots_after = slots_after.get('slots', [])
        available = [s for s in slots_after if s['time'] == time_str and s.get('available', True) is not False]
        assert len(available) > 0, 'Slot not freed after cancellation'


class TestExpiredReservationCleanup:
    """Expired reservations should be cleaned and slots freed."""

    def test_expire_old_reservations(self, fresh_qa_data):
        # 1. Create a reservation
        events = api('/events').json().get('events', [])
        event = events[0]
        slots = api(f'/slots/{event["date"]}?event_id={event["id"]}').json().get('slots', [])
        slot = slots[0]

        r = api('/reserve', 'POST', {
            'event_id': event['id'], 'time': slot['time'], 'date': event['date'],
            'name': QA_NAME, 'email': QA_EMAIL, 'phone': QA_PHONE,
        })
        assert r.status_code in (200, 429), f'Reserve failed: {r.status_code}'
        if r.status_code == 429:
            pytest.skip('Rate limited — cannot test expiry')
        booking_id = r.json()['booking_id']

        # 2. Manually age the reservation in DB
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('UPDATE bookings SET reserved_until=? WHERE id=?', (past, booking_id))
        conn.commit()
        conn.close()

        # 3. Trigger expiry endpoint
        r = api('/expired', 'POST')
        assert r.status_code == 200
        result = r.json()
        assert result['success'] is True
        assert result['released'] >= 1

        # 4. Verify booking is expired
        rows = db_query('SELECT * FROM bookings WHERE id=?', (booking_id,))
        assert rows[0]['status'] == 'expired'

        # 5. Verify slot is freed (check available count increased)
        r = api(f'/slots/{event["date"]}?event_id={event["id"]}')
        slots_after = r.json()
        available_count = slots_after.get('available', 0)
        assert available_count > 0, f'Slot not freed — available count is {available_count}'


class TestDataIntegrity:
    """No orphan records, no corrupted status transitions."""

    def test_no_orphan_reservations_without_event_id(self):
        rows = db_query("""
            SELECT id FROM bookings
            WHERE event_id IS NULL OR event_id = ''
            AND status IN ('reserved','confirmed','pending_payment')
        """)
        assert len(rows) == 0, f'Active bookings without event_id: {rows}'

    def test_status_transitions_are_valid(self):
        rows = db_query('SELECT id, status, confirmed, paid FROM bookings')
        for r in rows:
            if r['status'] == 'confirmed':
                assert r['confirmed'] == 1, f'Booking {r["id"]} confirmed status but confirmed=0'
            if r['status'] == 'cancelled':
                assert r['confirmed'] == 0, f'Booking {r["id"]} cancelled but confirmed=1'

    def test_no_negative_paid_amounts(self):
        rows = db_query('SELECT id, paid_amount FROM bookings WHERE paid_amount IS NOT NULL')
        for r in rows:
            assert r['paid_amount'] >= 0, f'Booking {r["id"]} has negative paid_amount'

    def test_timestamps_monotonic(self):
        rows = db_query('SELECT id, created_at, reserved_until FROM bookings WHERE reserved_until IS NOT NULL AND id > 100')
        for r in rows:
            created_str = r['created_at'].replace('Z', '+00:00') if r['created_at'] else None
            reserved_str = r['reserved_until'].replace('Z', '+00:00') if r['reserved_until'] else None
            if not created_str or not reserved_str:
                continue
            try:
                created = datetime.fromisoformat(created_str)
                reserved = datetime.fromisoformat(reserved_str)
                # Allow >= for edge cases (same-second reservations)
                assert reserved >= created, f'Booking {r["id"]} reserved_until < created_at'
            except (ValueError, TypeError):
                # Skip records with non-standard timestamp formats
                continue
