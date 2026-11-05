"""Reaction-role mappings and the safety rule that keeps privileged roles out of them."""

from __future__ import annotations

import sqlite3

from .db import audit, get_setting

# Permission names that make a role "privileged" and therefore unassignable by reaction.
PRIVILEGED_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "kick_members",
    "ban_members",
    "moderate_members",
    "manage_webhooks",
    "mention_everyone",
)


class ReactionRoleError(ValueError):
    """Invalid reaction-role configuration."""


def add_mapping(
    conn: sqlite3.Connection, channel_id: int, message_id: int, emoji: str, role_id: int, actor_id: int
) -> None:
    """Store a mapping; the bot validates the role and flips status to ok/blocked."""
    emoji = emoji.strip()
    if not emoji or len(emoji) > 64:
        raise ReactionRoleError("Emoji is required (use the emoji itself, or <:name:id> for custom emoji).")
    try:
        conn.execute(
            "INSERT INTO reaction_roles(channel_id,message_id,emoji,role_id) VALUES(?,?,?,?)",
            (channel_id, message_id, emoji, role_id),
        )
    except sqlite3.IntegrityError as exc:
        raise ReactionRoleError("That emoji is already mapped on that message.") from exc
    audit(conn, "portal", "reaction_roles", "mapping_added", actor_id, str(message_id), None, f"{emoji}->{role_id}")


def delete_mapping(conn: sqlite3.Connection, mapping_id: int, actor_id: int) -> None:
    """Remove a mapping (members keep roles they already have)."""
    row = conn.execute("SELECT * FROM reaction_roles WHERE id=?", (mapping_id,)).fetchone()
    if row is None:
        raise ReactionRoleError("Unknown mapping.")
    conn.execute("DELETE FROM reaction_roles WHERE id=?", (mapping_id,))
    audit(
        conn,
        "portal",
        "reaction_roles",
        "mapping_removed",
        actor_id,
        str(row["message_id"]),
        f"{row['emoji']}->{row['role_id']}",
        None,
    )


def role_problem(
    conn: sqlite3.Connection,
    role_id: int,
    role_name: str,
    permissions: dict[str, bool],
    is_default: bool,
    above_bot: bool,
) -> str | None:
    """Return why a role may not be handed out by reaction, or None if it is safe.

    Takes plain values (not discord objects) so the rule is unit-testable.
    """
    if is_default:
        return "@everyone cannot be assigned."
    if above_bot:
        return "Role is above the bot's top role; move the bot's role higher."
    bad = [p for p in PRIVILEGED_PERMISSIONS if permissions.get(p)]
    if bad:
        return "Role has privileged permissions: " + ", ".join(bad)
    protected = {
        get_setting(conn, k).strip().lower() for k in ("member_role", "sr_role", "jr_role", "ticket_staff_role")
    }
    if str(role_id) in protected or role_name.strip().lower() in protected:
        return "Member, leadership and staff roles cannot be self-assigned."
    return None


def lookup(conn: sqlite3.Connection, message_id: int, emoji: str) -> sqlite3.Row | None:
    """Find the mapping for a reacted emoji on a message."""
    return conn.execute("SELECT * FROM reaction_roles WHERE message_id=? AND emoji=?", (message_id, emoji)).fetchone()
