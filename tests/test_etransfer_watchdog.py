import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import app as booking_app
import check_etransfer_v2 as checker


class FakeTime:
    def __init__(self, now=1_000.0):
        self.now = float(now)
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class BusyLock:
    def acquire(self, blocking=False):
        assert blocking is False
        return False

    def release(self):
        raise AssertionError("A lock that was not acquired must not be released")


class AvailableLock:
    def __init__(self):
        self.released = False

    def acquire(self, blocking=False):
        assert blocking is False
        return True

    def release(self):
        self.released = True


@pytest.fixture(autouse=True)
def reset_watchdog_state():
    booking_app._reset_etransfer_watchdog_state()
    checker._EMAIL_FETCH_CACHE.update({
        "ts": 0.0,
        "page_size": None,
        "lookback_days": None,
        "emails": None,
    })
    checker.reset_email_fetch_status()
    checker.consume_last_body_read_status()
    yield
    booking_app._reset_etransfer_watchdog_state()
    checker._EMAIL_FETCH_CACHE.update({
        "ts": 0.0,
        "page_size": None,
        "lookback_days": None,
        "emails": None,
    })
    checker.reset_email_fetch_status()
    checker.consume_last_body_read_status()


def _configure_fast_watcher(monkeypatch):
    monkeypatch.setenv("ETRANSFER_EMAIL_POLL_INTERVAL", "0")
    monkeypatch.setenv("ETRANSFER_RECONCILIATION_INTERVAL", "999999")
    monkeypatch.setattr(checker, "get_pending_bookings", lambda within_minutes=30: [{"id": 1}])
    monkeypatch.setattr(checker, "get_reconciliation_bookings", lambda within_days=120: [])
    monkeypatch.setattr(booking_app, "expire_reservations", lambda: 0)


def test_scan_exception_and_sender_failure_are_contained_then_fresh_scan_recovers(monkeypatch):
    _configure_fast_watcher(monkeypatch)
    clock = FakeTime()
    calls = {"fetch": 0, "notify": 0}

    def fetch_then_recover(**_kwargs):
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            raise RuntimeError("SECRET provider response must never reach health or Telegram")
        checker._set_email_fetch_status("fresh")
        return []

    def failing_sender(_message, **_kwargs):
        calls["notify"] += 1
        raise RuntimeError("Telegram unavailable")

    monkeypatch.setattr(checker, "get_emails", fetch_then_recover)
    monkeypatch.setattr(booking_app, "_notify_admin", failing_sender)

    booking_app._watcher_thread(max_cycles=2, time_module=clock)

    state = booking_app._watcher_state_snapshot()
    assert calls == {"fetch": 2, "notify": 1}
    assert state["status"] == "healthy"
    assert state["detail"] is None
    assert state["last_attempt_at"] == "1970-01-01T00:17:10+00:00"
    assert state["last_success_at"] == "1970-01-01T00:17:10+00:00"
    assert state["last_email_count"] == 0


def test_repeated_scan_failures_alert_once_per_throttle_window(monkeypatch):
    _configure_fast_watcher(monkeypatch)
    clock = FakeTime()
    alerts = []

    monkeypatch.setattr(
        checker,
        "get_emails",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("raw-provider-secret")),
    )
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, **_kwargs: alerts.append(message))
    monkeypatch.setattr(booking_app, "ETRANSFER_SCAN_ALERT_THROTTLE_SECONDS", 60)

    booking_app._watcher_thread(max_cycles=3, time_module=clock)

    assert len(alerts) == 2
    assert all("raw-provider-secret" not in message for message in alerts)
    assert all("scan exception" in message.lower() for message in alerts)
    state = booking_app._watcher_state_snapshot()
    assert state["status"] == "failed"
    assert state["detail"] == "scan_exception"
    assert state["last_success_at"] is None


def test_busy_fetch_lock_with_cached_results_is_processed_but_degraded(monkeypatch):
    _configure_fast_watcher(monkeypatch)
    clock = FakeTime()
    alerts = []
    processed_batches = []
    checker._EMAIL_FETCH_CACHE.update({
        "ts": 0.0,
        "page_size": 25,
        "lookback_days": 7,
        "emails": [{"id": "cached-message"}],
    })
    monkeypatch.setattr(checker, "_EMAIL_FETCH_LOCK", BusyLock())
    monkeypatch.setattr(
        booking_app,
        "_process_etransfer_email_batch",
        lambda emails, pending, reconciliation: (
            processed_batches.append((emails, pending, reconciliation)) or []
        ),
    )
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, **_kwargs: alerts.append(message))

    booking_app._watcher_thread(max_cycles=1, time_module=clock)

    state = booking_app._watcher_state_snapshot()
    assert state["status"] == "degraded"
    assert state["detail"] == "fetch_busy_cached"
    assert state["last_success_at"] is None
    assert len(processed_batches) == 1
    assert len(alerts) == 1


