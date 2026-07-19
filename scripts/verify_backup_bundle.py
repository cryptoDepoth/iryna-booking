#!/usr/bin/env python3
"""Verify a portable Pashynska production backup without restoring it."""

import argparse
import hashlib
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

import yaml


REQUIRED_FILES = {"bookings.db", "events.yaml", "gift_referral.db", "manifest.json"}


def _check_sqlite(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise ValueError(f"SQLite integrity check failed for {path.name}: {result}")


def verify_backup(path: Path) -> dict:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Backup does not exist: {path}")

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        unsafe = [
            name for name in names
            if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
        ]
        if unsafe:
            raise ValueError(f"Unsafe archive paths: {unsafe}")

        missing = REQUIRED_FILES - names
        if missing:
            raise ValueError(f"Backup is incomplete; missing: {sorted(missing)}")

        corrupt = archive.testzip()
        if corrupt:
            raise ValueError(f"ZIP CRC check failed for: {corrupt}")

        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("integrity_check") != "ok":
            raise ValueError("Backup manifest does not report a successful source integrity check")
        if not isinstance(manifest.get("table_counts"), dict):
            raise ValueError("Backup manifest is missing table counts")
        if archive.getinfo("events.yaml").file_size == 0:
            raise ValueError("events.yaml is empty")
        revision_name = "events.yaml.revision.json"
        if revision_name in names:
            revision = json.loads(archive.read(revision_name))
            try:
                datetime.fromisoformat(revision["lastmod"]).date()
                expected_sha256 = revision["sha256"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Event revision metadata is invalid") from exc
            events_data = yaml.safe_load(archive.read("events.yaml"))
            normalized = yaml.safe_dump(
                events_data,
                allow_unicode=True,
                sort_keys=True,
            ).encode("utf-8")
            actual_sha256 = hashlib.sha256(normalized).hexdigest()
            if expected_sha256 != actual_sha256:
                raise ValueError(
                    "Event revision metadata does not match events.yaml"
                )

        with tempfile.TemporaryDirectory(prefix="pashynska-backup-verify-") as temp_dir:
            target = Path(temp_dir)
            for name in ("bookings.db", "gift_referral.db"):
                archive.extract(name, target)
                _check_sqlite(target / name)

    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "bookings": int(manifest["table_counts"].get("bookings", 0)),
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    result = verify_backup(args.backup)
    print(
        "Verified backup bundle: "
        f"{result['path']} ({result['size_bytes']} bytes, "
        f"{result['bookings']} bookings)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
