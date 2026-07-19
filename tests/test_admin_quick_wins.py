"""Regression coverage for the evidence-backed admin P2/P3 quick wins."""

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import app as booking_app


ROOT = Path(__file__).resolve().parents[1]


def _chromium_launch_options(playwright):
    launch_options = {"headless": True}
    if not Path(playwright.chromium.executable_path).exists():
        candidates = sorted(
            (Path.home() / "Library" / "Caches" / "ms-playwright").glob(
                "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/"
                "chrome-headless-shell"
            )
        )
        assert candidates, "A local Playwright Chromium binary is required for DOM regressions"
        launch_options["executable_path"] = str(candidates[-1])
    return launch_options


def _with_test_base(html):
    return html.replace("<head>", '<head><base href="http://booking.test/">', 1)


def _install_deferred_action_fetch(page):
    page.evaluate(
        """
        () => {
          window.__adminActionRequests = [];
          window.__adminActionPending = [];
          window.fetch = (url, options = {}) => {
            window.__adminActionRequests.push({
              url: String(url),
              method: options.method || 'GET',
            });
            return new Promise((resolve, reject) => {
              window.__adminActionPending.push({ resolve, reject });
            });
          };
        }
        """
    )


def _assert_single_flight_failure_and_retry(
    page,
    *,
    button_selector,
    invoke_twice_script,
    invoke_once_script,
    request_url,
    error_text,
    error_class,
    success_text,
    success_class,
    success_payload,
):
    button = page.locator(button_selector)
    original_text = button.evaluate("element => element.textContent")
    assert button.is_enabled()
    assert button.get_attribute("aria-busy") is None

    _install_deferred_action_fetch(page)
    page.evaluate(invoke_twice_script)
    page.wait_for_function("() => window.__adminActionRequests.length === 1")

    busy_state = button.evaluate(
        """
        element => ({
          disabled: element.disabled,
          ariaBusy: element.getAttribute('aria-busy'),
          text: element.textContent,
        })
        """
    )
    assert busy_state == {
        "disabled": True,
        "ariaBusy": "true",
        "text": "Sending…",
    }
    assert page.evaluate("() => window.__adminActionRequests") == [
        {"url": request_url, "method": "POST"}
    ]

    page.evaluate(
        """
        () => window.__adminActionPending[0].reject(
          new Error('forced offline regression')
        )
        """
    )
    page.wait_for_function(
        """
        ([selector, text]) => {
          const button = document.querySelector(selector);
          return button
            && !button.disabled
            && !button.hasAttribute('aria-busy')
            && button.textContent === text;
        }
        """,
        arg=[button_selector, original_text],
    )

    toast = page.locator("#toast")
    assert toast.is_visible()
    assert error_text in toast.inner_text()
    assert error_class in toast.get_attribute("class").split()

    page.evaluate(invoke_once_script)
    page.wait_for_function("() => window.__adminActionRequests.length === 2")
    assert button.is_disabled()
    assert button.get_attribute("aria-busy") == "true"
    assert button.evaluate("element => element.textContent") == "Sending…"

    page.evaluate(
        """
        payload => window.__adminActionPending[1].resolve(
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: {'Content-Type': 'application/json'},
          })
        )
        """,
        success_payload,
    )
    page.wait_for_function(
        """
        ([selector, text, successText]) => {
          const button = document.querySelector(selector);
          const toast = document.querySelector('#toast');
          return button
            && !button.disabled
            && !button.hasAttribute('aria-busy')
            && button.textContent === text
            && toast
            && toast.textContent.includes(successText);
        }
        """,
        arg=[button_selector, original_text, success_text],
    )

    assert toast.is_visible()
    assert success_text in toast.inner_text()
    assert success_class in toast.get_attribute("class").split()
    assert page.evaluate("() => window.__adminActionRequests") == [
        {"url": request_url, "method": "POST"},
        {"url": request_url, "method": "POST"},
    ]


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


