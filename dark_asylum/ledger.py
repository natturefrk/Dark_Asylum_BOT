"""GP bank and points ledgers.

All balance changes go through these functions so the invariants hold:
the bank never goes negative, every change has an actor and reason, and the
ledgers are append-only.
"""

from __future__ import annotations

import sqlite3

from .db import Tx, audit, get_setting
from .members import MemberError, current_period, get_member

MAX_GP_REWARD = 150_000_000  # per-redemption ceiling from the clan rules


class LedgerError(ValueError):
    """A rejected ledger operation with a user-safe message."""


def bank_balance(conn: sqlite3.Connection) -> int:
    """Current GP in the clan bank (balance after the newest ledger row)."""
    row = conn.execute("SELECT balance_after FROM bank_ledger ORDER BY id DESC LIMIT 1").fetchone()
    return row["balance_after"] if row else 0


def bank_entry_in_tx(conn: sqlite3.Connection, amount_gp: int, reason: str, actor_id: int | None) -> int:
    """Append a bank ledger row inside the caller's ``Tx``; returns the new balance.

    Shared by ``record_bank`` and by flows (goals, redemptions) that must change
    their own state and the bank atomically. The balance can never go negative.
    """
    reason = reason.strip()
    if amount_gp == 0:
        raise LedgerError("Amount cannot be zero.")
    if not reason:
        raise LedgerError("A reason is required.")
    new_balance = bank_balance(conn) + amount_gp
    if new_balance < 0:
        raise LedgerError("That withdrawal would make the bank balance negative.")
    conn.execute(
        "INSERT INTO bank_ledger(amount_gp,balance_after,reason,actor_id) VALUES(?,?,?,?)",
        (amount_gp, new_balance, reason, actor_id),
    )
    audit(conn, "portal", "bank", "transaction", actor_id, None, None, str(new_balance), f"{amount_gp:+,} GP: {reason}")
    return new_balance


def record_bank(conn: sqlite3.Connection, amount_gp: int, reason: str, actor_id: int) -> int:
    """Record a deposit (positive) or withdrawal (negative); returns the new balance."""
    with Tx(conn):
        return bank_entry_in_tx(conn, amount_gp, reason, actor_id)


def award_points(
    conn: sqlite3.Connection,
    discord_id: int,
    amount: int,
    reason: str,
    actor_id: int | None,
    source: str = "portal",
) -> tuple[int, int]:
    """Award points, filling ranking requirements first unless the member opted out.

    Returns ``(ranking_part, spending_part)``. Manual awards need a reason.
    """
    reason = reason.strip()
    if amount <= 0:
        raise LedgerError("Award amount must be positive.")
    if not reason:
        raise LedgerError("A reason is required for point awards.")
    with Tx(conn):
        member = get_member(conn, discord_id)
        if member is None or member["status"] != "active":
            raise MemberError("Points can only be awarded to active members.")
        target = int(get_setting(conn, "ranking_target_points", "0") or 0)
        room = 0 if member["ranking_opt_out"] else max(0, target - member["ranking_points"])
        ranking_part = min(amount, room)
        spending_part = amount - ranking_part
        conn.execute(
            "UPDATE members SET ranking_points=ranking_points+?, spending_points=spending_points+? WHERE discord_id=?",
            (ranking_part, spending_part, discord_id),
        )
        conn.execute(
            "INSERT INTO points_ledger(discord_id,period_id,ranking_delta,spending_delta,kind,reason,actor_id) "
            "VALUES(?,?,?,?,'award',?,?)",
            (discord_id, current_period(conn, discord_id), ranking_part, spending_part, reason, actor_id),
        )
        audit(
            conn,
            source,
            "points",
            "award",
            actor_id,
            str(discord_id),
            None,
            f"ranking+{ranking_part} spending+{spending_part}",
            reason,
        )
    return ranking_part, spending_part


def points_history(conn: sqlite3.Connection, discord_id: int, limit: int = 10) -> list[sqlite3.Row]:
    """Most recent ledger rows for one member."""
    return conn.execute(
        "SELECT * FROM points_ledger WHERE discord_id=? ORDER BY id DESC LIMIT ?",
        (discord_id, limit),
    ).fetchall()


def leaderboard(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """Top earners by lifetime-in-period points (public view: RSN + total only)."""
    return conn.execute(
        "SELECT rsn, ranking_points + spending_points AS total FROM members "
        "WHERE status='active' AND rsn IS NOT NULL ORDER BY total DESC LIMIT ?",
        (limit,),
    ).fetchall()


def spend_points_in_tx(
    conn: sqlite3.Connection, discord_id: int, amount: int, reason: str, kind: str = "spend"
) -> None:
    """Deduct spending points (or refund when ``amount`` is negative). Caller must hold a ``Tx``.

    Only the *spending* balance can be used; ranking points are never spent.
    Used so giveaway entries can spend and record the entry atomically.
    """
    member = get_member(conn, discord_id)
    if member is None or member["status"] != "active":
        raise MemberError("Only active members can spend points.")
    if amount > 0 and member["spending_points"] < amount:
        raise LedgerError(f"Not enough spending points (you have {member['spending_points']:,}).")
    conn.execute("UPDATE members SET spending_points=spending_points-? WHERE discord_id=?", (amount, discord_id))
    conn.execute(
        "INSERT INTO points_ledger(discord_id,period_id,ranking_delta,spending_delta,kind,reason,actor_id) "
        "VALUES(?,?,0,?,?,?,NULL)",
        (discord_id, current_period(conn, discord_id), -amount, kind, reason),
    )
