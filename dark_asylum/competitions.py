"""Competitions (individuals only): lifecycle, entries, proof approval and placement awards."""

from __future__ import annotations

import sqlite3

from . import ledger
from .db import Tx, audit, now_iso
from .members import get_member

# Allowed state changes; anything else is rejected.
TRANSITIONS = {"draft": {"open", "cancelled"}, "open": {"review", "cancelled"}, "review": {"finished", "open"}}
# Preset award names (managed on the points page) for placements 1-3.
PLACEMENT_PRESETS = {1: "Placement bonus: 1st", 2: "Placement bonus: 2nd", 3: "Placement bonus: 3rd"}


class CompetitionError(ValueError):
    """A rejected competition operation with a user-safe message."""


def create(conn: sqlite3.Connection, title: str, kind: str, skill: str, rewarded: bool, actor_id: int) -> int:
    """Create a draft competition. XP contests need exactly one skill."""
    title = title.strip()
    if not title or len(title) > 100:
        raise CompetitionError("A title (max 100 characters) is required.")
    if kind not in ("xp", "proof"):
        raise CompetitionError("Kind must be 'xp' or 'proof'.")
    if kind == "xp" and not skill.strip():
        raise CompetitionError("XP competitions need a skill.")
    with Tx(conn):
        cid = conn.execute(
            "INSERT INTO competitions(title,kind,skill,rewarded,created_by) VALUES(?,?,?,?,?)",
            (title, kind, skill.strip() or None, int(rewarded), actor_id),
        ).lastrowid
        audit(conn, "portal", "competitions", "created", actor_id, str(cid), None, title, kind)
    return cid


def set_status(conn: sqlite3.Connection, cid: int, new: str, actor_id: int) -> None:
    """Move through the lifecycle, rejecting illegal jumps."""
    with Tx(conn):
        row = conn.execute("SELECT status FROM competitions WHERE id=?", (cid,)).fetchone()
        if row is None or new not in TRANSITIONS.get(row["status"], set()):
            raise CompetitionError("That status change is not allowed.")
        conn.execute("UPDATE competitions SET status=? WHERE id=?", (new, cid))
        if new == "open":
            # Reopened contests are re-snapshotted at review; the start time is kept from the first opening.
            conn.execute(
                "UPDATE competitions SET final_snapshot_at=NULL, started_at=COALESCE(started_at, ?) WHERE id=?",
                (now_iso(), cid),
            )
        audit(conn, "portal", "competitions", "status", actor_id, str(cid), row["status"], new)


def join(conn: sqlite3.Connection, cid: int, discord_id: int, label: str) -> None:
    """Enter while open. Prize contests are members-only; fun contests allow guests."""
    comp = conn.execute("SELECT * FROM competitions WHERE id=?", (cid,)).fetchone()
    if comp is None or comp["status"] != "open":
        raise CompetitionError("This competition is not open for entries.")
    member = get_member(conn, discord_id)
    is_member = bool(member and member["status"] == "active")
    if comp["rewarded"] and not is_member:
        raise CompetitionError("Prize competitions are for clan members only.")
    try:
        conn.execute(
            "INSERT INTO competition_entries(competition_id,discord_id,label,is_member) VALUES(?,?,?,?)",
            (cid, discord_id, label[:50], int(is_member)),
        )
    except sqlite3.IntegrityError as exc:
        raise CompetitionError("You have already entered.") from exc


def submit_proof(conn: sqlite3.Connection, cid: int, discord_id: int, proof: str) -> None:
    """Attach proof (link or description) to an entry; it waits for admin approval."""
    proof = proof.strip()
    if not proof or len(proof) > 500:
        raise CompetitionError("Proof (max 500 characters) is required.")
    cur = conn.execute(
        "UPDATE competition_entries SET proof=?, proof_status='pending' WHERE competition_id=? AND discord_id=? "
        "AND EXISTS (SELECT 1 FROM competitions WHERE id=? AND kind='proof' AND status='open')",
        (proof, cid, discord_id, cid),
    )
    if cur.rowcount == 0:
        raise CompetitionError("Join an open proof competition first.")


def review_proof(conn: sqlite3.Connection, entry_id: int, approve: bool, actor_id: int) -> None:
    """Admin approves or rejects a pending proof."""
    with Tx(conn):
        row = conn.execute("SELECT proof_status FROM competition_entries WHERE id=?", (entry_id,)).fetchone()
        if row is None or row["proof_status"] != "pending":
            raise CompetitionError("No pending proof for that entry.")
        new = "approved" if approve else "rejected"
        conn.execute("UPDATE competition_entries SET proof_status=? WHERE id=?", (new, entry_id))
        audit(conn, "portal", "competitions", "proof_" + new, actor_id, str(entry_id))


def finish(conn: sqlite3.Connection, cid: int, placements: dict[int, int], actor_id: int) -> list[str]:
    """Finalise after admin review: record placements (1-3), pay awards once, close the contest.

    ``placements`` maps placement number -> entry id. Participation points go to
    every *member* entry (proof contests: approved proofs only). Guests never get points.
    Ties are resolved by the admin choosing who takes each place.
    """
    comp = conn.execute("SELECT * FROM competitions WHERE id=?", (cid,)).fetchone()
    if comp is None or comp["status"] != "review":
        raise CompetitionError("Move the competition to review before finishing it.")
    if len(set(placements.values())) != len(placements):
        raise CompetitionError("One entry cannot hold two placements.")
    entries = {r["id"]: r for r in conn.execute("SELECT * FROM competition_entries WHERE competition_id=?", (cid,))}
    for place, entry_id in placements.items():
        if place not in PLACEMENT_PRESETS or entry_id not in entries:
            raise CompetitionError("Invalid placement.")
    awards: list[tuple[int, int, str]] = []

    def preset(name: str) -> int:
        """Configured point value for a named preset (0 when missing or disabled)."""
        row = conn.execute("SELECT amount FROM point_awards WHERE name=?", (name,)).fetchone()
        return row["amount"] if row else 0

    participation = preset("Competition participation")
    for e in entries.values():
        if not e["is_member"] or (comp["kind"] == "proof" and e["proof_status"] != "approved"):
            continue
        if participation:
            awards.append((e["discord_id"], participation, f"Competition #{cid} participation"))
    for place, entry_id in placements.items():
        e = entries[entry_id]
        if e["is_member"] and preset(PLACEMENT_PRESETS[place]):
            awards.append((e["discord_id"], preset(PLACEMENT_PRESETS[place]), f"Competition #{cid} placement {place}"))
    with Tx(conn):
        # Status flips first inside the lock so a repeat click cannot pay twice.
        conn.execute("UPDATE competitions SET status='finished' WHERE id=?", (cid,))
        for place, entry_id in placements.items():
            conn.execute("UPDATE competition_entries SET placement=? WHERE id=?", (place, entry_id))
        audit(conn, "portal", "competitions", "finished", actor_id, str(cid), "review", "finished")
    done = []
    for discord_id, amount, reason in awards:
        try:
            ledger.award_points(conn, discord_id, amount, reason, actor_id)
            done.append(f"+{amount} to {discord_id}: {reason}")
        except ledger.MemberError:
            continue  # left the clan before payout
    return done
