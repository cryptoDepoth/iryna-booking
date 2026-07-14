import pytest

# Test public review helper route

def test_review_helper_family_en_is_public_and_contains_copy_button(client):
    # Uses test_client fixture from conftest
    resp = client.get("/review-helper?style=family&lang=en")
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "Pashynska Photography" in text
    assert "Copy review text" in text or "Copy" in text
    # Should now contain the regenerate button
    assert "New version" in text or "&#8635;" in text or "rotate" in text.lower() or "New version" in text
    # Uses our stable first-party short URL, which 301s to the direct GBP form.
    assert "https://review.pashynskaphoto.com" in text


def test_review_helper_uses_ai_generation_endpoint(client):
    resp = client.get("/review-helper?style=family&lang=en")
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "/api/review-helper/generate" in text
    assert "loadAiReview" in text
    # Check JS rotateReview function still exists and now fetches fresh AI text
    assert "function rotateReview()" in text


def test_review_helper_rotate_switches_variant(client):
    resp1 = client.get("/review-helper?style=family&v=0")
    resp2 = client.get("/review-helper?style=family&v=1")
    resp3 = client.get("/review-helper?style=family&v=2")
    assert resp1.status_code == resp2.status_code == resp3.status_code == 200
    t1 = resp1.data.decode()
    t2 = resp2.data.decode()
    t3 = resp3.data.decode()
    # Variants should be different
    assert t1 != t2, "Variant 0 and 1 should differ"
    assert t2 != t3, "Variant 1 and 2 should differ"


def test_review_helper_direct_google_review_link(client):
    resp = client.get("/review-helper")
    text = resp.data.decode()
    # Should NOT link to search anymore
    assert "google.com/search?q=Pashynska" not in text
    assert "https://review.pashynskaphoto.com" in text


def test_review_helper_russian_labels(client):
    resp = client.get("/review-helper?style=family&lang=ru")
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "Новый вариант" in text, "Russian rotate button label missing"


def test_review_helper_api_base_redirects_to_page(client):
    resp = client.get("/api/review-helper/", follow_redirects=False)
    assert resp.status_code in (301, 302, 308)
    assert resp.headers["Location"].endswith("/review-helper")


def test_review_helper_api_falls_back_without_ai_key(client, monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    resp = client.get("/api/review-helper/generate?style=family&lang=en")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["review"]
    assert data["source"] == "fallback-no-key"


def test_review_helper_api_uses_zai_when_configured(client, monkeypatch):
    import app as app_module

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "Fresh AI generated review text for Iryna that sounds natural and specific."}}]}

    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    monkeypatch.setenv("ZAI_MODEL", "glm-4.5-air")
    monkeypatch.setattr(app_module.requests, "post", fake_post)

    resp = client.get("/api/review-helper/generate?style=maternity&lang=en")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["source"] == "zai:glm-4.5-air"
    assert "Fresh AI generated" in data["review"]
    assert captured["payload"]["model"] == "glm-4.5-air"


def test_review_helper_normalizes_zai_base_url(monkeypatch):
    import app as app_module
    monkeypatch.setenv("REVIEW_AI_BASE_URL", "https://api.z.ai/api/paas/v4")
    assert app_module._review_ai_endpoint() == "https://api.z.ai/api/paas/v4/chat/completions"
