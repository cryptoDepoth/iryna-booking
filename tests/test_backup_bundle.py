import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import app as booking_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_backup_bundle import verify_backup  # noqa: E402


def test_backup_bundle_is_consistent_and_complete(tmp_path, monkeypatch):
    main_db = tmp_path / "bookings.db"
    backup_dir = tmp_path / "backups"
    events_path = tmp_path / "events.yaml"
    gift_db = tmp_path / "gift_referral.db"
    backup_dir.mkdir()
    events_path.write_text("events: []\n", encoding="utf-8")

    monkeypatch.setattr(booking_app, "DB_PATH", str(main_db))
    monkeypatch.setattr(booking_app, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(booking_app, "_EVENTS_PATH", str(events_path))
    monkeypatch.setattr(booking_app.gift_db, "DB_PATH", str(gift_db))
    booking_app.init_db()
    booking_app.gift_db.init_db(str(gift_db))

    conn = booking_app.db_conn()
    conn.execute(
        """INSERT INTO bookings
             (date,time,name,email,phone,instagram,session_type,status,confirmed,paid)
           VALUES ('2026-08-01','15:00','Backup Client','b@example.com','','','mini','confirmed',1,1)"""
    )
    conn.commit()
    conn.close()

    bundle = booking_app.create_backup("test")
    assert bundle.endswith(".zip")
    with zipfile.ZipFile(bundle) as archive:
        assert {"bookings.db", "events.yaml", "gift_referral.db", "manifest.json"} <= set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["integrity_check"] == "ok"
        assert manifest["table_counts"]["bookings"] == 1
        archive.extract("bookings.db", tmp_path / "restore")

    restored = sqlite3.connect(tmp_path / "restore" / "bookings.db")
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
    restored.close()

    verified = verify_backup(Path(bundle))
    assert verified["verified"] is True
    assert verified["bookings"] == 1


def test_backup_verifier_rejects_incomplete_bundle(tmp_path):
    bundle = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"integrity_check": "ok", "table_counts": {}}))

    try:
        verify_backup(bundle)
    except ValueError as exc:
        assert "missing" in str(exc).lower()
    else:
        raise AssertionError("incomplete backup unexpectedly passed verification")
