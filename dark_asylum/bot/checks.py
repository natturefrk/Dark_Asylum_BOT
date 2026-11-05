"""Shared helpers for bot commands: permissions, target lookup, DMs and staff notices."""

from __future__ import annotations

import re
import sqlite3

import discord
from discord.ext import commands

from ..db import audit, get_setting
from ..members import find_by_rsn

# Discord IDs allowed to rename the clan (fixed by the clan rules).
CLAN_NAME_OWNERS = (1194480773114368153, 295404063707889664)

_ID_PATTERN = re.compile(r"^(?:<@!?(\d{15,21})>|(\d{15,21}))$")


def role_matches(member: discord.Member, configured: str) -> bool:
    """True if the member holds the role identified by a configured name or numeric ID."""
    configured = configured.strip()
    if not configured:
        return False
    return any(
        (configured.isdigit() and role.id == int(configured)) or role.name.lower() == configured.lower()
        for role in member.roles
    )


def is_leader(conn: sqlite3.Connection, member: discord.abc.User) -> bool:
    """Leadership = Sr_Leadership or Jr_leadership (names/IDs come from settings)."""
    if not isinstance(member, discord.Member):
        return False
    return role_matches(member, get_setting(conn, "sr_role")) or role_matches(member, get_setting(conn, "jr_role"))


def is_clan_member(conn: sqlite3.Connection, member: discord.abc.User) -> bool:
    """A 'member' holds the configured @Member role (guests are everyone else)."""
    return isinstance(member, discord.Member) and role_matches(member, get_setting(conn, "member_role"))


def leaders_only():
    """Command check: leadership only."""

    async def predicate(ctx: commands.Context) -> bool:
        """Allow the command only when the invoking user passes the permission check."""
        if not is_leader(ctx.bot.db, ctx.author):
            raise commands.CheckFailure("This command is for leadership only.")
        return True

    return commands.check(predicate)


def members_only():
    """Command check: clan members (leadership counts as well)."""

    async def predicate(ctx: commands.Context) -> bool:
        """Allow the command only when the invoking user passes the permission check."""
        if not (is_clan_member(ctx.bot.db, ctx.author) or is_leader(ctx.bot.db, ctx.author)):
            raise commands.CheckFailure("This command is for clan members only.")
        return True

    return commands.check(predicate)


async def resolve_target(ctx: commands.Context, text: str) -> tuple[int | None, discord.Member | None]:
    """Turn a mention, raw Discord ID or RSN into ``(discord_id, Member-or-None)``.

    RSNs with spaces must be quoted. IDs are the identity key; RSNs are looked up
    only among active members, so a name can never be confused with another person.
    """
    match = _ID_PATTERN.match(text.strip())
    discord_id: int | None = None
    if match:
        discord_id = int(match.group(1) or match.group(2))
    else:
        row = find_by_rsn(ctx.bot.db, text.strip())
        if row:
            discord_id = row["discord_id"]
    if discord_id is None:
        return None, None
    member = ctx.guild.get_member(discord_id) if ctx.guild else None
    return discord_id, member


def hierarchy_problem(ctx: commands.Context, target: discord.Member) -> str | None:
    """Explain why moderating ``target`` is not allowed, or return None if it is fine."""
    guild = ctx.guild
    if target.id == ctx.author.id:
        return "You cannot do that to yourself."
    if target.id == guild.owner_id:
        return "The server owner cannot be moderated."
    if ctx.author.id != guild.owner_id and target.top_role >= ctx.author.top_role:
        return "That person's top role is equal to or above yours."
    if target.top_role >= guild.me.top_role:
        return "That person's top role is above the bot's; move the bot's role higher."
    return None


async def try_dm(ctx: commands.Context, user: discord.abc.User, text: str) -> bool:
    """DM a user; failures are logged (not raised) because DMs may be closed."""
    try:
        await user.send(text)
        return True
    except (discord.Forbidden, discord.HTTPException):
        audit(ctx.bot.db, "bot", "moderation", "dm_failed", ctx.author.id, str(user.id))
        return False


async def notify_staff(bot: commands.Bot, text: str, guild: discord.Guild | None = None) -> None:
    """Post a plain-text notice to the configured staff channel, if there is one."""
    channel_id = get_setting(bot.db, "staff_channel_id")
    if not channel_id.isdigit():
        return
    channel = bot.get_channel(int(channel_id))
    if channel is not None:
        try:
            await channel.send(text)
        except discord.HTTPException:
            audit(bot.db, "bot", "system", "staff_notice_failed", detail=text[:200])
