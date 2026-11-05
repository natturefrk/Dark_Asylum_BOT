"""Bot side of rewards: XP milestone progress, redemption windows, gear goal progress, message cleanup.

Admins configure everything in the portal; members query and request here.
Milestone evaluation itself runs in the RS3 cog's XP sync.
"""

from __future__ import annotations

from datetime import timedelta

import discord
from discord.ext import commands, tasks

from .. import bossing, milestones, purge, redemptions
from ..db import utcnow
from ..members import MemberError
from .checks import leaders_only, members_only


class RewardsCog(commands.Cog, name="Rewards"):
    """Member-facing rewards commands plus the message-cleanup executor."""

    def __init__(self, bot: commands.Bot):
        """Store the bot and start the cleanup loop."""
        self.bot = bot
        self.cleanup.start()

    def cog_unload(self) -> None:
        """Stop the cleanup loop."""
        self.cleanup.cancel()

    @tasks.loop(minutes=1)
    async def cleanup(self) -> None:
        """Run queued deletion jobs after checking the bot's permissions; log only counts and outcomes."""
        db = self.bot.db
        for job in db.execute("SELECT * FROM purge_jobs WHERE status='pending'").fetchall():
            channel = self.bot.get_channel(job["channel_id"])
            if channel is None or not hasattr(channel, "purge"):
                purge.finish(db, job["id"], None, "channel not found")
                continue
            perms = channel.permissions_for(channel.guild.me)
            if not (perms.manage_messages and perms.read_message_history):
                purge.finish(db, job["id"], None, "missing Manage Messages / Read History")
                continue
            cutoff = utcnow() - timedelta(days=job["days"])
            try:
                # bulk=False also removes messages older than 14 days, which bulk delete cannot.
                deleted = await channel.purge(limit=None, before=cutoff, bulk=False)
            except discord.HTTPException as exc:
                purge.finish(db, job["id"], None, f"Discord error {exc.status}")
                continue
            purge.finish(db, job["id"], len(deleted), "completed")

    @commands.command(name="purge_old")
    @leaders_only()
    async def purge_old(self, ctx: commands.Context, days: int):
        """``purge_old <30|60|90>`` - queue deletion of older messages in this channel."""
        try:
            purge.queue(self.bot.db, ctx.channel.id, days, ctx.author.id)
        except purge.PurgeError as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Queued: messages older than {days} days here will be deleted within a minute.")

    @commands.command(name="xp_milestones")
    @members_only()
    async def xp_milestones(self, ctx: commands.Context):
        """Show configured XP milestones and which ones you have already earned."""
        rows = milestones.progress(self.bot.db, ctx.author.id)
        note = "\n(No verified XP data source is configured yet, so milestones cannot trigger.)"
        lines = [
            f"{'[x]' if r['reached'] else '[ ]'} {r['scope']} {r['skill'] or ''} "
            f"{r['threshold']:,} XP: {r['points']} pts"
            for r in rows
        ]
        await ctx.send("\n".join(lines) + note if lines else "No milestones configured.")

    @commands.command(name="windows")
    @members_only()
    async def windows(self, ctx: commands.Context):
        """List upcoming redemption windows with their IDs."""
        rows = redemptions.open_windows(self.bot.db, utcnow().date())
        await ctx.send("\n".join(f"#{w['id']}: {w['day']} ({w['kind']})" for w in rows) or "No windows scheduled.")

    @commands.command(name="redeem")
    @members_only()
    async def redeem(self, ctx: commands.Context, window_id: int, gp: int, *, item: str = ""):
        """``redeem <window id> <gp value> [item name]`` - request a payout; an admin approves it."""
        kind = "item" if item else "gp"
        try:
            rid = redemptions.request(self.bot.db, ctx.author.id, window_id, kind, gp, item, utcnow().date())
        except (redemptions.RedemptionError, MemberError) as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Request #{rid} submitted. Points are only taken if an admin approves it.")

    @commands.command(name="goal")
    @members_only()
    async def goal(self, ctx: commands.Context):
        """Show progress on your Bossing Gear goal."""
        g = bossing.open_goal(self.bot.db, ctx.author.id)
        if g is None:
            return await ctx.send("You have no open goal. Ask an admin to start one.")
        await ctx.send(
            f"{g['item_name']}: {g['contributed_gp']:,} / {g['target_gp']:,} GP "
            f"({g['discount_pct']}% discount locked) - {g['status']}"
        )


async def setup(bot: commands.Bot) -> None:
    """Entry point used by discord.py to register the rewards cog."""
    await bot.add_cog(RewardsCog(bot))
