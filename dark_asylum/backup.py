"""Daily database backups to two locations with loud failure reporting.

Uses SQLite's online backup API (safe while the bot/portal are writing).
Archives are kept for 30 days. A failure writes an error heartbeat plus an
audit entry that the portal health page shows and the bot posts to staff.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from .db import audit, connect, heartbeat, iso, utcnow

RETENTION_DAYS = 30


def _backup_one(source: sqlite3.Connection, target_dir: Path, stamp: str) -> Path:
    """Write one consistent snapshot into ``target_dir`` and verify it opens."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"dark_asylum-{stamp}.sqlite3"
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
        # integrity_check proves the copy is readable before we trust it.
        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity check failed")
    finally:
        dest.close()
    return target


def prune(target_dir: Path, now=None) -> int:
    """Delete snapshots older than the retention period; returns count removed."""
    cutoff = (now or utcnow()) - timedelta(days=RETENTION_DAYS)
    removed = 0
    for file in target_dir.glob("dark_asylum-*.sqlite3"):
        if file.stat().st_mtime < cutoff.timestamp():
            file.unlink()
            removed += 1
    return removed


def run_backup(conn: sqlite3.Connection, primary: Path, secondary: Path | None) -> list[str]:
    """Back up to every configured location; return a list of error strings.

    The result is also persisted (heartbeat + audit) so failures can never be silent.
    """
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    errors: list[str] = []
    if secondary is None:
        errors.append("No secondary backup drive is configured")
    for label, directory in (("primary", primary), ("secondary", secondary)):
        if directory is None:
            continue
        try:
            _backup_one(conn, directory, stamp)
            prune(directory)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            errors.append(f"{label} backup failed: {exc}")
    if errors:
        heartbeat(conn, "backup", "error", "; ".join(errors))
        audit(conn, "system", "backup", "backup_failed", detail="; ".join(errors))
    else:
        heartbeat(conn, "backup", "ok", f"Completed {iso(utcnow())}")
    return errors


def backup_from_path(db_path: Path, primary: Path, secondary: Path | None) -> list[str]:
    """CLI helper: open the DB itself and run a backup."""
    conn = connect(db_path)
    try:
        return run_backup(conn, primary, secondary)
    finally:
        conn.close()
