#!/usr/bin/env python3
"""Push monthly Meta Ads spend into the booking admin expenses ledger.

Reads the Meta token locally (never committed), queries monthly account spend
from the Graph API, and POSTs idempotent rows to /admin/api/expenses/import.
Safe to re-run any time (rows are upserted by external_id "meta:YYYY-MM").

Usage:
  ADMIN_KEY=... python3 scripts/meta_expenses_sync.py [--since 2026-01-01] \
      [--base https://book.pashynskaphoto.com]

Cron-friendly: run monthly (e.g. Mac launchd) to keep the ledger current.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

ACCT = "902493111302920"
GRAPH = "https://graph.facebook.com/v20.0"
TOKEN_FILE = pathlib.Path.home() / ".hermes/credentials/fb_tokens_pashynska.json"


def _token() -> str:
    data = json.loads(TOKEN_FILE.read_text())
    for key in ("long_lived_user_token", "user_token", "page_token"):
        val = data.get(key)
        if isinstance(val, str) and len(val) > 40:
            return val
    raise SystemExit("No Meta token available")


def monthly_spend(since: str, until: str):
    params = {
        "level": "account",
        "fields": "spend,account_currency",
        "time_increment": "monthly",
        "time_range": json.dumps({"since": since, "until": until}),
        "access_token": _token(),
        "limit": "100",
    }
    url = f"{GRAPH}/act_{ACCT}/insights?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as resp:
        payload = json.loads(resp.read())
    rows = []
    for item in payload.get("data", []):
        spend = round(float(item.get("spend") or 0), 2)
        if spend <= 0:
            continue
        month = item["date_start"][:7]
        rows.append({
            "date": f"{month}-01",
            "amount": spend,
            "category": "ads_meta",
            "vendor": "Meta Ads",
            "description": f"Meta Ads spend {month} (auto, {item.get('account_currency', 'CAD')})",
            "source": "meta_auto",
            "external_id": f"meta:{month}",
        })
    return rows


def push(base: str, admin_key: str, rows):
    req = urllib.request.Request(
        base.rstrip("/") + "/admin/api/expenses/import",
        data=json.dumps({"rows": rows}).encode(),
        headers={"Content-Type": "application/json", "X-Admin-Key": admin_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=f"{dt.date.today().year}-01-01")
    ap.add_argument("--until", default=dt.date.today().isoformat())
    ap.add_argument("--base", default="https://book.pashynskaphoto.com")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key and not args.dry_run:
        raise SystemExit("Set ADMIN_KEY env var (or use --dry-run)")

    rows = monthly_spend(args.since, args.until)
    print(f"Meta monthly spend rows: {len(rows)}")
    for row in rows:
        print(f"  {row['external_id']}: ${row['amount']:.2f}")
    if args.dry_run:
        return
    result = push(args.base, admin_key, rows)
    print("Import result:", result)
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
