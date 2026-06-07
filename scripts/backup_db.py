"""Daily backup of autostock.db with rotation (keeps last 7 days).

Uses SQLite's online .backup API so it's safe to run while the app is using
the database -- the API handles locking and produces a consistent snapshot.

Triggered by the Windows scheduled task `hearthkeeper-db-backup` (daily 3 AM).
Backups land in <project>/backups/ as autostock-YYYY-MM-DD.db.
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SRC = PROJECT / "autostock.db"
BACKUP_DIR = PROJECT / "backups"
RETENTION_DAYS = 7


def main():
    BACKUP_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    dest = BACKUP_DIR / f"autostock-{today}.db"

    # SQLite online backup -- consistent snapshot even with the app writing.
    src = sqlite3.connect(str(SRC))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"[backup_db] {datetime.now():%Y-%m-%d %H:%M:%S} wrote {dest.name} "
          f"({size_mb:.1f} MB)")

    # Prune anything older than RETENTION_DAYS.
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    pruned = 0
    for f in BACKUP_DIR.glob("autostock-*.db"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            pruned += 1
    if pruned:
        print(f"[backup_db] pruned {pruned} backup(s) older than "
              f"{RETENTION_DAYS} days")


if __name__ == "__main__":
    main()
