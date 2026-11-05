"""Giveaways: point-priced entries, random draw, refunds on cancel, bank payout tracking."""

from __future__ import annotations

import random
import sqlite3

from . import ledger
from .db import Tx, audit, get_setting


class GiveawayError(ValueError):
    """A rejected giveaway operation with a user-safe message."""


def create_giveaway(
    conn: sqlite3.Connection, title: str, prize_gp: int, entry_cost: int, channel_id: str, actor_id: int
) -> int:
    """Publish a giveaway only if the bank can currently fund the prize."""
    title = title.strip()
    if not title or len(title) > 100:
        raise GiveawayError("A title (max 100 characters) is required.")
    if not 0 < prize_gp <= ledger.MAX_GP_REWARD:
        raise GiveawayError(f"Prize must be between 1 and {ledger.MAX_GP_REWARD:,} GP.")
    if entry_cost < 0:
        raise GiveawayError("Entry cost cannot be negative.")
    if ledger.bank_balance(conn) < prize_gp:
        raise GiveawayError("The bank cannot fund that prize right now.")
    with Tx(conn):
        gid = conn.execute(
            "INSERT INTO giveaways(title,prize_gp,entry_cost,channel_id,created_by) VALUES(?,?,?,?,?)",
            (title, prize_gp, entry_cost, channel_id, actor_id),
        ).lastrowid
        audit(conn, "portal", "giveaways", "created", actor_id, str(gid), None, f"{prize_gp:,} GP", title)
    return gid


def enter(conn: sqlite3.Connection, giveaway_id: int, discord_id: int) -> str:
    """Add one entry: the first is free, later ones cost points (deducted at once, no withdrawal).

    Returns a short description of what happened. The cap includes the free entry.
    """
    max_entries = int(get_setting(conn, "giveaway_max_entries", "5") or 5)
    with Tx(conn):
        g = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
        if g is None or g["status"] != "open":
            raise GiveawayError("This giveaway is not open.")
        have = conn.execute(
            "SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=? AND discord_id=?", (giveaway_id, discord_id)
        ).fetchone()[0]
        if have >= max_entries:
            raise GiveawayError(f"You already have the maximum of {max_entries} entries.")
        paid = have > 0  # the first entry is always free
        cost = g["entry_cost"] if paid else 0
        if cost:
            ledger.spend_points_in_tx(conn, discord_id, cost, f"Giveaway #{giveaway_id} entry")
        elif not paid:
            # Free entry still requires active membership.
            ledger.spend_points_in_tx(conn, discord_id, 0, f"Giveaway #{giveaway_id} free entry", kind="entry")
        conn.execute(
            "INSERT INTO giveaway_entries(giveaway_id,discord_id,paid,points_spent) VALUES(?,?,?,?)",
            (giveaway_id, discord_id, int(paid), cost),
        )
    return f"Entry {have + 1}/{max_entries} " + (f"(-{cost:,} points)" if cost else "(free)")


def cancel(conn: sqlite3.Connection, giveaway_id: int, actor_id: int) -> int:
    """Cancel an open giveaway and refund every point spent; returns the refunded total."""
    with Tx(conn):
        g = conn.execute("SELECT status FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
        if g is None or g["status"] != "open":
            raise GiveawayError("Only open giveaways can be cancelled.")
        total = 0
        spent = conn.execute(
            "SELECT discord_id, SUM(points_spent) s FROM giveaway_entries WHERE giveaway_id=? GROUP BY discord_id",
            (giveaway_id,),
        ).fetchall()
        for row in spent:
            if row["s"]:
                # A member who left in the meantime keeps their (forfeited) history untouched.
                try:
                    ledger.spend_points_in_tx(
                        conn, row["discord_id"], -row["s"], f"Giveaway #{giveaway_id} cancelled: refund", "refund"
                    )
                    total += row["s"]
                except ledger.MemberError:
                    pass
        conn.execute("UPDATE giveaways SET status='cancelled' WHERE id=?", (giveaway_id,))
        audit(
            conn,
            "portal",
            "giveaways",
            "cancelled",
            actor_id,
            str(giveaway_id),
            "open",
            "cancelled",
            f"{total:,} refunded",
        )
    return total


def draw(conn: sqlite3.Connection, giveaway_id: int, actor_id: int, rng: random.Random | None = None) -> int:
    """Pick the winner at random (each entry is one ticket); returns the winner's Discord ID."""
    rng = rng or random.SystemRandom()
    with Tx(conn):
        g = conn.execute("SELECT status FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
        if g is None or g["status"] != "open":
            raise GiveawayError("Only open giveaways can be drawn.")
        tickets = [
            r["discord_id"]
            for r in conn.execute(
                "SELECT discord_id FROM giveaway_entries WHERE giveaway_id=? ORDER BY id", (giveaway_id,)
            )
        ]
        if not tickets:
            raise GiveawayError("Nobody has entered yet.")
        winner = rng.choice(tickets)
        conn.execute("UPDATE giveaways SET status='drawn', winner_id=? WHERE id=?", (winner, giveaway_id))
        audit(conn, "portal", "giveaways", "drawn", actor_id, str(giveaway_id), "open", "drawn", f"winner {winner}")
    return winner


def record_payout(conn: sqlite3.Connection, giveaway_id: int, actor_id: int) -> None:
    """Withdraw the prize from the bank (never below zero) and mark the giveaway paid."""
    g = conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()
    if g is None or g["status"] != "drawn" or g["payout_paid"]:
        raise GiveawayError("Only a drawn, unpaid giveaway can be paid out.")
    ledger.record_bank(conn, -g["prize_gp"], f"Giveaway #{giveaway_id} prize to {g['winner_id']}", actor_id)
    conn.execute("UPDATE giveaways SET payout_paid=1 WHERE id=?", (giveaway_id,))
