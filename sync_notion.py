#!/usr/bin/env python3
"""
Sync SQLite bookings to Notion database.
Run: python3 sync_notion.py

Prerequisites:
1. In Notion, create a page (or use existing)
2. Click "..." → "Connect to" → select your integration
3. Copy page ID from URL (the part after notion.so/ and before ?)
4. Set NOTION_PAGE_ID below and run this script
"""

import os
import sqlite3
import requests

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "355510b9-cc5b-818c-aec6-d764f116e2b2")  # 📸 Session Bookings

DB_PATH = os.path.join(os.path.dirname(__file__), "bookings.db")
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

def get_database_id():
    """Return the hardcoded database ID"""
    return NOTION_DATABASE_ID

def sync_bookings(database_id):
    """Read SQLite and sync to Notion"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bookings ORDER BY time")
    rows = c.fetchall()
    conn.close()

    # Get existing pages in database
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    r = requests.post(url, headers=HEADERS, json={})
    existing = {page["properties"]["Booking ID"]["number"]: page["id"] 
                for page in r.json().get("results", [])
                if page.get("properties",{}).get("Booking ID",{}).get("number")}

    for row in rows:
        booking = dict(row)
        status_map = {
            "reserved": "Reserved",
            "pending": "Pending Payment",
            "confirmed": "Confirmed"
        }
        status = status_map.get(booking["status"], "Pending Payment")
        if booking["confirmed"]:
            status = "Confirmed"

        properties = {
            "Name": {"title": [{"text": {"content": booking["name"] or "(no name)"}}]},
            "Date": {"date": {"start": booking["date"]}},
            "Time": {"rich_text": [{"text": {"content": booking["time"]}}]},
            "Status": {"select": {"name": status}},
            "Instagram": {"rich_text": [{"text": {"content": booking.get("instagram","") or ""}}]},
            "Email": {"email": booking["email"] or None},
            "Phone": {"phone_number": booking["phone"] or None},
            "Session Type": {"rich_text": [{"text": {"content": booking.get("session_type","") or ""}}]},
            "Amount": {"number": booking.get("paid_amount") or 95},
            "Paid": {"checkbox": bool(booking["paid"])},
            "Booking ID": {"number": booking["id"]},
        }

        if booking["id"] in existing:
            # Update
            page_id = existing[booking["id"]]
            requests.patch(f"https://api.notion.com/v1/pages/{page_id}", 
                          headers=HEADERS, json={"properties": properties})
            print(f"Updated booking #{booking['id']}: {booking['name']} @ {booking['time']}")
        else:
            # Create
            payload = {
                "parent": {"database_id": database_id},
                "properties": properties
            }
            r = requests.post("https://api.notion.com/v1/pages", headers=HEADERS, json=payload)
            if r.status_code == 200:
                print(f"Created booking #{booking['id']}: {booking['name']} @ {booking['time']}")
            else:
                print(f"Error creating #{booking['id']}: {r.text}")

def main():
    db_id = get_database_id()
    if not db_id:
        print("❌ No database ID configured")
        return

    print(f"Syncing to database: {db_id}")
    sync_bookings(db_id)
    print("Done!")

if __name__ == "__main__":
    main()
