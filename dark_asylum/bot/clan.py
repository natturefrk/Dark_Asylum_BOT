"""Member-facing commands: linking, join date, bank, points, XP and clan name."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from discord.ext import commands

from .. import ledger, members
from ..db import audit, get_setting, set_setting
from .checks import CLAN_NAME_OWNERS, is_leader, leaders_only, members_only, notify_staff, resolve_target


class ClanCog(commands.Cog, name="Clan"):
    """Commands members use day to day."""

    def __init__(self, bot: commands.Bot):
        """Keep a reference to the bot (it carries the shared DB connection)."""
        self.bot = bot

    @commands.command(name="track")
    @members_only()
    async def track(self, ctx: commands.Context, rsn: str, join_date: str | None = None):
        """``track <RSN> [MM-DD-YYYY]`` - link your own RSN (leadership uses ``track-for``)."""
        conn = self.bot.db
        try:
            iso_date = members.parse_join_date(join_date) if join_date else None
            members.link_rsn(conn, ctx.author.id, rsn, ctx.author.id, iso_date)
        except members.MemberError as exc:
            # Tell leadership about duplicate RSN attempts so they can investigate.
            if "already linked" in str(exc):
                await notify_staff(self.bot, f"{ctx.author.mention} tried to link an RSN that is already taken: {rsn}")
            return await ctx.send(str(exc))
        await ctx.send(f"Linked {ctx.author.mention} to RSN '{members.clean_rsn(rsn)}'.")

    @commands.command(name="track-for")
    @leaders_only()
    async def track_for(self, ctx: commands.Context, target: str, rsn: str, join_date: str | None = None):
        """``track-for <member> <RSN> [MM-DD-YYYY]`` - leadership links an RSN for someone."""
        discord_id, _ = await resolve_target(ctx, target)
        if discord_id is None:
            return await ctx.send("Use a mention or Discord ID for a member who has no RSN yet.")
        try:
            iso_date = members.parse_join_date(join_date) if join_date else None
            members.link_rsn(self.bot.db, discord_id, rsn, ctx.author.id, iso_date)
        except members.MemberError as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Linked <@{discord_id}> to '{members.clean_rsn(rsn)}'.")

    @commands.command(name="join-date", aliases=["join_date"])
    @members_only()
    async def join_date(self, ctx: commands.Context, *args: str):
        """``join-date [discord ID] <MM-DD-YYYY>`` - set your join date (leadership may set anyone's)."""
        if not args or len(args) > 2:
            return await ctx.send("Usage: join-date [discord ID] MM-DD-YYYY")
        target_id = ctx.author.id
        if len(args) == 2:
            if not is_leader(self.bot.db, ctx.author):
                return await ctx.send("Only leadership can set someone else's join date.")
            if not args[0].strip("<@!>").isdigit():
                return await ctx.send("The first argument must be a Discord ID or mention.")
            target_id = int(args[0].strip("<@!>"))
        try:
            members.set_join_date(self.bot.db, target_id, members.parse_join_date(args[-1]), ctx.author.id)
        except members.MemberError as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Join date updated for <@{target_id}>.")

    @commands.command(name="clan_name")
    async def clan_name(self, ctx: commands.Context, *, name: str):
        """``clan_name <name>`` - restricted to two specific Discord IDs."""
        if ctx.author.id not in CLAN_NAME_OWNERS:
            return await ctx.send("You are not allowed to change the clan name.")
        old = get_setting(self.bot.db, "clan_name")
        set_setting(self.bot.db, "clan_name", name.strip()[:64])
        audit(self.bot.db, "bot", "settings", "clan_name", ctx.author.id, "clan_name", old, name.strip()[:64])
        await ctx.send(f"Clan name set to {name.strip()[:64]}.")

    @commands.command(name="timezone")
    @members_only()
    async def timezone(self, ctx: commands.Context, zone: str):
        """``timezone <Area/City>`` - used later to localise event announcements."""
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            return await ctx.send("Unknown timezone. Use an IANA name such as Europe/London.")
        self.bot.db.execute("UPDATE members SET timezone=? WHERE discord_id=?", (zone, ctx.author.id))
        await ctx.send(f"Timezone saved: {zone}.")

    @commands.command(name="bank")
    @members_only()
    async def bank(self, ctx: commands.Context):
        """Show the clan bank GP balance (members only)."""
        await ctx.send(f"Clan bank balance: {ledger.bank_balance(self.bot.db):,} GP")

    @commands.command(name="points")
    @members_only()
    async def points(self, ctx: commands.Context):
        """Show your own balances and recent history; also the public top earners."""
        member = members.get_member(self.bot.db, ctx.author.id)
        if member is None or member["status"] != "active":
            return await ctx.send("You are not tracked yet. Use track <RSN> first.")
        lines = [f"Ranking points: {member['ranking_points']:,} | Spending points: {member['spending_points']:,}"]
        for row in ledger.points_history(self.bot.db, ctx.author.id, 5):
            lines.append(f"{row['created_at'][:10]} {row['ranking_delta'] + row['spending_delta']:+d} {row['reason']}")
        top = ", ".join(f"{r['rsn']} ({r['total']:,})" for r in ledger.leaderboard(self.bot.db, 5))
        lines.append(f"Top earners: {top}")
        await ctx.send("\n".join(lines))

    @commands.command(name="point_values")
    async def point_values(self, ctx: commands.Context):
        """Plain-text list of point values (read from the portal-editable table)."""
        rows = self.bot.db.execute("SELECT name, amount FROM point_awards ORDER BY id").fetchall()
        rate = int(get_setting(self.bot.db, "points_to_gp", "100000"))
        text = "\n".join(f"{r['name']}: {r['amount']} points" for r in rows)
        await ctx.send(f"Point values\n{text}\n1 point = {rate:,} GP (draft conversion)")


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(ClanCog(bot))