def test_event_roster_actions_have_rendered_mobile_touch_targets(admin_client, monkeypatch):
    event = {
        "id": "mission-mini-1",
        "title": "Mission Test Mini One",
        "date": "2099-08-01",
        "start_time": "10:00",
        "end_time": "13:00",
        "session_length": 20,
        "break_length": 10,
        "slot_interval": 30,
        "deposit": 100.0,
        "full_price": 300.0,
        "location": "Calgary test location",
        "status": "active",
    }
    monkeypatch.setattr(booking_app, "EVENTS", [event])
    response = admin_client.get(
        "/admin/event/mission-mini-1",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.status_code == 200
    html = _with_test_base(response.get_data(as_text=True))
    slots_fixture = {
        "event": event,
        "summary": {
            "booked": 2,
            "confirmed": 1,
            "pending": 1,
            "blocked": 0,
            "free": 0,
        },
        "slots": [],
        "bookings": [
            {
                "id": 41,
                "name": "Rendered Roster Client",
                "email": "rendered-roster@example.test",
                "phone": "4035550141",
                "instagram": "rendered.roster",
                "time": "10:00",
                "status": "confirmed",
                "confirmed": 1,
                "paid_amount": 100.0,
                "deposit_amount": 100.0,
                "selected_addons": [],
            },
            {
                "id": 42,
                "name": "Pending Roster Client",
                "email": "pending-roster@example.test",
                "phone": "4035550142",
                "instagram": "pending.roster",
                "time": "10:30",
                "status": "reserved",
                "confirmed": 0,
                "paid_amount": 0.0,
                "deposit_amount": 100.0,
                "selected_addons": [],
            },
        ],
    }

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**_chromium_launch_options(playwright))
        page = browser.new_page(viewport={"width": 390, "height": 844})

        def route_request(route):
            path = urlsplit(route.request.url).path
            if path == "/admin/api/event/mission-mini-1/slots":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(slots_fixture),
                )
                return
            route.abort()

        page.route("**/*", route_request)
        page.set_content(html, wait_until="domcontentloaded")
        page.wait_for_selector(".roster-actions .mini-btn")
        actions = page.locator(".roster-actions .mini-btn")
        assert actions.count() == 8
        mobile_metrics = actions.evaluate_all(
            """
            elements => elements.map(element => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return {
                tag: element.tagName,
                text: element.textContent.trim(),
                display: style.display,
                alignItems: style.alignItems,
                justifyContent: style.justifyContent,
                height: rect.height,
                padding: style.padding,
              };
            })
            """
        )
        assert {metric["tag"] for metric in mobile_metrics} == {"A", "BUTTON"}
        assert {metric["text"] for metric in mobile_metrics} == {
            "Open",
            "Balance",
            "Confirm",
            "Reschedule",
            "Edit contact",
        }
        assert all(metric["display"] == "flex" for metric in mobile_metrics)
        assert all(metric["alignItems"] == "center" for metric in mobile_metrics)
        assert all(metric["justifyContent"] == "center" for metric in mobile_metrics)
        assert all(metric["height"] >= 44 for metric in mobile_metrics)
        assert all(metric["padding"] == "10px 12px" for metric in mobile_metrics)

        page.set_viewport_size({"width": 1280, "height": 900})
        desktop_metrics = actions.evaluate_all(
            """
            elements => elements.map(element => {
              const style = getComputedStyle(element);
              return {
                display: style.display,
                height: element.getBoundingClientRect().height,
                padding: style.padding,
              };
            })
            """
        )
        assert all(metric["display"] == "flex" for metric in desktop_metrics)
        assert all(28 <= metric["height"] < 30 for metric in desktop_metrics)
        assert all(metric["padding"] == "5px 9px" for metric in desktop_metrics)
        browser.close()


