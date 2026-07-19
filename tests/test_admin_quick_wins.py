"""Regression coverage for the evidence-backed admin P2/P3 quick wins."""

from pathlib import Path

import pytest

import app as booking_app


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setattr(booking_app, "DB_PATH", str(tmp_path / "admin-quick-wins.db"))
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    booking_app.init_db()
    booking_app.app.config["TESTING"] = True

    with booking_app.app.test_client() as client:
        yield client


def _insert_booking():
    conn = booking_app.db_conn()
    conn.execute(
        """
        INSERT INTO bookings (
            date, time, name, email, phone, instagram, session_type, status, paid, confirmed,
            paid_amount, deposit_amount, full_price, event_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-08-15",
            "10:00",
            "Admin Audit Client",
            "admin-audit@example.test",
            "4035550101",
            "admin.audit",
            "mini",
            "confirmed",
            1,
            1,
            100.0,
            100.0,
            300.0,
            "mission-mini-1",
        ),
    )
    booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return booking_id


def test_filtered_classic_admin_uses_an_explicit_no_matches_state(admin_client):
    _insert_booking()

    response = admin_client.get(
        "/admin?search=definitely-not-a-client",
        headers={"X-Admin-Key": "test-admin-key"},
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "No bookings match the current filters." in body
    assert "<p>No bookings yet.</p>" not in body


def test_booking_payment_actions_have_busy_duplicate_submit_guards():
    html = (ROOT / "templates" / "booking_detail.html").read_text(encoding="utf-8")

    for button_id in (
        "save-invoice-btn",
        "send-invoice-btn",
        "request-balance-btn",
        "recheck-payment-btn",
    ):
        assert f'id="{button_id}"' in html

    assert "function beginAdminAction(button, busyText)" in html
    assert "async function saveInvoiceAmounts(button)" in html
    assert "async function requestBalance(button)" in html
    assert "async function recheckPayment(button)" in html
    assert "async function sendInvoice(button)" in html
    assert html.count("if (!done) return") >= 4
    assert "aria-busy" in html
    assert "button.disabled = true;" in html
    assert "button.disabled = false;" in html
    assert "button.removeAttribute('aria-busy');" in html

    admin_post = html.split("async function adminPost", 1)[1].split(
        "function invoiceAmounts", 1
    )[0]
    request_balance = html.split("async function requestBalance", 1)[1].split(
        "async function recheckPayment", 1
    )[0]
    assert "catch (error)" in admin_post
    assert "showToast('Network error: ' + error.message, 'err');" in admin_post
    assert "catch (error)" in request_balance
    assert "showToast('Network error: ' + error.message, 'err');" in request_balance
    assert "finally" in request_balance


def test_other_invoice_and_balance_entry_points_have_busy_guards():
    classic = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    clients = (ROOT / "templates" / "admin_clients.html").read_text(encoding="utf-8")
    event_detail = (ROOT / "templates" / "admin_event.html").read_text(encoding="utf-8")

    assert "requestBalance({{ b.id }}, {{ (b.name or \"\") | tojson }}, this)" in classic
    assert "async function requestBalance(id, name, button)" in classic
    assert "if (!button || button.disabled) return;" in classic
    assert "button.textContent = 'Sending…';" in classic

    assert 'onclick="sendInvoice(${b.id}, this)"' in clients
    assert "async function sendInvoice(bookingId, button)" in clients
    assert "if (!button || button.disabled) return;" in clients
    assert 'button.textContent = "Sending…";' in clients

    assert "requestBalance(id, name, button)" in event_detail
    assert "async function requestBalance(id, name, button)" in event_detail
    assert "if (!button || button.disabled) return;" in event_detail
    assert "button.setAttribute('aria-busy', 'true');" in event_detail
    assert "button.textContent = 'Sending…';" in event_detail
    assert "showToast('Network error: ' + error.message, 'err');" in event_detail
    assert "button.removeAttribute('aria-busy');" in event_detail


def test_admin_mobile_action_targets_are_at_least_44px():
    classic = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    detail = (ROOT / "templates" / "booking_detail.html").read_text(encoding="utf-8")
    clients = (ROOT / "templates" / "admin_clients.html").read_text(encoding="utf-8")
    event_detail = (ROOT / "templates" / "admin_event.html").read_text(encoding="utf-8")

    assert ".btn-mark-paid,.btn-balance,.btn-reschedule,.filter-btn{min-height:44px}" in classic
    assert ".btn { min-height: 44px; }" in detail
    assert ".btn-sm { min-height: 44px;" in clients
    assert ".mini-btn { min-height: 44px;" in event_detail


def test_site_improvement_report_records_every_admin_candidate_and_decision_field():
    report = (ROOT / "SITE_IMPROVEMENT_REPORT_2026-07.md").read_text(encoding="utf-8")

    candidates = (
        "Rejection of `?key=` authentication",
        "Delete/cancel confirmations",
        "Loading and duplicate-submit protection for invoice, recheck, and Stripe actions; explicit empty states",
        "Status legend and mobile touch targets",
        "Authenticated health indicator",
    )
    for candidate in candidates:
        assert candidate in report

    assert "| Accepted candidate | Status before this feature | Problem | Impact | Benefit | Risk |" in report
    assert "No meaningful improvement was found" in report

    decision_rows = {}
    for line in report.splitlines():
        if not line.startswith("| ") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 7 and cells[0] != "Accepted candidate":
            decision_rows[cells[0]] = cells[1:]

    for candidate in candidates:
        assert candidate in decision_rows
        status, problem, impact, benefit, risk, decision = decision_rows[candidate]
        assert status
        assert problem
        assert impact
        assert benefit
        assert risk
        assert decision
