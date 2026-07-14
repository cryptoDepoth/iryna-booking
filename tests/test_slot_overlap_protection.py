"""Regression coverage for schedule edits that leave bookings off-grid."""

from datetime import datetime, timezone
import os
import tempfile

import app as booking_app
import pytest


@pytest.fixture()
def admin_client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kw: None, raising=False)
    booking_app._rate_limits.clear()
    booking_app.init_db()
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as test_client:
        yield test_client
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _event():
    return {
        "id": "overlap-regression-event",
        "title": "Overlap Regression Session",
        "date": "2026-08-01",
        "start_time": "14:00",
        "end_time": "16:00",
        "session_length": 30,
        "break_length": 10,
        "slot_interval": 40,
        "deposit": 100,
        "full_price": 250,
        "session_type": "mini",
        "booking_type": "fixed_slots",
        "status": "active",
        "hidden": False,
        "photos": ["/images/overlap-regression.jpg"],
    }


def _insert_confirmed_booking(event):
    conn = booking_app.db_conn()
    conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,instagram,session_type,status,
              event_id,deposit_amount,full_price,confirmed,paid,paid_amount,
              reserved_until)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event["date"], "15:00", "Existing Client", "client@example.com",
            "", "", "mini", "confirmed", event["id"], 100, 250,
            1, 1, 250, datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def test_public_slots_hide_interval_that_overlaps_off_grid_booking(
    admin_client, monkeypatch
):
    event = _event()
    monkeypatch.setattr(booking_app, "EVENTS", [event])
    _insert_confirmed_booking(event)

    response = admin_client.get(
        f"/slots/{event['date']}?event_id={event['id']}"
    )
    assert response.status_code == 200
    available = {slot["time"] for slot in response.get_json()["slots"]}
    assert "14:00" in available
    assert "14:40" not in available  # 14:40–15:10 overlaps the 15:00 client
    assert "15:20" not in available  # and the required 10-minute break


def test_public_event_card_uses_same_overlap_aware_availability(
    admin_client, monkeypatch
):
    event = _event()
    monkeypatch.setattr(booking_app, "EVENTS", [event])
    _insert_confirmed_booking(event)

    response = admin_client.get("/events")

    assert response.status_code == 200
    card = response.get_json()["events"][0]
    assert card["total_spots"] == 3
    assert card["spots_left"] == 1


def test_paid_traffic_landing_never_features_a_false_last_slot(
    admin_client, monkeypatch
):
    sold_out = _event() | {
        "id": "sold-out-by-cross-event-overlap",
        "title": "Sold Out Cross Event",
        "start_time": "14:00",
        "end_time": "16:00",
        "session_length": 20,
        "break_length": 10,
        "slot_interval": 30,
    }
    available = sold_out | {
        "id": "actually-bookable-event",
        "title": "Actually Bookable Event",
        "date": "2026-08-02",
    }
    monkeypatch.setattr(booking_app, "EVENTS", [sold_out, available])

    conn = booking_app.db_conn()
    # Exact internal blocks close two of the four generated starts.
    for slot_time in ("14:00", "14:30"):
        conn.execute(
            """INSERT INTO bookings
                 (date,time,name,email,phone,instagram,session_type,status,event_id,confirmed,paid,reserved_until)
               VALUES (?,?,?,?,?,?,'internal_block','reserved',?,0,0,'2099-01-01T00:00:00+00:00')""",
            (
                sold_out["date"], slot_time, "⛔ Closed", "", "", "",
                sold_out["id"],
            ),
        )
    # A real 60-minute booking belonging to another event overlaps both the
    # 15:00 and 15:30 mini starts. Three rows for four starts would make the old
    # row-count arithmetic falsely claim one spot left.
    private_event = sold_out | {
        "id": "private-overlap",
        "session_length": 60,
        "break_length": 0,
        "session_type": "private",
        "hidden": True,
    }
    monkeypatch.setattr(booking_app, "EVENTS", [sold_out, available, private_event])
    conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,instagram,session_type,status,event_id,confirmed,paid)
           VALUES (?,?,?,?,?,?,'private','confirmed',?,1,1)""",
        (
            sold_out["date"], "15:00", "Private Client", "private@example.com",
            "", "", private_event["id"],
        ),
    )
    conn.commit()
    conn.close()

    response = admin_client.get("/book?type=mini")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Sold Out Cross Event" in html
    assert "Actually Bookable Event" in html
    assert '<div class="status sold"><span class="dot"></span>Sold out</div>' in html

    # The featured block must advance to the next actually bookable event.
    featured = html.split('<div class="featured-wrap"', 1)[1].split('</a>\n</div>', 1)[0]
    assert "Actually Bookable Event" in featured
    assert "Sold Out Cross Event" not in featured


def test_reserve_and_manual_book_reject_overlapping_time(admin_client, monkeypatch):
    event = _event()
    monkeypatch.setattr(booking_app, "EVENTS", [event])
    _insert_confirmed_booking(event)

    public = admin_client.post(
        "/reserve",
        json={
            "event_id": event["id"],
            "date": event["date"],
            "time": "14:40",
            "name": "Second Client",
            "email": "second@example.com",
            "phone": "",
            "instagram": "",
        },
    )
    assert public.status_code == 409

    manual = admin_client.post(
        f"/admin/api/event/{event['id']}/manual-book",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"time": "14:40", "name": "Walk In", "mark_paid": True},
    )
    assert manual.status_code == 409


def test_admin_board_shows_conflict_and_off_grid_booking(admin_client, monkeypatch):
    event = _event()
    monkeypatch.setattr(booking_app, "EVENTS", [event])
    _insert_confirmed_booking(event)

    response = admin_client.get(
        f"/admin/api/event/{event['id']}/slots",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.status_code == 200
    body = response.get_json()
    by_time = {}
    for slot in body["slots"]:
        by_time.setdefault(slot["time"], []).append(slot)

    assert by_time["14:40"][0]["state"] == "conflict"
    assert by_time["14:40"][0]["conflict_time"] == "15:00"
    off_grid = [s for s in by_time["15:00"] if s["off_grid"]]
    assert len(off_grid) == 1
    assert off_grid[0]["client"] == "Existing Client"
    assert body["summary"]["free"] == 1
    assert body["summary"]["conflicts"] == 2
    assert body["summary"]["off_grid"] == 1


def test_generate_slots_never_runs_past_event_end():
    event = _event() | {
        "start_time": "15:30",
        "end_time": "19:00",
        "session_length": 30,
        "break_length": 10,
        "slot_interval": 40,
    }
    slots = booking_app.generate_slots(event)
    assert slots[-1]["label"] == "18:10 – 18:40"
    assert all(slot["time"] != "18:50" for slot in slots)


def test_same_day_event_is_not_next_after_its_end_time():
    event = _event() | {"date": "2026-07-11", "end_time": "19:00"}
    now = datetime(2026, 7, 12, 5, 30, tzinfo=timezone.utc)  # 23:30 Edmonton
    assert booking_app._admin_event_has_ended(event, now=now)
    assert not booking_app._admin_event_is_current(event, now=now)
