"""SQLite access shared by the bot and the portal."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Format a datetime exactly like the schema's default timestamps."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    """Current UTC time as an ISO string."""
    return iso(utcnow())


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with safe pragmas.

    WAL lets the bot and portal read/write concurrently; foreign keys and a
    busy timeout prevent silent corruption and spurious "database is locked".
    ``isolation_level=None`` means we manage transactions explicitly.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply any unapplied ``migrations/NNN_*.sql`` files in order.

    Returns the names applied. Each file runs in its own transaction.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied: list[str] = []
    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if file.name in done:
            continue
        # executescript commits implicitly, so wrap with an explicit script-level transaction.
        conn.executescript(
            f"BEGIN;\n{file.read_text()}\nINSERT INTO schema_migrations VALUES ('{file.name}', '{now_iso()}');\nCOMMIT;"
        )
        applied.append(file.name)
    return applied


class Tx:
    """Context manager for an immediate (write-locking) transaction.

    Using BEGIN IMMEDIATE serialises read-modify-write sequences such as
    "check bank balance then withdraw", so concurrent writers cannot overdraw.
    """

    def __init__(self, conn: sqlite3.Connection):
        """Remember the connection this transaction will run on."""
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        """Open an immediate write transaction so concurrent writers queue instead of failing late."""
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Commit on success; roll back if the block raised, and never swallow the error."""
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Read one configuration value."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a configuration value (callers are responsible for auditing)."""
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso()),
    )


def heartbeat(conn: sqlite3.Connection, name: str, status: str = "ok", detail: str = "") -> None:
    """Record that a component (bot, backup...) is alive, or failing."""
    conn.execute(
        "INSERT INTO heartbeats(name,updated_at,status,detail) VALUES(?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at, "
        "status=excluded.status, detail=excluded.detail",
        (name, now_iso(), status, detail),
    )


def purge_old_audit(conn: sqlite3.Connection, days: int = 90) -> int:
    """Delete audit rows older than the retention window; returns rows removed."""
    cutoff = iso(utcnow() - timedelta(days=days))
    return conn.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,)).rowcount


def audit(
    conn: sqlite3.Connection,
    source: str,
    category: str,
    action: str,
    actor_id: int | None = None,
    target: str | None = None,
    before: str | None = None,
    after: str | None = None,
    detail: str | None = None,
) -> None:
    """Append an important-action audit entry (routine lookups must not call this)."""
    conn.execute(
        "INSERT INTO audit_log(source,actor_id,category,action,target,before,after,detail) VALUES(?,?,?,?,?,?,?,?)",
        (source, actor_id, category, action, target, before, after, detail),
    )
