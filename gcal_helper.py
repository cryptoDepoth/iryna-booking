#!/usr/bin/env python3
"""Google Calendar CLI helper used by app.py to create events on confirm.

One-time setup:
  1. In Google Cloud Console: create OAuth Client (Desktop), download credentials.json
     into this folder.
  2. pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
  3. python3 gcal_helper.py auth         # opens browser, stores token.json
  4. export GCAL_HELPER="$(pwd)/gcal_helper.py"

Then app.py calls:
  gcal_helper.py create --calendar <id> --summary <s> --start <iso> --end <iso> --tz <tz> --description <d> --location <loc>
which prints a single JSON line: {"id": "...", "htmlLink": "..."}
To reconcile a reversed confirmation it calls:
  gcal_helper.py delete --calendar <id> --event-id <event_id>
"""
import argparse
import json
import os
import re
import sys

SCOPES = ["https://www.googleapis.com/auth/calendar"]
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(HERE, "token.json")
CRED_PATH = os.path.join(HERE, "credentials.json")


def _service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    loaded_from_env = False
    # Production: token comes from env vars (no browser, no token.json file)
    refresh_token = os.environ.get("GOOGLE_CALENDAR_REFRESH_TOKEN")
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if refresh_token and client_id and client_secret:
        loaded_from_env = True
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
    elif os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        # Credentials assembled from a refresh token have no access token or
        # expiry yet, so ``expired`` can be False even though ``valid`` is
        # also False. Refresh whenever a refresh token is available instead
        # of incorrectly falling back to an interactive browser flow.
        if creds and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CRED_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        # Environment credentials are Fly secrets; do not copy them into a
        # token file in the application filesystem.
        if not loaded_from_env:
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def cmd_auth():
    _service()
    print("OK — credentials cached in token.json")


def cmd_probe(args):
    """Verify OAuth and read access without creating or changing an event."""
    svc = _service()
    calendar = svc.calendars().get(calendarId=args.calendar).execute()
    print(json.dumps({
        "ok": True,
        "calendar_id": calendar.get("id") or args.calendar,
        "time_zone": calendar.get("timeZone"),
    }))


def _find_booking_event(svc, calendar_id, booking_id):
    """Find a prior sync before inserting, including legacy events without metadata."""
    if not booking_id:
        return None

    # New events carry a private property, which is the most reliable and
    # cheapest idempotency lookup.
    response = svc.events().list(
        calendarId=calendar_id,
        privateExtendedProperty=f"booking_id={booking_id}",
        singleEvents=True,
        maxResults=1,
    ).execute()
    items = response.get("items") or []
    if items:
        return items[0]

    # Events created before the private property was introduced still include
    # "Booking #<id>" in their description. Search and verify the exact marker
    # so a database repair cannot create a duplicate calendar entry.
    marker = f"Booking #{booking_id}"
    response = svc.events().list(
        calendarId=calendar_id,
        q=marker,
        singleEvents=True,
        maxResults=10,
    ).execute()
    marker_pattern = re.compile(rf"(?m)^{re.escape(marker)}(?:\s|$)")
    for event in response.get("items") or []:
        if marker_pattern.search(event.get("description") or ""):
            return event
    return None


def cmd_create(args):
    svc = _service()
    existing = _find_booking_event(svc, args.calendar, args.booking_id)
    if existing:
        print(json.dumps({
            "id": existing.get("id"),
            "htmlLink": existing.get("htmlLink"),
            "existing": True,
        }))
        return

    body = {
        "summary": args.summary,
        "description": args.description or "",
        "location": args.location or "",
        "start": {"dateTime": args.start, "timeZone": args.tz},
        "end": {"dateTime": args.end, "timeZone": args.tz},
        "reminders": {"useDefault": False, "overrides": [
            {"method": "email", "minutes": 24 * 60},
            {"method": "popup", "minutes": 60},
        ]},
    }
    if args.booking_id:
        body["extendedProperties"] = {
            "private": {
                "booking_id": str(args.booking_id),
                "source": "pashynska-booking",
            }
        }
    ev = svc.events().insert(calendarId=args.calendar, body=body).execute()
    print(json.dumps({"id": ev.get("id"), "htmlLink": ev.get("htmlLink"), "existing": False}))


def cmd_delete(args):
    """Delete one known booking event, treating already-missing as success."""
    svc = _service()
    try:
        svc.events().delete(
            calendarId=args.calendar,
            eventId=args.event_id,
        ).execute()
    except Exception as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status not in {404, 410}:
            raise
        print(json.dumps({
            "deleted": False,
            "missing": True,
            "id": args.event_id,
        }))
        return
    print(json.dumps({
        "deleted": True,
        "missing": False,
        "id": args.event_id,
    }))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    probe = sub.add_parser("probe")
    probe.add_argument("--calendar", required=True)
    cr = sub.add_parser("create")
    cr.add_argument("--calendar", required=True)
    cr.add_argument("--booking-id")
    cr.add_argument("--summary", required=True)
    cr.add_argument("--start", required=True)
    cr.add_argument("--end", required=True)
    cr.add_argument("--tz", default="America/Edmonton")
    cr.add_argument("--description", default="")
    cr.add_argument("--location", default="")
    delete = sub.add_parser("delete")
    delete.add_argument("--calendar", required=True)
    delete.add_argument("--event-id", required=True)
    args = p.parse_args()
    if args.cmd == "auth":
        cmd_auth()
    elif args.cmd == "probe":
        cmd_probe(args)
    elif args.cmd == "create":
        cmd_create(args)
    elif args.cmd == "delete":
        cmd_delete(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        sys.exit(1)