def test_recent_cache_hit_is_processed_but_not_counted_as_fresh(monkeypatch):
    _configure_fast_watcher(monkeypatch)
    clock = FakeTime()
    alerts = []
    processed_batches = []
    checker._EMAIL_FETCH_CACHE.update({
        "ts": checker.time.time(),
        "page_size": 25,
        "lookback_days": 7,
        "emails": [{"id": "recent-cached-message"}],
    })
    monkeypatch.setattr(
        booking_app,
        "_process_etransfer_email_batch",
        lambda emails, pending, reconciliation: (
            processed_batches.append((emails, pending, reconciliation)) or []
        ),
    )
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, **_kwargs: alerts.append(message))

    booking_app._watcher_thread(max_cycles=1, time_module=clock)

    state = booking_app._watcher_state_snapshot()
    assert state["status"] == "skipped"
    assert state["detail"] == "fetch_cached"
    assert state["last_success_at"] is None
    assert len(processed_batches) == 1
    assert alerts == []


def test_envelope_list_failure_records_sanitized_failure(monkeypatch):
    _configure_fast_watcher(monkeypatch)
    clock = FakeTime()
    alerts = []
    lock = AvailableLock()
    calls = []
    checker._EMAIL_FETCH_CACHE.update({
        "ts": 0.0,
        "page_size": None,
        "lookback_days": None,
        "emails": None,
    })

    def failed_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=2, stdout="", stderr="oauth SECRET-TOKEN rejected")

    monkeypatch.setattr(checker, "_EMAIL_FETCH_LOCK", lock)
    monkeypatch.setattr(checker.subprocess, "run", failed_run)
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, **_kwargs: alerts.append(message))

    booking_app._watcher_thread(max_cycles=1, time_module=clock)

    state = booking_app._watcher_state_snapshot()
    assert len(calls) == 2
    assert lock.released is True
    assert state["status"] == "failed"
    assert state["detail"] == "envelope_fetch_failed"
    assert "SECRET-TOKEN" not in json.dumps(state)
    assert "SECRET-TOKEN" not in alerts[0]
    assert state["last_success_at"] is None


def test_interac_body_read_failure_is_degraded_without_database_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "watchdog.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE bookings (id INTEGER PRIMARY KEY, status TEXT, paid INTEGER, confirmed INTEGER)"
    )
    conn.execute(
        "CREATE TABLE processed_emails ("
        "id INTEGER PRIMARY KEY, message_id TEXT UNIQUE, booking_id INTEGER, amount REAL)"
    )
    conn.execute(
        "CREATE TABLE etransfers ("
        "id INTEGER PRIMARY KEY, message_id TEXT, status TEXT, amount REAL)"
    )
    conn.execute("INSERT INTO bookings VALUES (1, 'pending_payment', 0, 0)")
    conn.commit()
    conn.close()

    _configure_fast_watcher(monkeypatch)
    clock = FakeTime()
    alerts = []
    envelope = {
        "id": "body-failure",
        "subject": "Interac e-Transfer: You've received $120.75",
        "from": {"addr": "notify@payments.interac.ca"},
    }

    monkeypatch.setattr(checker, "DB_PATH", str(db_path))
    monkeypatch.setattr(checker, "get_emails", lambda **_kwargs: [envelope])
    monkeypatch.setattr(checker, "get_last_email_fetch_status", lambda: {"outcome": "fresh"})
    monkeypatch.setattr(checker, "read_message_body", lambda _message_id: None)
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, **_kwargs: alerts.append(message))

    booking_app._watcher_thread(max_cycles=1, time_module=clock)

    state = booking_app._watcher_state_snapshot()
    assert state["status"] == "degraded"
    assert state["detail"] == "message_body_unavailable"
    assert state["last_success_at"] is None
    assert len(alerts) == 1

    conn = sqlite3.connect(db_path)
    booking = conn.execute(
        "SELECT status, paid, confirmed FROM bookings WHERE id=1"
    ).fetchone()
    processed_count = conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0]
    ledger_count = conn.execute("SELECT COUNT(*) FROM etransfers").fetchone()[0]
    conn.close()
    assert booking == ("pending_payment", 0, 0)
    assert processed_count == 0
    assert ledger_count == 0


