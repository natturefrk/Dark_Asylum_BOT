"""Pure moderation rules (no Discord objects) so they are easy to test."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from .db import Tx, audit
from .members import current_period

# Prefix characters leadership may choose from.
VALID_PREFIXES = ("!", "#", "\\", "&", "$")
# Discord timeouts cannot exceed 28 days.
MAX_TIMEOUT = timedelta(days=28)
TEMP_ROLE_MIN = timedelta(minutes=10)
TEMP_ROLE_MAX = timedelta(hours=24)

_DURATION = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_duration(raw: str) -> timedelta:
    """Parse ``30m``/``2h``/``1d``/``1w`` into a timedelta."""
    match = _DURATION.match(raw.strip())
    if not match:
        raise ValueError("Durations look like 30m, 2h, 1d or 1w.")
    return timedelta(**{_UNITS[match.group(2).lower()]: int(match.group(1))})


@dataclass(frozen=True)
class StrikeOutcome:
    """What the bot must do for a given strike number."""

    action: str  # verbal_warning | timeout_24h | permanent_ban
    description: str


def strike_outcome(strike_number: int) -> StrikeOutcome:
    """Strike 1 = verbal warning, 2 = 24h timeout, 3 or more = permanent ban."""
    if strike_number <= 1:
        return StrikeOutcome("verbal_warning", "a verbal warning")
    if strike_number == 2:
        return StrikeOutcome("timeout_24h", "a 24 hour timeout")
    return StrikeOutcome("permanent_ban", "a permanent ban")


def add_strike(
    conn: sqlite3.Connection, discord_id: int, reason: str, issued_by: int, dm_delivered: bool = False
) -> tuple[int, StrikeOutcome]:
    """Persist a new strike in the member's current period and return its outcome."""
    with Tx(conn):
        period = current_period(conn, discord_id)
        if period is None:
            # Non-members can be warned too; open a period lazily so strikes are scoped.
            conn.execute("INSERT OR IGNORE INTO members(discord_id) VALUES(?)", (discord_id,))
            conn.execute("INSERT INTO membership_periods(discord_id) VALUES(?)", (discord_id,))
            period = current_period(conn, discord_id)
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM warnings WHERE discord_id=? AND period_id=?", (discord_id, period)
        ).fetchone()["c"]
        number = count + 1
        outcome = strike_outcome(number)
        conn.execute(
            "INSERT INTO warnings(discord_id,period_id,strike_number,reason,issued_by,action_taken,dm_delivered) "
            "VALUES(?,?,?,?,?,?,?)",
            (discord_id, period, number, reason, issued_by, outcome.action, int(dm_delivered)),
        )
        audit(
            conn,
            "bot",
            "moderation",
            "warn",
            issued_by,
            str(discord_id),
            None,
            f"strike {number}: {outcome.action}",
            reason,
        )
    return number, outcome
