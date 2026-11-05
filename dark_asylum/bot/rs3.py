"""Bot side of the RuneScape 3 data features: background syncs plus XP / Citadel / DXP commands.

The loops keep the shared SQLite database fresh (clan roster, daily XP snapshots, Citadel
adventure-log polling, XP-contest snapshots) and alert the staff channel when an upstream API
goes down or recovers. Commands only read what the loops stored, except ``!xp`` for players
with no snapshot yet, which asks the live API once.
"""

from __future__ import annotations

from discord.ext import commands, tasks

from .. import citadel, members, rs3sync, xp
from ..db import utcnow
from .checks import members_only, notify_staff

PERIODS = {"day": 1, "week": 7, "month": 30}  # accepted period words -> days


class Rs3Cog(commands.Cog, name="RS3"):
    """Background RS3 syncing and read-only stat commands."""

    def __init__(self, bot: commands.Bot):
        """Store the bot and start the four sync loops."""
        self.bot = bot
        db = bot.db
        # Intervals come from settings (portal-editable); a change takes effect after a bot restart.
        self.roster.change_interval(minutes=rs3sync.interval(db, "roster_interval_minutes", 60, 15, 1440))
        self.citadel_poll.change_interval(minutes=rs3sync.interval(db, "citadel_poll_minutes", 30, 10, 120))
        for loop in (self.roster, self.snapshots, self.citadel_poll, self.contests):
            loop.start()

    def cog_unload(self) -> None:
        """Stop every loop."""
        for loop in (self.roster, self.snapshots, self.citadel_poll, self.contests):
            loop.cancel()

    async def _alert(self, alerts: list[str]) -> None:
        """Forward sync messages (API down/recovered, milestone awards) to the staff channel."""
        for text in alerts:
            await notify_staff(self.bot, text)

    @tasks.loop(hours=1)
    async def roster(self) -> None:
        """Refresh the clan roster (default hourly; setting ``roster_interval_minutes``)."""
        await self._alert(await rs3sync.sync_roster(self.bot.db, xp.get_provider()))

    @tasks.loop(minutes=15)
    async def snapshots(self) -> None:
        """Cheap check every 15 minutes; a real pass only runs when the snapshot interval (default 6h) elapsed."""
        await self._alert(await rs3sync.sync_xp(self.bot.db, xp.get_provider()))

    @tasks.loop(minutes=30)
    async def citadel_poll(self) -> None:
        """Poll adventure logs (default 30 min; setting ``citadel_poll_minutes``, max 120).

        Only the latest 20 log lines are served, so the interval must stay short enough that a
        visit/cap is never pushed out of that window before we see it.
        """
        await self._alert(await rs3sync.sync_citadel(self.bot.db, xp.get_provider()))

    @tasks.loop(minutes=15)
    async def contests(self) -> None:
        """Copy contest-skill XP from member snapshots into running XP contests (database only)."""
        rs3sync.sync_competitions(self.bot.db)

    @roster.before_loop
    @snapshots.before_loop
    @citadel_poll.before_loop
    @contests.before_loop
    async def _wait(self) -> None:
        """Hold each loop until the bot has connected, so alerts can be delivered."""
        await self.bot.wait_until_ready()

    def _resolve(self, ctx: commands.Context, rsn: str | None):
        """The member row for an RSN, or for the caller when no RSN is given."""
        if rsn:
            return members.find_by_rsn(self.bot.db, rsn)
        return members.get_member(self.bot.db, ctx.author.id)

    @commands.command(name="xp")
    @members_only()
    async def xp_cmd(self, ctx: commands.Context, *, rsn: str | None = None):
        """``xp [rsn]`` - latest total XP and day/week/month gains from our periodic snapshots."""
        db = self.bot.db
        member = self._resolve(ctx, rsn)
        total = rs3sync.latest_total(db, member["discord_id"]) if member else None
        if total is None:
            return await ctx.send("No XP snapshot for that member yet (snapshots run every few hours).")
        lines = [f"{member['rsn']}: total XP {total:,}"]
        for label, days in PERIODS.items():
            gain, since = rs3sync.xp_gain(db, member["discord_id"], days)
            lines.append(f"{label}: +{gain:,} (baseline {since[:16].replace('T', ' ')} UTC)")
        await ctx.send("\n".join(lines))

    @commands.command(name="cxp")
    @members_only()
    async def cxp(self, ctx: commands.Context, *, rsn: str | None = None):
        """``cxp [rsn]`` - clan XP and rank from the public clan roster."""
        member = self._resolve(ctx, rsn)
        name = rsn or (member["rsn"] if member else None)
        row = rs3sync.clan_xp_for(self.bot.db, name) if name else None
        if row is None:
            return await ctx.send("Not found on the clan roster (it refreshes hourly; check the clan name setting).")
        await ctx.send(f"{row['rsn']}: {row['rank']}, clan XP {row['total_xp']:,}")

    @commands.command(name="gains")
    @members_only()
    async def gains(self, ctx: commands.Context, *args: str):
        """``gains [rsn] [day|week|month]`` - XP gained over a period (default week)."""
        words = list(args)
        period = words.pop().lower() if words and words[-1].lower() in PERIODS else "week"
        member = self._resolve(ctx, " ".join(words) or None)
        result = rs3sync.xp_gain(self.bot.db, member["discord_id"], PERIODS[period]) if member else None
        if result is None:
            return await ctx.send("No XP snapshots for that member yet.")
        await ctx.send(f"{member['rsn']} {period}: +{result[0]:,} XP (baseline {result[1][:16].replace('T', ' ')} UTC)")

    @commands.command(name="visits")
    @members_only()
    async def visits(self, ctx: commands.Context):
        """List members who have visited the Citadel this cycle."""
        names = self._citadel_names("visited")
        await ctx.send(f"Visited this cycle ({len(names)}): " + (", ".join(names) or "nobody yet"))

    @commands.command(name="capping")
    @members_only()
    async def capping(self, ctx: commands.Context):
        """Citadel cap status this cycle, plus members whose private profile needs a manual check."""
        db = self.bot.db
        capped = self._citadel_names("capped")
        private = [r["rsn"] for r in db.execute("SELECT rsn FROM members WHERE status='active' AND profile_private=1")]
        text = f"Capped this cycle ({len(capped)}): " + (", ".join(capped) or "nobody yet")
        if private:
            text += f"\nPrivate profiles (staff check manually): {', '.join(private)}"
        await ctx.send(text)

    def _citadel_names(self, column: str) -> list[str]:
        """RSNs with the given flag ('visited' or 'capped') set in the open cycle."""
        rows = self.bot.db.execute(
            f"SELECT m.rsn FROM citadel_entries e JOIN members m USING(discord_id) "
            f"WHERE e.cycle_id=? AND e.{column}=1 ORDER BY m.rsn",
            (citadel.current_cycle(self.bot.db),),
        )
        return [r["rsn"] for r in rows]

    @commands.command(name="dxp")
    async def dxp(self, ctx: commands.Context):
        """Show the current or next double-XP period (dates are entered by admins in the portal)."""
        today = utcnow().date().isoformat()
        row = self.bot.db.execute(
            "SELECT * FROM dxp_periods WHERE ends_on>=? ORDER BY starts_on LIMIT 1", (today,)
        ).fetchone()
        if row is None:
            return await ctx.send("No double XP dates have been set.")
        state = "ACTIVE now" if row["starts_on"] <= today else "upcoming"
        note = f" - {row['note']}" if row["note"] else ""
        await ctx.send(f"Double XP ({state}): {row['starts_on']} to {row['ends_on']} UTC{note}")


async def setup(bot: commands.Bot) -> None:
    """Entry point used by discord.py to register the RS3 cog."""
    await bot.add_cog(Rs3Cog(bot))
