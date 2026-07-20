import sys
import types
from argparse import Namespace

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


def test_probe_is_read_only_and_reports_calendar(monkeypatch, capsys):
    calls = {"get": 0}

    class FakeRequest:
        def execute(self):
            return {"id": "iryna@example.com", "timeZone": "America/Edmonton"}

    class FakeCalendars:
        def get(self, calendarId):
            calls["get"] += 1
            assert calendarId == "iryna@example.com"
            return FakeRequest()

    class FakeService:
        def calendars(self):
            return FakeCalendars()

    monkeypatch.setattr(gcal_helper, "_service", lambda: FakeService())
    gcal_helper.cmd_probe(Namespace(calendar="iryna@example.com"))

    assert calls["get"] == 1
    output = capsys.readouterr().out
    assert '"ok": true' in output
    assert '"time_zone": "America/Edmonton"' in output


def test_find_booking_event_prefers_private_idempotency_marker():
    calls = []

    class Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class Events:
        def list(self, **kwargs):
            calls.append(kwargs)
            return Request({"items": [{"id": "existing-42", "htmlLink": "https://calendar/existing-42"}]})

    class Service:
        def events(self):
            return Events()

    event = gcal_helper._find_booking_event(Service(), "iryna@example.com", "42")
    assert event["id"] == "existing-42"
    assert calls == [{
        "calendarId": "iryna@example.com",
        "privateExtendedProperty": "booking_id=42",
        "singleEvents": True,
        "maxResults": 1,
    }]


def test_find_booking_event_falls_back_to_legacy_description():
    calls = []

    class Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class Events:
        def list(self, **kwargs):
            calls.append(kwargs)
            if "privateExtendedProperty" in kwargs:
                return Request({"items": []})
            return Request({"items": [
                {"id": "wrong", "description": "Booking #420"},
                {"id": "legacy-42", "description": "Client details\nBooking #42\nLocation"},
            ]})

    class Service:
        def events(self):
            return Events()

    event = gcal_helper._find_booking_event(Service(), "iryna@example.com", "42")
    assert event["id"] == "legacy-42"
    assert calls[1]["q"] == "Booking #42"


def test_delete_removes_exact_calendar_event(monkeypatch, capsys):
    calls = []

    class Request:
        def execute(self):
            return None

    class Events:
        def delete(self, **kwargs):
            calls.append(kwargs)
            return Request()

    class Service:
        def events(self):
            return Events()

    monkeypatch.setattr(gcal_helper, "_service", lambda: Service())

    gcal_helper.cmd_delete(
        Namespace(calendar="iryna@example.com", event_id="calendar-event-42")
    )

    assert calls == [{
        "calendarId": "iryna@example.com",
        "eventId": "calendar-event-42",
    }]
    output = capsys.readouterr().out
    assert '"deleted": true' in output
    assert '"missing": false' in output


def test_delete_treats_missing_calendar_event_as_reconciled(monkeypatch, capsys):
    class MissingError(Exception):
        def __init__(self):
            self.resp = Namespace(status=404)

    class Request:
        def execute(self):
            raise MissingError()

    class Events:
        def delete(self, **kwargs):
            return Request()

    class Service:
        def events(self):
            return Events()

    monkeypatch.setattr(gcal_helper, "_service", lambda: Service())

    gcal_helper.cmd_delete(
        Namespace(calendar="iryna@example.com", event_id="missing-event-42")
    )

    output = capsys.readouterr().out
    assert '"deleted": false' in output
    assert '"missing": true' in output