def test_eligible_work_becomes_overdue_without_advancing_last_success(monkeypatch):
    monkeypatch.setenv("ETRANSFER_EMAIL_POLL_INTERVAL", "2000")
    monkeypatch.setenv("ETRANSFER_RECONCILIATION_INTERVAL", "999999")
    monkeypatch.setattr(checker, "get_pending_bookings", lambda within_minutes=30: [{"id": 1}])
    monkeypatch.setattr(checker, "get_reconciliation_bookings", lambda within_days=120: [])
    monkeypatch.setattr(booking_app, "expire_reservations", lambda: 0)
    monkeypatch.setattr(booking_app, "ETRANSFER_SCAN_OVERDUE_SECONDS", 60)
    alerts = []
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, **_kwargs: alerts.append(message))
    monkeypatch.setattr(
        checker,
        "get_emails",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Email poll is throttled in this overdue test")
        ),
    )
    booking_app._record_etransfer_scan_success(900.0, email_count=0)

    booking_app._watcher_thread(max_cycles=1, time_module=FakeTime(now=1_000.0))

    state = booking_app._watcher_state_snapshot()
    assert state["status"] == "overdue"
    assert state["detail"] == "fresh_scan_overdue"
    assert state["last_success_at"] == "1970-01-01T00:15:00+00:00"
    assert len(alerts) == 1


def test_repeated_cached_scan_becomes_overdue_without_advancing_success(monkeypatch):
    _configure_fast_watcher(monkeypatch)
    monkeypatch.setattr(booking_app, "ETRANSFER_SCAN_OVERDUE_SECONDS", 60)
    checker._EMAIL_FETCH_CACHE.update({
        "ts": checker.time.time(),
        "page_size": 25,
        "lookback_days": 7,
        "emails": [],
    })
    alerts = []
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, **_kwargs: alerts.append(message))
    booking_app._record_etransfer_scan_success(900.0, email_count=0)

    booking_app._watcher_thread(max_cycles=1, time_module=FakeTime(now=1_000.0))

    state = booking_app._watcher_state_snapshot()
    assert state["status"] == "overdue"
    assert state["detail"] == "fresh_scan_overdue"
    assert state["last_attempt_at"] == "1970-01-01T00:16:40+00:00"
    assert state["last_success_at"] == "1970-01-01T00:15:00+00:00"
    assert len(alerts) == 1


def test_reconciliation_only_cached_scan_becomes_overdue_between_refreshes(monkeypatch):
    monkeypatch.setenv("ETRANSFER_EMAIL_POLL_INTERVAL", "0")
    monkeypatch.setenv("ETRANSFER_RECONCILIATION_INTERVAL", "1800")
    monkeypatch.setattr(checker, "get_pending_bookings", lambda within_minutes=30: [])
    reconciliation_calls = []

    def get_reconciliation_bookings(within_days=120):
        reconciliation_calls.append(within_days)
        return [{"id": 42}]

    monkeypatch.setattr(checker, "get_reconciliation_bookings", get_reconciliation_bookings)
    monkeypatch.setattr(booking_app, "expire_reservations", lambda: 0)
    monkeypatch.setattr(booking_app, "ETRANSFER_SCAN_OVERDUE_SECONDS", 60)
    fetch_calls = []
    monkeypatch.setattr(
        checker,
        "get_emails",
        lambda **_kwargs: fetch_calls.append(_kwargs) or [],
    )
    monkeypatch.setattr(checker, "get_last_email_fetch_status", lambda: {"outcome": "cache_hit"})
    alerts = []
    monkeypatch.setattr(booking_app, "_notify_admin", lambda message, **_kwargs: alerts.append(message))
    booking_app._record_etransfer_scan_success(2_000.0, email_count=0)

    booking_app._watcher_thread(max_cycles=3, time_module=FakeTime(now=2_000.0))

    state = booking_app._watcher_state_snapshot()
    assert reconciliation_calls == [120]
    assert len(fetch_calls) == 1
    assert state["status"] == "overdue"
    assert state["detail"] == "fresh_scan_overdue"
    assert state["last_attempt_at"] == "1970-01-01T00:33:20+00:00"
    assert state["last_success_at"] == "1970-01-01T00:33:20+00:00"
    assert len(alerts) == 1


