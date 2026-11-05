"""Clan events: scheduling, recurrence, reminders (24h / 1h) and confirmed attendance."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ledger
from .db import Tx, audit, iso, utcnow

REMINDERS = (("24h", timedelta(hours=24)), ("1h", timedelta(hours=1)))


class EventError(ValueError):
    """A rejected event operation with a user-safe message."""


def parse_utc(raw: str) -> datetime:
    """Parse ``YYYY-MM-DD HH:MM`` (or ISO) as UTC; portal times are always UTC."""
    raw = raw.strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=utcnow().tzinfo)
        except ValueError:
            continue
    raise EventError("Use the format YYYY-MM-DD HH:MM (UTC).")


def create_event(
    conn: sqlite3.Connection,
    title: str,
    starts_at: str,
    recurrence_days: int,
    remind_24h: bool,
    remind_1h: bool,
    actor_id: int,
    description: str = "",
) -> int:
    """Create a one-time (recurrence 0) or repeating event; returns its id."""
    title = title.strip()
    if not title or len(title) > 100:
        raise EventError("A title (max 100 characters) is required.")
    if not 0 <= recurrence_days <= 365:
        raise EventError("Repeat interval must be 0-365 days (0 = one time).")
    start = parse_utc(starts_at)
    with Tx(conn):
        eid = conn.execute(
            "INSERT INTO events(title,description,starts_at,recurrence_days,remind_24h,remind_1h,created_by) "
            "VALUES(?,?,?,?,?,?,?)",
            (title, description.strip()[:500], iso(start), recurrence_days, int(remind_24h), int(remind_1h), actor_id),
        ).lastrowid
        audit(conn, "portal", "events", "created", actor_id, str(eid), None, iso(start), title)
    return eid


def cancel_event(conn: sqlite3.Connection, event_id: int, actor_id: int) -> None:
    """Cancel an event (and all future repeats); history is kept."""
    with Tx(conn):
        cur = conn.execute("UPDATE events SET status='cancelled' WHERE id=? AND status='scheduled'", (event_id,))
        if cur.rowcount == 0:
            raise EventError("That event is not scheduled.")
        audit(conn, "portal", "events", "cancelled", actor_id, str(event_id))


def next_occurrence(event: sqlite3.Row, now: datetime) -> datetime | None:
    """The upcoming start time, or None for a finished one-time event."""
    start = datetime.strptime(event["starts_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=now.tzinfo)
    if start >= now:
        return start
    if event["recurrence_days"] <= 0:
        return None
    step = timedelta(days=event["recurrence_days"])
    skipped = (now - start) // step + 1  # whole intervals already elapsed, plus one
    return start + skipped * step


def due_reminders(conn: sqlite3.Connection, now: datetime | None = None) -> list[tuple[sqlite3.Row, str, str]]:
    """Reminders that should be sent now as ``(event, occurrence_iso, kind)``.

    The 24h reminder is skipped once the 1h window has begun, so a late-created
    event does not send both at once. Sent reminders are recorded separately.
    """
    now = now or utcnow()
    due = []
    for ev in conn.execute("SELECT * FROM events WHERE status='scheduled'").fetchall():
        occ = next_occurrence(ev, now)
        if occ is None:
            continue
        occ_iso = iso(occ)
        for kind, lead in REMINDERS:
            if not ev["remind_" + kind]:
                continue
            opens = occ - lead
            closes = occ - timedelta(hours=1) if kind == "24h" else occ
            if (
                opens <= now < closes
                and not conn.execute(
                    "SELECT 1 FROM event_reminders WHERE event_id=? AND occurrence=? AND kind=?",
                    (ev["id"], occ_iso, kind),
                ).fetchone()
            ):
                due.append((ev, occ_iso, kind))
    return due


def mark_reminder_sent(conn: sqlite3.Connection, event_id: int, occurrence: str, kind: str) -> None:
    """Remember a sent reminder so restarts never repeat it."""
    conn.execute(
        "INSERT OR IGNORE INTO event_reminders(event_id,occurrence,kind) VALUES(?,?,?)", (event_id, occurrence, kind)
    )


def local_time(occurrence_iso: str, timezone: str | None) -> str:
    """Format a UTC time for a member's ``!timezone`` (falls back to UTC)."""
    dt = datetime.strptime(occurrence_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=utcnow().tzinfo)
    try:
        zone = ZoneInfo(timezone) if timezone else None
    except ZoneInfoNotFoundError:
        zone = None
    return (dt.astimezone(zone) if zone else dt).strftime("%a %d %b %H:%M %Z")


def reminder_text(conn: sqlite3.Connection, title: str, occurrence_iso: str, kind: str) -> str:
    """Reminder message listing the start time in every distinct timezone members have set (plus UTC)."""
    zones = sorted(
        {
            r["timezone"]
            for r in conn.execute("SELECT timezone FROM members WHERE status='active' AND timezone IS NOT NULL")
        }
    )
    lines = [f"Reminder ({kind}): **{title}** starts {local_time(occurrence_iso, None)}"]
    lines += [f"{zone}: {local_time(occurrence_iso, zone)}" for zone in zones[:10]]
    return "\n".join(lines)


def confirm_attendance(conn: sqlite3.Connection, event_id: int, occurrence: str, discord_id: int, actor_id: int) -> int:
    """An admin confirms attendance; the attendance preset is awarded exactly once. Returns points paid."""
    if conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is None:
        raise EventError("Unknown event.")
    try:
        conn.execute(
            "INSERT INTO event_attendance(event_id,occurrence,discord_id,confirmed_by) VALUES(?,?,?,?)",
            (event_id, occurrence, discord_id, actor_id),
        )
    except sqlite3.IntegrityError as exc:
        raise EventError("Attendance already confirmed for that member.") from exc
    row = conn.execute("SELECT amount FROM point_awards WHERE name='Clan event attendance'").fetchone()
    amount = row["amount"] if row else 0
    if amount > 0:
        ledger.award_points(conn, discord_id, amount, f"Event #{event_id} attendance", actor_id)
    return amount
