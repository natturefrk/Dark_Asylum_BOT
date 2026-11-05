"""XP and clan-XP milestones with once-only point awards.

Pure database logic: callers pass in XP numbers they obtained from a *verified*
provider. Nothing here fetches or invents XP data, so with no provider the
configuration works but no awards can trigger.
"""

from __future__ import annotations

import sqlite3

from . import ledger
from .db import Tx, audit
from .members import MemberError


class MilestoneError(ValueError):
    """A rejected milestone operation with a user-safe message."""


def add_milestone(conn: sqlite3.Connection, scope: str, skill: str, threshold: int, points: int, actor_id: int) -> int:
    """Configure a milestone. ``skill`` '*' means any skill; clan milestones ignore skill."""
    if scope not in ("skill", "clan"):
        raise MilestoneError("Scope must be 'skill' or 'clan'.")
    if threshold <= 0 or points <= 0:
        raise MilestoneError("Threshold and points must be positive.")
    skill = (skill.strip() or "*") if scope == "skill" else None
    try:
        with Tx(conn):
            mid = conn.execute(
                "INSERT INTO xp_milestones(scope,skill,threshold,points) VALUES(?,?,?,?)",
                (scope, skill, threshold, points),
            ).lastrowid
            audit(
                conn,
                "portal",
                "milestones",
                "added",
                actor_id,
                str(mid),
                None,
                f"{scope}:{skill}:{threshold}",
                f"{points} points",
            )
    except sqlite3.IntegrityError as exc:
        raise MilestoneError("That milestone already exists.") from exc
    return mid


def set_enabled(conn: sqlite3.Connection, milestone_id: int, enabled: bool, actor_id: int) -> None:
    """Enable or disable a milestone (awards already paid are untouched)."""
    with Tx(conn):
        cur = conn.execute("UPDATE xp_milestones SET enabled=? WHERE id=?", (int(enabled), milestone_id))
        if cur.rowcount == 0:
            raise MilestoneError("Unknown milestone.")
        audit(conn, "portal", "milestones", "enabled" if enabled else "disabled", actor_id, str(milestone_id))


def evaluate_member(
    conn: sqlite3.Connection, discord_id: int, skills: dict[str, int], actor_id: int | None = None
) -> list[str]:
    """Pay every skill milestone this member has reached and not yet been paid for.

    ``skills`` maps skill name -> XP. Running this for an existing high-level
    member simply pays whatever thresholds they already exceed, once (the
    "reward existing progress at setup" rule). Returns descriptions of awards.
    """
    paid: list[str] = []
    for m in conn.execute("SELECT * FROM xp_milestones WHERE scope='skill' AND enabled=1").fetchall():
        reached = [s for s, xp in skills.items() if xp >= m["threshold"] and m["skill"] in ("*", s)]
        if not reached:
            continue
        # The primary key makes this insert the once-only guard; a repeat run changes nothing.
        cur = conn.execute(
            "INSERT OR IGNORE INTO xp_milestone_awards(milestone_id,discord_id) VALUES(?,?)", (m["id"], discord_id)
        )
        if cur.rowcount:
            try:
                ledger.award_points(
                    conn, discord_id, m["points"], f"XP milestone {m['threshold']:,} ({reached[0]})", actor_id, "bot"
                )
                paid.append(f"+{m['points']} for {m['threshold']:,} XP in {reached[0]}")
            except MemberError:
                conn.execute(
                    "DELETE FROM xp_milestone_awards WHERE milestone_id=? AND discord_id=?", (m["id"], discord_id)
                )
    return paid


def evaluate_clan(conn: sqlite3.Connection, total_xp: int, actor_id: int | None = None) -> list[str]:
    """Pay each reached clan milestone once, to every active member at that moment."""
    paid: list[str] = []
    for m in conn.execute("SELECT * FROM xp_milestones WHERE scope='clan' AND enabled=1").fetchall():
        if total_xp < m["threshold"]:
            continue
        # discord_id 0 is the "this clan milestone has been reached" marker.
        if not conn.execute(
            "INSERT OR IGNORE INTO xp_milestone_awards(milestone_id,discord_id) VALUES(?,0)", (m["id"],)
        ).rowcount:
            continue
        for member in conn.execute("SELECT discord_id FROM members WHERE status='active'").fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO xp_milestone_awards(milestone_id,discord_id) VALUES(?,?)",
                (m["id"], member["discord_id"]),
            )
            ledger.award_points(
                conn, member["discord_id"], m["points"], f"Clan XP milestone {m['threshold']:,}", actor_id, "bot"
            )
        paid.append(f"Clan reached {m['threshold']:,} XP: +{m['points']} points each")
    return paid


def progress(conn: sqlite3.Connection, discord_id: int) -> list[sqlite3.Row]:
    """Every enabled milestone with whether this member has already been paid for it."""
    return conn.execute(
        "SELECT m.*, (a.discord_id IS NOT NULL) AS reached FROM xp_milestones m "
        "LEFT JOIN xp_milestone_awards a ON a.milestone_id=m.id AND a.discord_id=? WHERE m.enabled=1 "
        "ORDER BY m.scope, m.threshold",
        (discord_id,),
    ).fetchall()
