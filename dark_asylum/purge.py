"""Message-deletion jobs (30/60/90 days). Metadata only: bodies are never stored."""

from __future__ import annotations

import sqlite3

from .db import audit

ALLOWED_DAYS = (30, 60, 90)


class PurgeError(ValueError):
    """A rejected purge request with a user-safe message."""


def queue(conn: sqlite3.Connection, channel_id: int, days: int, actor_id: int) -> int:
    """Queue deletion of messages older than ``days`` in a channel; the bot executes it."""
    if days not in ALLOWED_DAYS:
        raise PurgeError("Days must be 30, 60 or 90.")
    if channel_id <= 0:
        raise PurgeError("A channel ID is required.")
    jid = conn.execute(
        "INSERT INTO purge_jobs(channel_id,days,requested_by) VALUES(?,?,?)", (channel_id, days, actor_id)
    ).lastrowid
    audit(conn, "portal", "purge", "queued", actor_id, str(channel_id), None, f"{days} days")
    return jid


def finish(conn: sqlite3.Connection, job_id: int, deleted: int | None, note: str) -> None:
    """Record the outcome (count or failure reason) and audit it."""
    status = "done" if deleted is not None else "failed"
    conn.execute("UPDATE purge_jobs SET status=?, deleted=?, note=? WHERE id=?", (status, deleted, note, job_id))
    job = conn.execute("SELECT * FROM purge_jobs WHERE id=?", (job_id,)).fetchone()
    audit(
        conn,
        "bot",
        "purge",
        status,
        job["requested_by"],
        str(job["channel_id"]),
        None,
        str(deleted),
        f"{job['days']} days; {note}",
    )
