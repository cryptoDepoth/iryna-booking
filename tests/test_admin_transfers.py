import io
import os
import tempfile

import pytest

import app as booking_app


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    booking_app.init_db()
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        yield c, booking_app
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _login(client):
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True


def test_admin_transfers_page_imports_csv_and_renders(client):
    c, _ = client
    _login(c)
    csv_data = "# ,Date,Sender,Amount ($),Direction,Subject\n1,2026-05-30,Yulia Levitskaya and it,120.50,IN ←,Interac e-Transfer: You've received $120.50 from Yulia Levitskaya and it has been automatically deposited.\n"
    resp = c.post(
        "/admin/transfers/import",
        data={"csv": (io.BytesIO(csv_data.encode()), "interac.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["inserted"] == 1

    page = c.get("/admin/transfers")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Yulia Levitskaya" in html
    assert "$120.50" in html
    assert "unmatched" in html


def test_admin_transfer_link_updates_booking_paid_amount(client):
    c, app = client
    _login(c)
    with app.app.app_context():
        import app as booking_app
        conn = booking_app.db_conn()
        conn.execute("""
            INSERT INTO bookings (date, time, name, email, phone, instagram, session_type,
                                  status, confirmed, paid, paid_amount, deposit_amount, full_price, event_id)
            VALUES ('2026-07-04','13:30','Yulia Levitskaya','yulia@example.com','403','',
                    'Canoe Mini Session','pending_payment',0,0,0,110.25,220.50,'canoe-jul4')
        """)
        booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("""
            INSERT INTO etransfers (reference_number, sender_name, amount, memo, direction, email_date, status, source)
            VALUES ('test-ref-12050','Yulia Levitskaya',120.50,'Canoe mini session July 4 1:30 pm','in','2026-05-30','unmatched','csv')
        """)
        transfer_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit(); conn.close()

    resp = c.post(f"/admin/transfers/{transfer_id}/link", json={"booking_id": booking_id})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    with app.app.app_context():
        import app as booking_app
        conn = booking_app.db_conn()
        b = conn.execute("SELECT paid_amount, confirmed, paid, status FROM bookings WHERE id=?", (booking_id,)).fetchone()
        t = conn.execute("SELECT matched_booking_id, status FROM etransfers WHERE id=?", (transfer_id,)).fetchone()
        conn.close()
    assert round(b["paid_amount"], 2) == 120.50
    assert b["confirmed"] == 1
    assert b["paid"] == 1
    assert b["status"] == "confirmed"
    assert t["matched_booking_id"] == booking_id
    assert t["status"] == "matched"
