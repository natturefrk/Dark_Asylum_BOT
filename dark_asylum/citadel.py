"""Citadel cycle tracking: visit/cap checklist, one-time point awards and cycle archive.

Rules: a visit is worth 50 points once per cycle; capping implies a visit and adds
the +100 cap bonus (150 total). Awards use the portal-editable preset values and
are guarded by flags so re-marking never pays twice.
"""

from __future__ import annotations

import sqlite3

from . import ledger
from .db import Tx, audit, now_iso
from .members import MemberError, get_member


def current_cycle(conn: sqlite3.Connection) -> int:
    """Id of the open cycle (one always exists; created by the migration)."""
    return conn.execute("SELECT id FROM citadel_cycles WHERE ended_at IS NULL ORDER BY id DESC").fetchone()["id"]


def _preset(conn: sqlite3.Connection, name: str) -> int:
    """Points configured for a named award preset (0 if an admin removed it)."""
    row = conn.execute("SELECT amount FROM point_awards WHERE name=?", (name,)).fetchone()
    return row["amount"] if row else 0


def mark(conn: sqlite3.Connection, discord_id: int, capped: bool, actor_id: int, source: str = "portal") -> list[str]:
    """Record a visit (or a cap, which implies a visit) and award any points not yet paid.

    Returns human-readable descriptions of the awards made.
    """
    member = get_member(conn, discord_id)
    if member is None or member["status"] != "active":
        raise MemberError("Citadel progress can only be recorded for active members.")
    owed: list[tuple[int, str]] = []
    with Tx(conn):
        cycle = current_cycle(conn)
        conn.execute("INSERT OR IGNORE INTO citadel_entries(cycle_id,discord_id) VALUES(?,?)", (cycle, discord_id))
        entry = conn.execute(
            "SELECT * FROM citadel_entries WHERE cycle_id=? AND discord_id=?", (cycle, discord_id)
        ).fetchone()
        # Flip the "awarded" flags inside the transaction so a repeat call can never pay twice.
        if not entry["visit_awarded"]:
            owed.append((_preset(conn, "Citadel visit"), f"Citadel visit (cycle {cycle})"))
        if capped and not entry["cap_awarded"]:
            owed.append((_preset(conn, "Citadel cap bonus"), f"Citadel cap bonus (cycle {cycle})"))
        conn.execute(
            "UPDATE citadel_entries SET visited=1, capped=capped OR ?, visit_awarded=1, "
            "cap_awarded=cap_awarded OR ?, updated_at=? WHERE cycle_id=? AND discord_id=?",
            (int(capped), int(capped), now_iso(), cycle, discord_id),
        )
        audit(
            conn,
            source,
            "citadel",
            "capped" if capped else "visited",
            actor_id,
            str(discord_id),
            None,
            None,
            f"cycle {cycle}",
        )
    done = []
    for amount, reason in owed:
        if amount > 0:
            ledger.award_points(conn, discord_id, amount, reason, actor_id, source)
            done.append(f"+{amount} {reason}")
    return done


def status(conn: sqlite3.Connection, discord_id: int) -> sqlite3.Row | None:
    """A member's row for the open cycle, if they have one."""
    return conn.execute(
        "SELECT * FROM citadel_entries WHERE cycle_id=? AND discord_id=?", (current_cycle(conn), discord_id)
    ).fetchone()


def reset_cycle(conn: sqlite3.Connection, actor_id: int) -> int:
    """Archive the open cycle with frozen totals and start a fresh one; returns the new id."""
    with Tx(conn):
        old = current_cycle(conn)
        totals = conn.execute(
            "SELECT COALESCE(SUM(visited),0) v, COALESCE(SUM(capped),0) c FROM citadel_entries WHERE cycle_id=?", (old,)
        ).fetchone()
        conn.execute(
            "UPDATE citadel_cycles SET ended_at=?, visitors=?, cappers=? WHERE id=?",
            (now_iso(), totals["v"], totals["c"], old),
        )
        new = conn.execute("INSERT INTO citadel_cycles DEFAULT VALUES").lastrowid
        audit(
            conn,
            "portal",
            "citadel",
            "cycle_reset",
            actor_id,
            str(old),
            None,
            str(new),
            f"{totals['v']} visited, {totals['c']} capped",
        )
        return new