def test_invoice_balance_and_recheck_actions_are_single_flight_and_recover(admin_client):
    booking_id = _insert_booking()
    response = admin_client.get(
        f"/admin/booking/{booking_id}",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.status_code == 200
    html = _with_test_base(response.get_data(as_text=True))
    calls = {
        "invoice_save": 0,
        "invoice_send": 0,
        "balance": 0,
        "recheck": 0,
    }
    request_state = {"invoice_should_fail": True}

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**_chromium_launch_options(playwright))
        page = browser.new_page()

        def route_request(route):
            path = urlsplit(route.request.url).path
            if path == f"/admin/booking/{booking_id}/invoice":
                calls["invoice_save"] += 1
                if request_state["invoice_should_fail"]:
                    route.abort("failed")
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"success": True, "balance_due": 200.0}),
                )
                return
            if path == f"/admin/booking/{booking_id}/send-invoice":
                calls["invoice_send"] += 1
                route.abort("failed")
                return
            if path == "/admin/request-balance":
                calls["balance"] += 1
                route.abort("failed")
                return
            if path == f"/admin/booking/{booking_id}/recheck-payment":
                calls["recheck"] += 1
                route.abort("failed")
                return
            route.abort()

        page.route("**/*", route_request)
        page.set_content(html, wait_until="domcontentloaded")
        page.evaluate("window.confirm = () => true")

        page.evaluate(
            """
            () => {
              const button = document.querySelector('#save-invoice-btn');
              window.saveInvoiceAmounts(button);
              window.saveInvoiceAmounts(button);
            }
            """
        )
        page.wait_for_function(
            """
            () => {
              const button = document.querySelector('#save-invoice-btn');
              return button && !button.disabled && !button.hasAttribute('aria-busy');
            }
            """
        )
        assert calls["invoice_save"] == 1
        assert "Network error:" in page.locator("#toast").inner_text()
        assert "err" in page.locator("#toast").get_attribute("class").split()

        request_state["invoice_should_fail"] = False
        action_cases = (
            ("sendInvoice", "#send-invoice-btn", "invoice_send"),
            ("requestBalance", "#request-balance-btn", "balance"),
            ("recheckPayment", "#recheck-payment-btn", "recheck"),
        )
        for function_name, selector, call_name in action_cases:
            before = calls[call_name]
            page.evaluate(
                """
                ([functionName, selector]) => {
                  const button = document.querySelector(selector);
                  window[functionName](button);
                  window[functionName](button);
                }
                """,
                [function_name, selector],
            )
            page.wait_for_function(
                """
                selector => {
                  const button = document.querySelector(selector);
                  return button && !button.disabled && !button.hasAttribute('aria-busy');
                }
                """,
                arg=selector,
            )
            assert calls[call_name] == before + 1
            assert "Network error:" in page.locator("#toast").inner_text()
            assert "err" in page.locator("#toast").get_attribute("class").split()

        assert calls == {
            "invoice_save": 3,
            "invoice_send": 1,
            "balance": 1,
            "recheck": 1,
        }
        browser.close()


def test_classic_dashboard_balance_request_is_single_flight_and_retries(admin_client):
    booking_id = _insert_booking()
    response = admin_client.get(
        "/admin",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.status_code == 200
    html = _with_test_base(response.get_data(as_text=True))

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**_chromium_launch_options(playwright))
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_content(html, wait_until="domcontentloaded")
        page.evaluate("window.confirm = () => true")

        selector = ".btn-balance"
        page.wait_for_selector(selector)
        _assert_single_flight_failure_and_retry(
            page,
            button_selector=selector,
            invoke_twice_script=f"""
                () => {{
                  const button = document.querySelector('{selector}');
                  window.requestBalance({booking_id}, 'Admin Audit Client', button);
                  window.requestBalance({booking_id}, 'Admin Audit Client', button);
                }}
            """,
            invoke_once_script=f"""
                () => {{
                  window.requestBalance(
                    {booking_id},
                    'Admin Audit Client',
                    document.querySelector('{selector}')
                  );
                }}
            """,
            request_url="/admin/request-balance",
            error_text="Network error: forced offline regression",
            error_class="error",
            success_text="Balance request sent",
            success_class="success",
            success_payload={"success": True, "balance_due": 200.0},
        )
        browser.close()


