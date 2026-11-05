"""Leadership commands: prefixes, kick/ban/timeout, strikes and temporary roles.

All output is plain text (never embeds). Every action respects Discord's role
hierarchy and DMs the target, logging DM failures.
"""

from __future__ import annotations

from datetime import timedelta

import discord
from discord.ext import commands

from .. import rules
from ..db import audit, get_setting, iso, set_setting, utcnow
from .checks import (
    hierarchy_problem,
    leaders_only,
    notify_staff,
    resolve_target,
    try_dm,
)


class ModerationCog(commands.Cog, name="Moderation"):
    """Leadership-only tools."""

    def __init__(self, bot: commands.Bot):
        """Keep a reference to the bot (it carries the shared DB connection)."""
        self.bot = bot

    async def _target(self, ctx: commands.Context, text: str) -> discord.Member | None:
        """Resolve a guild member target or explain the failure to the caller."""
        _, member = await resolve_target(ctx, text)
        if member is None:
            await ctx.send(
                "I could not find that member. Use a mention, Discord ID or a linked RSN (quote names with spaces)."
            )
            return None
        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.send(problem)
            return None
        return member

    async def _set_prefix(self, ctx: commands.Context, key: str, value: str):
        """Validate and store a prefix setting, logging before/after."""
        if value not in rules.VALID_PREFIXES:
            return await ctx.send("Valid prefixes: " + " ".join(rules.VALID_PREFIXES))
        old = get_setting(self.bot.db, key)
        set_setting(self.bot.db, key, value)
        audit(self.bot.db, "bot", "settings", "setting_changed", ctx.author.id, key, old, value)
        await ctx.send(f"{key} changed from {old} to {value}")

    @commands.command(name="prefix")
    @leaders_only()
    async def prefix(self, ctx: commands.Context, value: str):
        """``prefix <char>`` - change the built-in command prefix."""
        await self._set_prefix(ctx, "prefix", value)

    @commands.command(name="cprefix")
    @leaders_only()
    async def cprefix(self, ctx: commands.Context, value: str):
        """``cprefix <char>`` - change the custom command prefix."""
        await self._set_prefix(ctx, "cprefix", value)

    @commands.command(name="kick")
    @leaders_only()
    async def kick(self, ctx: commands.Context, target: str, *, reason: str = "No reason given"):
        """``kick <RSN|mention> [reason]`` - kick (not a strike)."""
        member = await self._target(ctx, target)
        if member is None:
            return
        dm = await try_dm(ctx, member, f"You were kicked from {ctx.guild.name}. Reason: {reason}")
        await member.kick(reason=f"{ctx.author}: {reason}")
        audit(self.bot.db, "bot", "moderation", "kick", ctx.author.id, str(member.id), None, None, reason)
        await ctx.send(f"Kicked {member} ({member.id}). Reason: {reason}. DM {'sent' if dm else 'failed'}.")

    @commands.command(name="ban")
    @leaders_only()
    async def ban(self, ctx: commands.Context, target: str, *, reason: str = "No reason given"):
        """``ban <RSN|mention> [reason]`` - permanent ban (not a strike)."""
        member = await self._target(ctx, target)
        if member is None:
            return
        dm = await try_dm(
            ctx, member, f"You were banned from {ctx.guild.name}. Reason: {reason}. Bans cannot be appealed."
        )
        await member.ban(reason=f"{ctx.author}: {reason}")
        audit(self.bot.db, "bot", "moderation", "ban", ctx.author.id, str(member.id), None, None, reason)
        await ctx.send(f"Banned {member} ({member.id}). Reason: {reason}. DM {'sent' if dm else 'failed'}.")

    @commands.command(name="temp_ban")
    @leaders_only()
    async def temp_ban(self, ctx: commands.Context, target: str, duration: str, *, reason: str = "No reason given"):
        """``temp_ban <target> <duration> [reason]`` - applies a Discord TIMEOUT (max 28d)."""
        member = await self._target(ctx, target)
        if member is None:
            return
        try:
            span = rules.parse_duration(duration)
        except ValueError as exc:
            return await ctx.send(str(exc))
        if span > rules.MAX_TIMEOUT:
            return await ctx.send("Discord timeouts cannot exceed 28 days.")
        dm = await try_dm(ctx, member, f"You were timed out in {ctx.guild.name} for {duration}. Reason: {reason}")
        await member.timeout(span, reason=f"{ctx.author}: {reason}")
        audit(self.bot.db, "bot", "moderation", "timeout", ctx.author.id, str(member.id), None, duration, reason)
        await ctx.send(f"Timed out {member} for {duration}. Reason: {reason}. DM {'sent' if dm else 'failed'}.")

    @commands.command(name="warn")
    @leaders_only()
    async def warn(self, ctx: commands.Context, target: str, *, reason: str):
        """``warn <RSN|mention> <reason>`` - strike 1 warning, 2 = 24h timeout, 3 = ban."""
        member = await self._target(ctx, target)
        if member is None:
            return
        number, outcome = rules.add_strike(self.bot.db, member.id, reason, ctx.author.id)
        dm = await try_dm(
            ctx,
            member,
            f"Strike {number} in {ctx.guild.name}: {outcome.description}. Reason: {reason}"
            + (" Bans cannot be appealed." if outcome.action == "permanent_ban" else ""),
        )
        self.bot.db.execute(
            "UPDATE warnings SET dm_delivered=? WHERE discord_id=? AND strike_number=? "
            "AND id=(SELECT MAX(id) FROM warnings WHERE discord_id=?)",
            (int(dm), member.id, number, member.id),
        )
        # Apply the automatic consequence for this strike.
        if outcome.action == "timeout_24h":
            await member.timeout(timedelta(hours=24), reason=f"Strike 2: {reason}")
        elif outcome.action == "permanent_ban":
            await member.ban(reason=f"Strike 3: {reason}")
        text = (
            f"Strike {number} issued to {member} ({member.id}): {outcome.description}. "
            f"Reason: {reason}. DM {'sent' if dm else 'failed'}."
        )
        await ctx.send(text)
        await notify_staff(self.bot, text)

    @commands.command(name="temp_role")
    @leaders_only()
    async def temp_role(self, ctx: commands.Context, target: str, role: discord.Role, duration: str):
        """``temp_role <target> <role> <duration>`` - 10 minutes to 24 hours, auto-removed."""
        member = await self._target(ctx, target)
        if member is None:
            return
        try:
            span = rules.parse_duration(duration)
        except ValueError as exc:
            return await ctx.send(str(exc))
        if not rules.TEMP_ROLE_MIN <= span <= rules.TEMP_ROLE_MAX:
            return await ctx.send("Temporary roles last between 10 minutes and 24 hours.")
        # Members must never be able to gain privileged roles through this command.
        too_high = role >= ctx.guild.me.top_role or (
            ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role
        )
        perms = role.permissions
        if role.is_default() or too_high or perms.administrator or perms.manage_guild or perms.manage_roles:
            return await ctx.send("That role is too privileged or above your/the bot's role.")
        await member.add_roles(role, reason=f"temp_role by {ctx.author}")
        self.bot.db.execute(
            "INSERT INTO temp_roles(guild_id,discord_id,role_id,expires_at,granted_by) VALUES(?,?,?,?,?)",
            (ctx.guild.id, member.id, role.id, iso(utcnow() + span), ctx.author.id),
        )
        audit(self.bot.db, "bot", "moderation", "temp_role", ctx.author.id, str(member.id), None, role.name, duration)
        await ctx.send(f"Gave {member} the role {role.name} for {duration}.")


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(ModerationCog(bot))
