from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import load_config


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite jobs database backup")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retention-days", type=int, default=14)
    return parser.parse_args()


def create_backup(database_path: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = output_dir / f"jobs-{timestamp}.db"
    temporary = destination.with_suffix(".db.tmp")
    temporary.unlink(missing_ok=True)

    source_connection = sqlite3.connect(database_path, timeout=30)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()

    temporary.replace(destination)
    return destination


def remove_expired_backups(output_dir: Path, retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=max(retention_days, 1))
    removed = 0
    for backup in output_dir.glob("jobs-*.db"):
        modified = datetime.fromtimestamp(backup.stat().st_mtime, UTC)
        if modified < cutoff:
            backup.unlink()
            removed += 1
    return removed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args()
    config = load_config()
    backup = create_backup(config.db_path, args.output_dir.resolve())
    removed = remove_expired_backups(args.output_dir.resolve(), args.retention_days)
    logger.info("SQLite backup created at %s; expired_backups_removed=%d", backup, removed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