def test_scan_health_is_admin_only_sanitized_and_public_liveness_stays_green(tmp_path, monkeypatch):
    db_path = tmp_path / "health.db"
    monkeypatch.setattr(booking_app, "DB_PATH", str(db_path))
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "watchdog-admin")
    booking_app.init_db()
    booking_app._record_etransfer_scan_issue(
        status="failed",
        detail="envelope_fetch_failed",
        at_epoch=1_000.0,
        notify=False,
    )

    with booking_app.app.test_client() as client:
        anonymous = client.get("/admin/health", headers={"Accept": "application/json"})
        public_health = client.get("/healthz")
        authenticated = client.get(
            "/admin/health",
            headers={"Accept": "application/json", "X-Admin-Key": "watchdog-admin"},
        )

    assert anonymous.status_code == 401
    assert public_health.status_code == 200
    assert public_health.get_json() == {"ok": True, "service": "iryna-booking"}
    assert authenticated.status_code == 503
    scan = authenticated.get_json()["checks"]["etransfer_scan"]
    assert scan == {
        "ok": False,
        "status": "failed",
        "detail": "envelope_fetch_failed",
        "last_attempt_at": "1970-01-01T00:16:40+00:00",
        "last_success_at": None,
        "last_email_count": 0,
    }
    assert "raw-provider-secret" not in authenticated.get_data(as_text=True)


def test_admin_health_renders_degraded_watchdog_card_in_browser(client):
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
    response = client.get("/admin/health-center")
    assert response.status_code == 200
    html = response.get_data(as_text=True).replace(
        "<head>",
        '<head><base href="http://booking.test/">',
        1,
    )
    health_fixture = {
        "healthy": False,
        "timestamp": "2026-07-19T05:30:00Z",
        "checks": {
            "etransfer_scan": {
                "ok": False,
                "status": "degraded",
                "detail": "message_body_unavailable",
                "last_attempt_at": "2026-07-19T05:29:30+00:00",
                "last_success_at": "2026-07-19T05:20:00+00:00",
                "last_email_count": 1,
            }
        },
    }

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        if not Path(playwright.chromium.executable_path).exists():
            candidates = sorted(
                (Path.home() / "Library" / "Caches" / "ms-playwright").glob(
                    "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/"
                    "chrome-headless-shell"
                )
            )
            assert candidates, "A local Playwright Chromium binary is required for the DOM regression"
            launch_options["executable_path"] = str(candidates[-1])
        browser = playwright.chromium.launch(**launch_options)
        page = browser.new_page()
        page.route(
            "http://booking.test/admin/health",
            lambda route: route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps(health_fixture),
            ),
        )
        page.route(
            "http://booking.test/admin/backups",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body="[]",
            ),
        )
        page.set_content(html, wait_until="domcontentloaded")
        card = page.locator("article.card", has_text="e-Transfer scan")
        card.wait_for()

        assert card.locator(".pill").inner_text() == "Attention"
        card_text = card.inner_text()
        assert "degraded" in card_text
        assert "message_body_unavailable" in card_text
        assert "2026-07-19T05:29:30+00:00" in card_text
        assert "2026-07-19T05:20:00+00:00" in card_text
        assert "raw-provider-secret" not in page.locator("body").inner_text()
        browser.close()


def test_production_import_with_background_threads_disabled_starts_no_app_loops(tmp_path):
    env = os.environ.copy()
    env.update({
        "FLASK_ENV": "production",
        "DISABLE_BACKGROUND_THREADS": "1",
        "DB_PATH": str(tmp_path / "process.db"),
        "GIFT_REFERRAL_DB": str(tmp_path / "gift.db"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "ADMIN_KEY": "test",
        "ADMIN_PASSWORD": "test",
        "STRIPE_SECRET_KEY": "",
        "STRIPE_WEBHOOK_SECRET": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "N8N_WEBHOOK_URL": "",
        "RECAPTCHA_SECRET_KEY": "",
        "NOTION_API_KEY": "",
        "GCAL_HELPER": "",
        "GOOGLE_CALENDAR_REFRESH_TOKEN": "",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
    })
    script = (
        "import json, threading, app; "
        "print(json.dumps(sorted(t.name for t in threading.enumerate())))"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    thread_names = json.loads(result.stdout.strip().splitlines()[-1])
    assert not {"etransfer-watcher", "email-scheduler", "daily-backup"}.intersection(thread_names)
