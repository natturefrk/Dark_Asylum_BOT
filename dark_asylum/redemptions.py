"""Monthly GP/item redemptions: admin-scheduled windows, member requests, approval and payment.

Rules enforced here: at most 150m per redemption; one *approved* redemption per
member per UTC month (the slot is taken on approval); points are deducted on
approval; payment withdraws from the bank, which can never go negative.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date

from . import ledger
from .db import Tx, audit, get_setting
from .members import MemberError, get_member


class RedemptionError(ValueError):
    """A rejected redemption operation with a user-safe message."""


def add_window(conn: sqlite3.Connection, day: str, kind: str, actor_id: int) -> int:
    """Schedule one single-day window; each month allows one 'mid' and one 'end'."""
    if kind not in ("mid", "end"):
        raise RedemptionError("Kind must be 'mid' (mid-month) or 'end' (month-end).")
    try:
        parsed = date.fromisoformat(day.strip())
    except ValueError as exc:
        raise RedemptionError("Day must look like YYYY-MM-DD (UTC).") from exc
    month = parsed.strftime("%Y-%m")
    try:
        with Tx(conn):
            wid = conn.execute(
                "INSERT INTO redemption_windows(month,kind,day) VALUES(?,?,?)", (month, kind, parsed.isoformat())
            ).lastrowid
            audit(conn, "portal", "redemptions", "window_added", actor_id, str(wid), None, f"{kind} {parsed}")
    except sqlite3.IntegrityError as exc:
        raise RedemptionError("That month already has a window of this kind.") from exc
    return wid


def open_windows(conn: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    """Windows still ahead of (or on) ``today``, soonest first."""
    return conn.execute("SELECT * FROM redemption_windows WHERE day>=? ORDER BY day", (today.isoformat(),)).fetchall()


def points_needed(conn: sqlite3.Connection, gp_value: int) -> int:
    """Points cost of a GP amount at the configured conversion (rounded up)."""
    rate = int(get_setting(conn, "points_to_gp", "100000") or 100000)
    return math.ceil(gp_value / rate)


def request(
    conn: sqlite3.Connection, discord_id: int, window_id: int, kind: str, gp_value: int, item_name: str, today: date
) -> int:
    """A member asks for a payout in one chosen window; nothing is deducted yet."""
    member = get_member(conn, discord_id)
    if member is None or member["status"] != "active":
        raise MemberError("Only clan members can redeem.")
    if kind not in ("gp", "item"):
        raise RedemptionError("Choose 'gp' or 'item'.")
    if not 0 < gp_value <= ledger.MAX_GP_REWARD:
        raise RedemptionError(f"Value must be between 1 and {ledger.MAX_GP_REWARD:,} GP.")
    if kind == "item" and not item_name.strip():
        raise RedemptionError("Name the item you want.")
    window = conn.execute("SELECT * FROM redemption_windows WHERE id=?", (window_id,)).fetchone()
    if window is None or window["day"] < today.isoformat():
        raise RedemptionError("Pick a window that has not passed yet.")
    live = conn.execute(
        "SELECT 1 FROM redemptions WHERE discord_id=? AND window_id=? AND status IN ('requested','approved','paid')",
        (discord_id, window_id),
    ).fetchone()
    if live:
        raise RedemptionError("You already have a request in that window.")
    return conn.execute(
        "INSERT INTO redemptions(discord_id,window_id,month,kind,item_name,gp_value,points_cost) VALUES(?,?,?,?,?,?,?)",
        (
            discord_id,
            window_id,
            window["month"],
            kind,
            item_name.strip()[:100] or None,
            gp_value,
            points_needed(conn, gp_value),
        ),
    ).lastrowid


def approve(conn: sqlite3.Connection, redemption_id: int, actor_id: int) -> None:
    """Admin approval: takes the monthly slot and deducts the spending points together."""
    with Tx(conn):
        r = conn.execute("SELECT * FROM redemptions WHERE id=?", (redemption_id,)).fetchone()
        if r is None or r["status"] != "requested":
            raise RedemptionError("Only requested redemptions can be approved.")
        taken = conn.execute(
            "SELECT 1 FROM redemptions WHERE discord_id=? AND month=? AND status IN ('approved','paid')",
            (r["discord_id"], r["month"]),
        ).fetchone()
        if taken:
            raise RedemptionError("That member already used their redemption for this month.")
        ledger.spend_points_in_tx(conn, r["discord_id"], r["points_cost"], f"Redemption #{redemption_id}")
        conn.execute("UPDATE redemptions SET status='approved', decided_by=? WHERE id=?", (actor_id, redemption_id))
        audit(
            conn,
            "portal",
            "redemptions",
            "approved",
            actor_id,
            str(redemption_id),
            "requested",
            "approved",
            f"{r['gp_value']:,} GP for {r['points_cost']:,} points",
        )


def reject(conn: sqlite3.Connection, redemption_id: int, actor_id: int) -> None:
    """Reject a request (no points were taken, the slot stays free)."""
    with Tx(conn):
        cur = conn.execute(
            "UPDATE redemptions SET status='rejected', decided_by=? WHERE id=? AND status='requested'",
            (actor_id, redemption_id),
        )
        if cur.rowcount == 0:
            raise RedemptionError("Only requested redemptions can be rejected.")
        audit(conn, "portal", "redemptions", "rejected", actor_id, str(redemption_id))


def pay(conn: sqlite3.Connection, redemption_id: int, actor_id: int) -> None:
    """Record the payout as a bank withdrawal (fails if the bank is short)."""
    with Tx(conn):
        r = conn.execute("SELECT * FROM redemptions WHERE id=?", (redemption_id,)).fetchone()
        if r is None or r["status"] != "approved":
            raise RedemptionError("Only approved redemptions can be paid.")
        ledger.bank_entry_in_tx(
            conn, -r["gp_value"], f"Redemption #{redemption_id} payout to {r['discord_id']}", actor_id
        )
        conn.execute("UPDATE redemptions SET status='paid' WHERE id=?", (redemption_id,))


def cancel_approved(conn: sqlite3.Connection, redemption_id: int, actor_id: int) -> None:
    """Cancel an approved-but-unpaid redemption: refund the points and free the slot."""
    with Tx(conn):
        r = conn.execute("SELECT * FROM redemptions WHERE id=?", (redemption_id,)).fetchone()
        if r is None or r["status"] != "approved":
            raise RedemptionError("Only approved, unpaid redemptions can be cancelled.")
        try:
            ledger.spend_points_in_tx(
                conn, r["discord_id"], -r["points_cost"], f"Redemption #{redemption_id} cancelled: refund", "refund"
            )
        except MemberError:
            pass  # member left meanwhile; their balance was already forfeited
        conn.execute("UPDATE redemptions SET status='cancelled', decided_by=? WHERE id=?", (actor_id, redemption_id))
        audit(conn, "portal", "redemptions", "cancelled", actor_id, str(redemption_id), "approved", "cancelled")