def test_client_database_invoice_send_is_single_flight_and_retries(admin_client):
    booking_id = _insert_booking()
    booking_app.sync_client(
        "admin-audit@example.test",
        "Admin Audit Client",
        "4035550101",
        "admin.audit",
    )
    response = admin_client.get(
        "/admin/clients",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    clients_response = admin_client.get(
        "/admin/api/clients",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.status_code == 200
    assert clients_response.status_code == 200
    clients_fixture = clients_response.get_json()
    assert len(clients_fixture) == 1
    client_id = clients_fixture[0]["id"]
    detail_response = admin_client.get(
        f"/admin/api/clients/{client_id}",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert detail_response.status_code == 200
    detail_fixture = detail_response.get_json()
    html = _with_test_base(response.get_data(as_text=True))

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**_chromium_launch_options(playwright))
        page = browser.new_page()

        def route_request(route):
            path = urlsplit(route.request.url).path
            if path == "/admin/api/clients":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(clients_fixture),
                )
                return
            if path == f"/admin/api/clients/{client_id}":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(detail_fixture),
                )
                return
            route.abort()

        page.route("**/*", route_request)
        page.set_content(html, wait_until="domcontentloaded")
        page.locator(".client-card").click()

        selector = (
            f'.booking-row[data-booking-id="{booking_id}"] '
            'button[title="Send invoice to client"]'
        )
        page.wait_for_selector(selector)
        _assert_single_flight_failure_and_retry(
            page,
            button_selector=selector,
            invoke_twice_script=f"""
                () => {{
                  const button = document.querySelector('{selector}');
                  window.sendInvoice({booking_id}, button);
                  window.sendInvoice({booking_id}, button);
                }}
            """,
            invoke_once_script=f"""
                () => {{
                  window.sendInvoice(
                    {booking_id},
                    document.querySelector('{selector}')
                  );
                }}
            """,
            request_url=f"/admin/booking/{booking_id}/send-invoice",
            error_text="Failed to send invoice",
            error_class="err",
            success_text=f"Invoice sent to client for #{booking_id}",
            success_class="ok",
            success_payload={"success": True},
        )
        browser.close()


def test_event_roster_balance_request_is_single_flight_and_retries(
    admin_client,
    monkeypatch,
):
    booking_id = _insert_booking()
    event = {
        "id": "mission-mini-1",
        "title": "Mission Test Mini One",
        "date": "2099-08-01",
        "start_time": "10:00",
        "end_time": "13:00",
        "session_length": 20,
        "break_length": 10,
        "slot_interval": 30,
        "deposit": 100.0,
        "full_price": 300.0,
        "location": "Calgary test location",
        "status": "active",
    }
    monkeypatch.setattr(booking_app, "EVENTS", [event])
    response = admin_client.get(
        "/admin/event/mission-mini-1",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.status_code == 200
    html = _with_test_base(response.get_data(as_text=True))
    slots_fixture = {
        "event": event,
        "summary": {
            "booked": 1,
            "confirmed": 1,
            "pending": 0,
            "blocked": 0,
            "free": 0,
        },
        "slots": [],
        "bookings": [
            {
                "id": booking_id,
                "name": "Admin Audit Client",
                "email": "admin-audit@example.test",
                "phone": "4035550101",
                "instagram": "admin.audit",
                "time": "10:00",
                "status": "confirmed",
                "confirmed": 1,
                "paid_amount": 100.0,
                "deposit_amount": 100.0,
                "selected_addons": [],
            },
        ],
    }

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**_chromium_launch_options(playwright))
        page = browser.new_page()

        def route_request(route):
            path = urlsplit(route.request.url).path
            if path == "/admin/api/event/mission-mini-1/slots":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(slots_fixture),
                )
                return
            route.abort()

        page.route("**/*", route_request)
        page.set_content(html, wait_until="domcontentloaded")
        page.evaluate("window.confirm = () => true")

        selector = '[data-roster-action="balance"]'
        page.wait_for_selector(selector)
        _assert_single_flight_failure_and_retry(
            page,
            button_selector=selector,
            invoke_twice_script=f"""
                () => {{
                  const button = document.querySelector('{selector}');
                  window.requestBalance({booking_id}, 'Admin Audit Client', button);
                  window.requestBalance({booking_id}, 'Admin Audit Client', button);
                }}
            """,
            invoke_once_script=f"""
                () => {{
                  window.requestBalance(
                    {booking_id},
                    'Admin Audit Client',
                    document.querySelector('{selector}')
                  );
                }}
            """,
            request_url="/admin/request-balance",
            error_text="Network error: forced offline regression",
            error_class="err",
            success_text="Balance request sent",
            success_class="ok",
            success_payload={"success": True, "balance_due": 200.0},
        )
        browser.close()


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
