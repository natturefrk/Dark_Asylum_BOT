"""Ticket lifecycle and local transcript archive (no Discord objects, so it is testable).

The bot gathers messages and calls these functions; the portal reads the results.
Archives live under ARCHIVE_DIR, are indexed in SQLite with *relative* paths, and
are never deleted automatically.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .db import Tx, audit, now_iso

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # skip anything larger than this when archiving


class TicketError(ValueError):
    """A rejected ticket operation with a user-safe message."""


def sanitize_filename(name: str) -> str:
    """Reduce an untrusted attachment name to a safe basename."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)[:80].strip("._")
    return base or "file"


def _log(conn: sqlite3.Connection, ticket_id: int, actor_id: int, event: str, detail: str | None = None) -> None:
    """Append to the ticket's own history."""
    conn.execute(
        "INSERT INTO ticket_log(ticket_id,actor_id,event,detail) VALUES(?,?,?,?)",
        (ticket_id, actor_id, event, detail),
    )


def enabled_types(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Categories offered to members."""
    return conn.execute("SELECT * FROM ticket_types WHERE enabled=1 ORDER BY id").fetchall()


def create_ticket(conn: sqlite3.Connection, opener_id: int, type_name: str, thread_id: int) -> int:
    """Record a newly opened ticket (the thread already exists in Discord)."""
    with Tx(conn):
        cur = conn.execute(
            "INSERT INTO tickets(opener_id,type_name,thread_id) VALUES(?,?,?)", (opener_id, type_name, thread_id)
        )
        _log(conn, cur.lastrowid, opener_id, "opened", type_name)
        return cur.lastrowid


def get_by_thread(conn: sqlite3.Connection, thread_id: int) -> sqlite3.Row | None:
    """Find a ticket from its Discord thread id."""
    return conn.execute("SELECT * FROM tickets WHERE thread_id=?", (thread_id,)).fetchone()


def claim_ticket(conn: sqlite3.Connection, ticket_id: int, staff_id: int) -> int | None:
    """Make ``staff_id`` the single claimant; returns the previous claimant if taken over."""
    with Tx(conn):
        row = conn.execute("SELECT status, claimed_by FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None or row["status"] != "open":
            raise TicketError("That ticket is not open.")
        if row["claimed_by"] == staff_id:
            raise TicketError("You already claimed this ticket.")
        conn.execute("UPDATE tickets SET claimed_by=? WHERE id=?", (staff_id, ticket_id))
        previous = row["claimed_by"]
        _log(conn, ticket_id, staff_id, "takeover" if previous else "claimed", f"from {previous}" if previous else None)
        return previous


def close_ticket(conn: sqlite3.Connection, ticket_id: int, closer_id: int, reason: str = "") -> None:
    """Mark a ticket closed. Closed tickets stay archived; the opener starts a new one."""
    with Tx(conn):
        row = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if row is None or row["status"] != "open":
            raise TicketError("That ticket is already closed.")
        conn.execute(
            "UPDATE tickets SET status='closed', closed_at=?, closed_by=?, close_reason=? WHERE id=?",
            (now_iso(), closer_id, reason or None, ticket_id),
        )
        _log(conn, ticket_id, closer_id, "closed", reason or None)
        audit(conn, "bot", "tickets", "ticket_closed", closer_id, str(ticket_id), None, None, reason or None)


def archive_ticket(conn: sqlite3.Connection, archive_dir: Path, ticket_id: int, messages: list[dict]) -> Path:
    """Write the transcript and attachments to disk and index them in the database.

    ``messages`` are dicts: author_id, author_name, created_at, content and
    attachments (a list of ``(filename, bytes)``). Returns the transcript path.
    """
    folder = archive_dir / "tickets" / str(ticket_id)
    (folder / "attachments").mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, msg in enumerate(messages):
        lines.append(f"[{msg['created_at']}] {msg['author_name']} ({msg['author_id']}): {msg['content']}")
        for att_no, (name, data) in enumerate(msg.get("attachments", [])):
            if len(data) > MAX_ATTACHMENT_BYTES:
                lines.append(f"    (attachment {name} skipped: too large)")
                continue
            stored = folder / "attachments" / f"{index}_{att_no}_{sanitize_filename(name)}"
            stored.write_bytes(data)
            conn.execute(
                "INSERT INTO ticket_attachments(ticket_id,original_name,stored_path,size_bytes) VALUES(?,?,?,?)",
                (ticket_id, name[:200], str(stored.relative_to(archive_dir)), len(data)),
            )
            lines.append(f"    (attachment: {name})")
    transcript = folder / "transcript.txt"
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn.execute(
        "INSERT OR REPLACE INTO ticket_archives(ticket_id,transcript_path,message_count) VALUES(?,?,?)",
        (ticket_id, str(transcript.relative_to(archive_dir)), len(messages)),
    )
    return transcript


def set_dm_status(conn: sqlite3.Connection, ticket_id: int, delivered: bool) -> None:
    """Remember whether the transcript DM reached the opener (it may fail)."""
    conn.execute(
        "UPDATE ticket_archives SET dm_status=? WHERE ticket_id=?", ("sent" if delivered else "failed", ticket_id)
    )


def resolve_archive_path(archive_dir: Path, relative: str) -> Path | None:
    """Turn a stored relative path into a real file, refusing anything outside the archive."""
    root = archive_dir.resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate
