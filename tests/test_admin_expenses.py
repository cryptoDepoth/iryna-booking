"""Business expenses: auth, manual add, monthly summary math, idempotent import."""

import io
import os
import tempfile

import app as booking_app
import pytest


@pytest.fixture()
def admin_client(monkeypatch, tmp_path):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    booking_app._rate_limits.clear()
    booking_app.init_db()
    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as test_client:
        yield test_client
    try:
        os.unlink(db_path)
    except OSError:
        pass


HDRS = {"X-Admin-Key": "test-admin-key"}


def test_expenses_requires_admin(admin_client):
    r = admin_client.get("/admin/expenses")
    assert r.status_code in (302, 401)
    r = admin_client.get("/admin/expenses", headers=HDRS)
    assert r.status_code == 200
    assert b"Expenses" in r.data


def test_manual_add_shows_in_month_and_summary(admin_client):
    r = admin_client.post(
        "/admin/expenses/add",
        data={
            "date": "2026-07-05", "amount": "49.99", "category": "software",
            "vendor": "Adobe", "description": "Lightroom plan",
        },
        headers=HDRS,
    )
    assert r.status_code in (302, 303)
    page = admin_client.get("/admin/expenses?month=2026-07", headers=HDRS)
    assert page.status_code == 200
    assert b"Adobe" in page.data and b"49.99" in page.data


def test_add_rejects_bad_amount_and_date(admin_client):
    r = admin_client.post(
        "/admin/expenses/add",
        data={"date": "2026-07-05", "amount": "-5"},
        headers=HDRS,
    )
    assert r.status_code == 400
    r = admin_client.post(
        "/admin/expenses/add",
        data={"date": "07/05/2026", "amount": "10"},
        headers=HDRS,
    )
    assert r.status_code == 400


def test_import_is_idempotent_and_updates(admin_client):
    row = {
        "date": "2026-03-01", "amount": 111.11, "category": "ads_meta",
        "vendor": "Meta Ads", "description": "March spend",
        "source": "meta_auto", "external_id": "meta:2026-03",
    }
    r1 = admin_client.post("/admin/api/expenses/import", json={"rows": [row]}, headers=HDRS)
    assert r1.status_code == 200 and r1.get_json()["inserted"] == 1
    row["amount"] = 222.22
    r2 = admin_client.post("/admin/api/expenses/import", json={"rows": [row]}, headers=HDRS)
    body = r2.get_json()
    assert body["inserted"] == 0 and body["updated"] == 1
    page = admin_client.get("/admin/expenses?month=2026-03", headers=HDRS)
    assert b"222.22" in page.data and b"111.11" not in page.data


def test_import_rejects_rows_without_external_id(admin_client):
    r = admin_client.post(
        "/admin/api/expenses/import",
        json={"rows": [{"date": "2026-03-01", "amount": 5}]},
        headers=HDRS,
    )
    assert r.status_code == 200 and r.get_json()["skipped"] == 1


def test_invoice_upload_roundtrip_and_delete(admin_client, monkeypatch, tmp_path):
    monkeypatch.setattr(booking_app, "DB_PATH", booking_app.DB_PATH)
    data = {
        "date": "2026-07-06", "amount": "20", "category": "props",
        "vendor": "Dollarama", "invoice": (io.BytesIO(b"%PDF-1.4 test"), "receipt.pdf"),
    }
    r = admin_client.post(
        "/admin/expenses/add", data=data, headers=HDRS,
        content_type="multipart/form-data",
    )
    assert r.status_code in (302, 303)
    page = admin_client.get("/admin/expenses?month=2026-07", headers=HDRS)
    assert b"/admin/expenses/invoice/" in page.data
    import re as _re
    m = _re.search(rb"/admin/expenses/invoice/(\d+)", page.data)
    assert m
    eid = int(m.group(1))
    inv = admin_client.get(f"/admin/expenses/invoice/{eid}", headers=HDRS)
    assert inv.status_code == 200 and inv.data.startswith(b"%PDF")
    r = admin_client.post("/admin/expenses/delete", data={"id": str(eid)}, headers=HDRS)
    assert r.status_code in (302, 303)
    inv2 = admin_client.get(f"/admin/expenses/invoice/{eid}", headers=HDRS)
    assert inv2.status_code == 404
