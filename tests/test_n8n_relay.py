import app as booking_app


def test_n8n_relay_rejects_missing_or_wrong_secret(client, monkeypatch):
    monkeypatch.setattr(booking_app, "N8N_WEBHOOK_SECRET", "relay-secret")

    assert client.post(
        "/webhook/pashynska-automation",
        json={"action": "site.health_check"},
    ).status_code == 401
    assert client.post(
        "/webhook/pashynska-automation",
        json={"action": "site.health_check"},
        headers={"X-Webhook-Secret": "wrong"},
    ).status_code == 401


def test_n8n_relay_validates_payload_and_forwards_only_to_local_n8n(
    client, monkeypatch
):
    monkeypatch.setattr(booking_app, "N8N_WEBHOOK_SECRET", "relay-secret")
    monkeypatch.setattr(
        booking_app,
        "N8N_LOCAL_FORWARD_URL",
        "http://127.0.0.1:5678/webhook/pashynska-automation",
    )

    assert client.post(
        "/webhook/pashynska-automation",
        json={"not_action": True},
        headers={"X-Webhook-Secret": "relay-secret"},
    ).status_code == 400

    calls = []

    class Upstream:
        content = b'{"success":true}'
        status_code = 200
        headers = {"Content-Type": "application/json"}

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Upstream()

    monkeypatch.setattr(booking_app.requests, "post", fake_post)
    response = client.post(
        "/webhook/pashynska-automation",
        json={"action": "site.health_check", "source": "test"},
        headers={"X-Webhook-Secret": "relay-secret"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    assert calls == [
        (
            "http://127.0.0.1:5678/webhook/pashynska-automation",
            {
                "json": {"action": "site.health_check", "source": "test"},
                "headers": {"Content-Type": "application/json"},
                "timeout": 10,
            },
        )
    ]
