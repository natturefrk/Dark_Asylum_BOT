"""Member registry: RSN linking, join dates, leaving and rejoining.

Discord IDs are the identity key. Members are never deleted; "removal" marks
them ``left`` so strikes, points and bank history stay intact.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime

from .db import Tx, audit, now_iso

# RuneScape names: 1-12 chars of letters, digits, space, hyphen or underscore.
RSN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,11}$")


class MemberError(ValueError):
    """A user-facing validation problem (safe to show in Discord/portal)."""


def clean_rsn(raw: str) -> str:
    """Validate and normalise an RSN (underscores/hyphens kept, whitespace trimmed)."""
    rsn = " ".join(raw.strip().split())
    if not RSN_PATTERN.match(rsn):
        raise MemberError("Invalid RSN: use 1-12 letters, numbers, spaces, hyphens or underscores.")
    return rsn


def parse_join_date(raw: str) -> str:
    """Convert a user supplied MM-DD-YYYY string to ISO, rejecting future dates."""
    try:
        parsed = datetime.strptime(raw.strip(), "%m-%d-%Y").date()
    except ValueError as exc:
        raise MemberError("Dates must look like MM-DD-YYYY, e.g. 03-27-2025.") from exc
    if parsed > date.today():
        raise MemberError("The join date cannot be in the future.")
    return parsed.isoformat()


def get_member(conn: sqlite3.Connection, discord_id: int) -> sqlite3.Row | None:
    """Fetch a member row by Discord ID."""
    return conn.execute("SELECT * FROM members WHERE discord_id=?", (discord_id,)).fetchone()


def find_by_rsn(conn: sqlite3.Connection, rsn: str) -> sqlite3.Row | None:
    """Find the ACTIVE member currently holding an RSN (case-insensitive)."""
    return conn.execute("SELECT * FROM members WHERE rsn=? AND status='active'", (rsn,)).fetchone()


def current_period(conn: sqlite3.Connection, discord_id: int) -> int | None:
    """Id of the member's open membership period, if any."""
    row = conn.execute(
        "SELECT id FROM membership_periods WHERE discord_id=? AND ended_at IS NULL", (discord_id,)
    ).fetchone()
    return row["id"] if row else None


def _ensure_active(conn: sqlite3.Connection, discord_id: int) -> None:
    """Create the member, or reopen a ``left`` member with a fresh period (zero strikes)."""
    member = get_member(conn, discord_id)
    if member is None:
        conn.execute("INSERT INTO members(discord_id) VALUES(?)", (discord_id,))
    elif member["status"] == "left":
        # Rejoin: new period; balances were forfeited when they left.
        conn.execute("UPDATE members SET status='active', rsn=NULL WHERE discord_id=?", (discord_id,))
    if current_period(conn, discord_id) is None:
        conn.execute("INSERT INTO membership_periods(discord_id) VALUES(?)", (discord_id,))


def link_rsn(
    conn: sqlite3.Connection,
    discord_id: int,
    rsn: str,
    actor_id: int,
    join_date: str | None = None,
) -> None:
    """Link (or replace) a member's RSN.

    Raises ``MemberError`` if another active member already owns the RSN.
    The previous RSN is replaced, not kept as an alias.
    """
    rsn = clean_rsn(rsn)
    with Tx(conn):
        owner = find_by_rsn(conn, rsn)
        if owner is not None and owner["discord_id"] != discord_id:
            raise MemberError(f"'{rsn}' is already linked to another member.")
        _ensure_active(conn, discord_id)
        before = get_member(conn, discord_id)["rsn"]
        conn.execute("UPDATE members SET rsn=? WHERE discord_id=?", (rsn, discord_id))
        if join_date:
            conn.execute("UPDATE members SET join_date=? WHERE discord_id=?", (join_date, discord_id))
        audit(
            conn,
            "bot",
            "members",
            "rsn_linked",
            actor_id,
            str(discord_id),
            before,
            rsn,
            f"join_date={join_date}" if join_date else None,
        )


def set_join_date(conn: sqlite3.Connection, discord_id: int, iso_date: str, actor_id: int) -> None:
    """Set a member's join date (history preserved through the audit log)."""
    with Tx(conn):
        _ensure_active(conn, discord_id)
        before = get_member(conn, discord_id)["join_date"]
        conn.execute("UPDATE members SET join_date=? WHERE discord_id=?", (iso_date, discord_id))
        audit(conn, "bot", "members", "join_date_set", actor_id, str(discord_id), before, iso_date)


def remove_member(conn: sqlite3.Connection, discord_id: int, actor_id: int, reason: str, source: str) -> None:
    """Mark a member as left: forfeit points, close the period, keep all history."""
    with Tx(conn):
        member = get_member(conn, discord_id)
        if member is None or member["status"] != "active":
            raise MemberError("That person is not an active member.")
        period = current_period(conn, discord_id)
        rank, spend = member["ranking_points"], member["spending_points"]
        if rank or spend:
            # Ledger row keeps the forfeited amounts visible forever.
            conn.execute(
                "INSERT INTO points_ledger(discord_id,period_id,ranking_delta,spending_delta,kind,reason,actor_id) "
                "VALUES(?,?,?,?,'forfeit',?,?)",
                (discord_id, period, -rank, -spend, f"Left clan: {reason}", actor_id),
            )
        conn.execute(
            "UPDATE members SET status='left', ranking_points=0, spending_points=0 WHERE discord_id=?",
            (discord_id,),
        )
        conn.execute(
            "UPDATE membership_periods SET ended_at=?, end_reason=? WHERE id=?",
            (now_iso(), reason, period),
        )
        audit(
            conn,
            source,
            "members",
            "member_removed",
            actor_id,
            str(discord_id),
            None,
            None,
            f"{reason}; forfeited ranking={rank} spending={spend}",
        )


def search(conn: sqlite3.Connection, term: str, limit: int = 15) -> list[sqlite3.Row]:
    """Autocomplete helper: match by RSN fragment or exact/partial Discord ID."""
    like = f"%{term.strip().replace('%', '').replace('_', '')}%"
    return conn.execute(
        "SELECT discord_id, rsn, status FROM members "
        "WHERE rsn LIKE ? OR CAST(discord_id AS TEXT) LIKE ? ORDER BY rsn LIMIT ?",
        (like, like, limit),
    ).fetchall()
