"""Bossing Gear goals: catalog, discounts, goal lifecycle, GP contributions, refunds, fulfilment.

Money flow: contributions are deposited into the clan bank; on fulfilment the
bank pays out the item's FULL catalog value (the discount is clan-funded); on
cancellation every contributed GP is paid back out of the bank. All of it goes
through the bank ledger, which never allows a negative balance.
"""

from __future__ import annotations

import sqlite3

from . import ledger
from .db import Tx, audit, get_setting
from .members import MemberError, get_member

MAX_DISCOUNT = 50  # percent, hard ceiling


class GoalError(ValueError):
    """A rejected goal operation with a user-safe message."""


def save_item(conn: sqlite3.Connection, name: str, price_gp: int, source: str, confirmed: bool, actor_id: int) -> None:
    """Add or update a catalog item. A price is only trusted once an admin confirms it."""
    name = name.strip()
    if not name or len(name) > 80 or price_gp <= 0:
        raise GoalError("A name (max 80) and a positive price are required.")
    if source not in ("manual", "ge_reference"):
        raise GoalError("Price source must be 'manual' or 'ge_reference'.")
    with Tx(conn):
        old = conn.execute("SELECT price_gp FROM bossing_items WHERE name=?", (name,)).fetchone()
        conn.execute(
            "INSERT INTO bossing_items(name,price_gp,price_source,confirmed) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET price_gp=excluded.price_gp, price_source=excluded.price_source, "
            "confirmed=excluded.confirmed",
            (name, price_gp, source, int(confirmed)),
        )
        audit(
            conn,
            "portal",
            "bossing",
            "item_saved",
            actor_id,
            name,
            str(old["price_gp"]) if old else None,
            str(price_gp),
            source,
        )


