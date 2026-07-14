import sys
import types

import gcal_helper


def test_env_refresh_token_is_refreshed_without_interactive_oauth(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    calls = {"refresh": 0, "flow": 0, "build": 0}

    class FakeCredentials:
        def __init__(self, **kwargs):
            self.refresh_token = kwargs["refresh_token"]
            self.valid = False
            self.expired = False

        def refresh(self, request):
            calls["refresh"] += 1
            self.valid = True

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, *args, **kwargs):
            calls["flow"] += 1
            raise AssertionError("interactive OAuth must not run in production")

    def fake_build(*args, **kwargs):
        calls["build"] += 1
        return "calendar-service"

    modules = {
        "google": types.ModuleType("google"),
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.credentials": types.SimpleNamespace(Credentials=FakeCredentials),
        "google_auth_oauthlib": types.ModuleType("google_auth_oauthlib"),
        "google_auth_oauthlib.flow": types.SimpleNamespace(InstalledAppFlow=FakeFlow),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": types.SimpleNamespace(Request=lambda: object()),
        "googleapiclient": types.ModuleType("googleapiclient"),
        "googleapiclient.discovery": types.SimpleNamespace(build=fake_build),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    assert gcal_helper._service() == "calendar-service"
    assert calls == {"refresh": 1, "flow": 0, "build": 1}
