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
"""
import argparse
import json
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(HERE, "token.json")
CRED_PATH = os.path.join(HERE, "credentials.json")


def _service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CRED_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def cmd_auth():
    _service()
    print("OK — credentials cached in token.json")


def cmd_create(args):
    svc = _service()
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
    ev = svc.events().insert(calendarId=args.calendar, body=body).execute()
    print(json.dumps({"id": ev.get("id"), "htmlLink": ev.get("htmlLink")}))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    cr = sub.add_parser("create")
    cr.add_argument("--calendar", required=True)
    cr.add_argument("--summary", required=True)
    cr.add_argument("--start", required=True)
    cr.add_argument("--end", required=True)
    cr.add_argument("--tz", default="America/Edmonton")
    cr.add_argument("--description", default="")
    cr.add_argument("--location", default="")
    args = p.parse_args()
    if args.cmd == "auth":
        cmd_auth()
    elif args.cmd == "create":
        cmd_create(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        sys.exit(1)
