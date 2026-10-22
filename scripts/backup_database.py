"""Create a consistent SQLite backup without stopping MILAAN.

Usage:
  DATABASE_PATH=/srv/milaan/milaan.db python scripts/backup_database.py /srv/backups
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/backup_database.py BACKUP_DIRECTORY")
    destination = Path(sys.argv[1]).expanduser().resolve()
    if not destination.exists() or not destination.is_dir():
        raise SystemExit("BACKUP_DIRECTORY must be an existing directory")
    source = db.get_database_path()
    if not source.exists():
        raise SystemExit(f"database does not exist: {source}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = destination / f"milaan-{stamp}.sqlite3"
    if target.exists():
        raise SystemExit(f"refusing to overwrite existing backup: {target}")
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)
    with sqlite3.connect(target) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            target.unlink(missing_ok=True)
            raise SystemExit("backup integrity check failed")
    print(target)


if __name__ == "__main__":
    main()
