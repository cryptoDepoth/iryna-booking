import app as booking_app


class DummyResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True, "result": {}}


def _callback_payload(username="random_user", user_id=123456, data="confirm:999"):
    return {
        "callback_query": {
            "id": "cb-test-1",
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Test",
                "username": username,
            },
            "message": {
                "message_id": 777,
                "chat": {"id": 555, "type": "private"},
            },
            "data": data,
        }
    }


def test_telegram_callback_rejects_non_whitelisted_user(monkeypatch):
    monkeypatch.setattr(booking_app, "TELEGRAM_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(booking_app, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(booking_app, "TELEGRAM_CHAT_ID", "792920251")
    monkeypatch.setattr(booking_app, "TELEGRAM_ADMIN_CHAT_ID", "792920251")
    monkeypatch.setattr(booking_app, "TELEGRAM_ALLOWED_ADMIN_USERNAMES", "pashynskaphoto")
    monkeypatch.setattr(booking_app, "TELEGRAM_ALLOWED_ADMIN_USER_IDS", "")

    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append({"url": url, "json": json, "timeout": timeout})
        return DummyResponse()

    def fail_db_conn():
        raise AssertionError("unauthorized callback must not touch DB")

    monkeypatch.setattr(booking_app.requests, "post", fake_post)
    monkeypatch.setattr(booking_app, "db_conn", fail_db_conn)

    resp = booking_app.app.test_client().post(
        "/telegram/webhook",
        json=_callback_payload(username="not_iryna", user_id=111),
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
    )

    assert resp.status_code == 200
    assert posts, "callback should be answered"
    assert posts[0]["json"]["show_alert"] is True
    assert "not enabled" in posts[0]["json"]["text"]


def test_telegram_callback_allows_pashynskaphoto_username(monkeypatch):
    monkeypatch.setattr(booking_app, "TELEGRAM_ALLOWED_ADMIN_USERNAMES", "pashynskaphoto")
    monkeypatch.setattr(booking_app, "TELEGRAM_ALLOWED_ADMIN_USER_IDS", "")

    assert booking_app._is_telegram_admin_callback({
        "from": {"id": 222, "username": "pashynskaphoto"}
    })


def test_telegram_callback_allows_iryna_stable_user_id(monkeypatch):
    """Username can change; Iryna's numeric Telegram ID is the durable grant."""
    monkeypatch.setattr(booking_app, "TELEGRAM_ALLOWED_ADMIN_USERNAMES", "")
    monkeypatch.setattr(
        booking_app,
        "TELEGRAM_ALLOWED_ADMIN_USER_IDS",
        "792920251,938104602",
    )

    assert booking_app._is_telegram_admin_callback({
        "from": {"id": 938104602, "username": "renamed_later"}
    })


def test_telegram_admin_chat_ids_include_extra_admins(monkeypatch):
    monkeypatch.setattr(booking_app, "TELEGRAM_CHAT_ID", "100")
    monkeypatch.setattr(booking_app, "TELEGRAM_ADMIN_CHAT_ID", "200")
    monkeypatch.setattr(booking_app, "TELEGRAM_EXTRA_ADMIN_CHAT_IDS", "300, 400 500")

    assert booking_app._telegram_admin_chat_ids() == ["100", "200", "300", "400", "500"]