def set_member_discount(conn: sqlite3.Connection, discord_id: int, percent: int | None, actor_id: int) -> None:
    """Set a member's total discount (replaces the default; max 50%); None removes the override."""
    if percent is not None and not 0 <= percent <= MAX_DISCOUNT:
        raise GoalError(f"Discount must be between 0 and {MAX_DISCOUNT}%.")
    with Tx(conn):
        old = conn.execute("SELECT percent FROM member_discounts WHERE discord_id=?", (discord_id,)).fetchone()
        if percent is None:
            conn.execute("DELETE FROM member_discounts WHERE discord_id=?", (discord_id,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO member_discounts(discord_id,percent) VALUES(?,?)", (discord_id, percent)
            )
        audit(
            conn,
            "portal",
            "bossing",
            "discount",
            actor_id,
            str(discord_id),
            str(old["percent"]) if old else None,
            str(percent),
        )


def discount_for(conn: sqlite3.Connection, discord_id: int) -> int:
    """The member's override if set, otherwise the default (10%); always capped at 50%."""
    row = conn.execute("SELECT percent FROM member_discounts WHERE discord_id=?", (discord_id,)).fetchone()
    pct = row["percent"] if row else int(get_setting(conn, "gear_default_discount", "10") or 10)
    return min(pct, MAX_DISCOUNT)


def start_goal(conn: sqlite3.Connection, discord_id: int, item_id: int, actor_id: int) -> int:
    """Begin a goal for a member; the discounted price is locked now. One open goal per member."""
    member = get_member(conn, discord_id)
    if member is None or member["status"] != "active":
        raise MemberError("Goals are for active clan members.")
    with Tx(conn):
        item = conn.execute("SELECT * FROM bossing_items WHERE id=? AND enabled=1", (item_id,)).fetchone()
        if item is None:
            raise GoalError("Unknown catalog item.")
        if not item["confirmed"]:
            raise GoalError("That item's price has not been confirmed by an admin yet.")
        pct = discount_for(conn, discord_id)
        target = item["price_gp"] * (100 - pct) // 100
        try:
            gid = conn.execute(
                "INSERT INTO bossing_goals(discord_id,item_id,item_name,catalog_price,discount_pct,target_gp) "
                "VALUES(?,?,?,?,?,?)",
                (discord_id, item_id, item["name"], item["price_gp"], pct, target),
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            raise GoalError("That member already has an open goal.") from exc
        audit(
            conn,
            "portal",
            "bossing",
            "goal_started",
            actor_id,
            str(gid),
            None,
            f"{item['name']} @ {target:,} GP ({pct}% off)",
        )
    return gid


def contribute(conn: sqlite3.Connection, goal_id: int, amount_gp: int, actor_id: int) -> int:
    """Record a GP contribution (also a bank deposit); returns the GP still needed."""
    if amount_gp <= 0:
        raise GoalError("Contribution must be positive.")
    with Tx(conn):
        g = conn.execute("SELECT * FROM bossing_goals WHERE id=?", (goal_id,)).fetchone()
        if g is None or g["status"] != "active":
            raise GoalError("That goal is not collecting contributions.")
        remaining = g["target_gp"] - g["contributed_gp"]
        if amount_gp > remaining:
            raise GoalError(f"That is more than the remaining {remaining:,} GP.")
        ledger.bank_entry_in_tx(conn, amount_gp, f"Goal #{goal_id} contribution ({g['item_name']})", actor_id)
        conn.execute(
            "INSERT INTO goal_contributions(goal_id,amount_gp,actor_id) VALUES(?,?,?)", (goal_id, amount_gp, actor_id)
        )
        done = amount_gp == remaining
        conn.execute(
            "UPDATE bossing_goals SET contributed_gp=contributed_gp+?, status=? WHERE id=?",
            (amount_gp, "ready" if done else "active", goal_id),
        )
        return remaining - amount_gp


def fulfill(conn: sqlite3.Connection, goal_id: int, actor_id: int) -> None:
    """Admin confirms delivery: bank pays the FULL catalog value and the goal closes."""
    with Tx(conn):
        g = conn.execute("SELECT * FROM bossing_goals WHERE id=?", (goal_id,)).fetchone()
        if g is None or g["status"] != "ready":
            raise GoalError("Only a fully funded (ready) goal can be fulfilled.")
        ledger.bank_entry_in_tx(
            conn, -g["catalog_price"], f"Goal #{goal_id} item delivered: {g['item_name']}", actor_id
        )
        conn.execute("UPDATE bossing_goals SET status='fulfilled' WHERE id=?", (goal_id,))
        audit(conn, "portal", "bossing", "goal_fulfilled", actor_id, str(goal_id), "ready", "fulfilled")


def cancel(conn: sqlite3.Connection, goal_id: int, actor_id: int) -> int:
    """Cancel an open goal; contributed GP is refunded as a bank payout. Returns the refund."""
    with Tx(conn):
        g = conn.execute("SELECT * FROM bossing_goals WHERE id=?", (goal_id,)).fetchone()
        if g is None or g["status"] not in ("active", "ready"):
            raise GoalError("Only open goals can be cancelled.")
        if g["contributed_gp"]:
            ledger.bank_entry_in_tx(
                conn, -g["contributed_gp"], f"Goal #{goal_id} cancelled: refund to {g['discord_id']}", actor_id
            )
        conn.execute("UPDATE bossing_goals SET status='cancelled' WHERE id=?", (goal_id,))
        audit(
            conn,
            "portal",
            "bossing",
            "goal_cancelled",
            actor_id,
            str(goal_id),
            g["status"],
            "cancelled",
            f"{g['contributed_gp']:,} GP refunded",
        )
        return g["contributed_gp"]


def open_goal(conn: sqlite3.Connection, discord_id: int) -> sqlite3.Row | None:
    """A member's active or ready goal, if any (used by the bot's progress command)."""
    return conn.execute(
        "SELECT * FROM bossing_goals WHERE discord_id=? AND status IN ('active','ready')", (discord_id,)
    ).fetchone()
