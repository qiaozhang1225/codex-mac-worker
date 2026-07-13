#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import gzip
from pathlib import Path
import shutil
import sqlite3


def backup_database(database: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"worker-{datetime.now(UTC):%Y-%m-%d}.sqlite3"
    temporary = destination.with_suffix(".sqlite3.tmp")
    temporary.unlink(missing_ok=True)
    source_connection = sqlite3.connect(database)
    try:
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()
    temporary.replace(destination)
    return destination


def rotate_logs(log_dir: Path, *, max_bytes: int = 10 * 1024 * 1024) -> None:
    if not log_dir.exists():
        return
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    for log in log_dir.glob("*.log"):
        if not log.is_file() or log.stat().st_size <= max_bytes:
            continue
        archive = log.with_name(f"{log.name}.{stamp}.gz")
        with log.open("rb") as source, gzip.open(archive, "wb") as target:
            shutil.copyfileobj(source, target)
        log.open("wb").close()


def retain_newest(directory: Path, pattern: str, count: int) -> None:
    files = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in files[count:]:
        stale.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--retention", type=int, default=14)
    args = parser.parse_args()
    if not args.database.is_file():
        raise SystemExit(f"database does not exist: {args.database}")
    if args.retention <= 0:
        raise SystemExit("retention must be positive")
    destination = backup_database(args.database, args.backup_dir)
    rotate_logs(args.log_dir)
    retain_newest(args.backup_dir, "worker-*.sqlite3", args.retention)
    retain_newest(args.log_dir, "*.log.*.gz", args.retention * 4)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
