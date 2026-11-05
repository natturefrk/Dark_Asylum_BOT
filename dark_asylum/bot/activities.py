"""Bot side of clan activities: event reminders, survey buttons, giveaways, competition commands.

Portal admins create everything in the shared database; this cog *publishes*
it to Discord and records members' interactions (button clicks, commands).
"""

from __future__ import annotations

import discord
from discord.ext import commands, tasks

from .. import competitions, events, giveaways, surveys
from ..db import get_setting, iso, utcnow
from ..ledger import LedgerError
from ..members import MemberError
from .checks import members_only


class ActivitiesCog(commands.Cog, name="Activities"):
    """Publishes portal-created activities and handles member responses."""

    def __init__(self, bot: commands.Bot):
        """Store the bot and start the periodic publisher."""
        self.bot = bot
        self.publish.start()

    def cog_unload(self) -> None:
        """Stop the publisher when the cog is unloaded."""
        self.publish.cancel()

    def _channel(self, specific: str = ""):
        """Resolve a configured channel: the item's own, else the shared events channel."""
        for raw in (specific, get_setting(self.bot.db, "events_channel_id")):
            if raw and raw.isdigit() and (channel := self.bot.get_channel(int(raw))):
                return channel
        return None

    @tasks.loop(minutes=1)
    async def publish(self) -> None:
        """Send due event reminders, post new surveys/giveaways and announce giveaway winners."""
        db = self.bot.db
        for ev, occurrence, kind in events.due_reminders(db):
            channel = self._channel(ev["announce_channel_id"])
            if channel is None:
                continue
            # Each member timezone gets its own local start time in the message.
            await channel.send(events.reminder_text(db, ev["title"], occurrence, kind))
            events.mark_reminder_sent(db, ev["id"], occurrence, kind)
        for s in db.execute("SELECT * FROM surveys WHERE status='open' AND message_id=''").fetchall():
            channel = self._channel(s["channel_id"])
            if channel is None:
                continue
            view = discord.ui.View(timeout=None)
            for o in surveys.results(db, s["id"]):
                view.add_item(discord.ui.Button(label=o["label"][:80], custom_id=f"da:survey:{s['id']}:{o['id']}"))
            kind = "anonymous" if s["anonymous"] else "named"
            msg = await channel.send(f"**Survey #{s['id']}** ({kind}): {s['question']}", view=view)
            db.execute("UPDATE surveys SET message_id=? WHERE id=?", (str(msg.id), s["id"]))
        for g in db.execute("SELECT * FROM giveaways WHERE status='open' AND announced=0").fetchall():
            channel = self._channel(g["channel_id"])
            if channel is None:
                continue
            view = discord.ui.View(timeout=None)
            view.add_item(
                discord.ui.Button(label="Enter", style=discord.ButtonStyle.success, custom_id=f"da:give:{g['id']}")
            )
            await channel.send(
                f"**Giveaway #{g['id']}: {g['title']}** - first entry free, extra entries {g['entry_cost']:,} points "
                f"(max {get_setting(db, 'giveaway_max_entries', '5')}).",
                view=view,
            )
            db.execute("UPDATE giveaways SET announced=1 WHERE id=?", (g["id"],))
        # Drawn giveaways whose winner has not been announced yet (announced=1 only means "published").
        for g in db.execute("SELECT * FROM giveaways WHERE status='drawn' AND announced<2").fetchall():
            channel = self._channel(g["channel_id"])
            if channel is None:
                continue
            await channel.send(f"Giveaway #{g['id']} **{g['title']}** winner: <@{g['winner_id']}>! Congratulations!")
            db.execute("UPDATE giveaways SET announced=2 WHERE id=?", (g["id"],))

    @publish.before_loop
    async def _wait(self) -> None:
        """Wait for the bot to be connected before the first run."""
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle survey-answer and giveaway-entry button clicks."""
        cid = (
            (interaction.data or {}).get("custom_id", "")
            if interaction.type == discord.InteractionType.component
            else ""
        )
        parts = cid.split(":")
        if parts[:2] not in (["da", "survey"], ["da", "give"]):
            return
        try:
            if parts[1] == "survey":
                salt = self.bot.settings.field_key
                surveys.vote(self.bot.db, int(parts[2]), int(parts[3]), interaction.user.id, salt)
                text = "Answer recorded. Click another option to change it."
            else:
                text = giveaways.enter(self.bot.db, int(parts[2]), interaction.user.id)
        except (surveys.SurveyError, giveaways.GiveawayError, LedgerError, MemberError) as exc:
            text = str(exc)
        await interaction.response.send_message(text, ephemeral=True)

    @commands.command(name="comp_join")
    @members_only()
    async def comp_join(self, ctx: commands.Context, competition_id: int):
        """``comp_join <id>`` - enter an open competition."""
        try:
            competitions.join(self.bot.db, competition_id, ctx.author.id, ctx.author.display_name)
        except competitions.CompetitionError as exc:
            return await ctx.send(str(exc))
        await ctx.send("You are entered.")

    @commands.command(name="comp_proof")
    @members_only()
    async def comp_proof(self, ctx: commands.Context, competition_id: int, *, proof: str):
        """``comp_proof <id> <link or description>`` - submit proof for admin approval."""
        try:
            competitions.submit_proof(self.bot.db, competition_id, ctx.author.id, proof)
        except competitions.CompetitionError as exc:
            return await ctx.send(str(exc))
        await ctx.send("Proof submitted; an admin will review it.")

    @commands.command(name="event_time")
    @members_only()
    async def event_time(self, ctx: commands.Context):
        """List upcoming events in your own ``!timezone`` (UTC if unset)."""
        row = self.bot.db.execute("SELECT timezone FROM members WHERE discord_id=?", (ctx.author.id,)).fetchone()
        zone = row["timezone"] if row else None
        lines = []
        for ev in self.bot.db.execute("SELECT * FROM events WHERE status='scheduled'").fetchall():
            occ = events.next_occurrence(ev, utcnow())
            if occ:
                lines.append(f"{ev['title']}: {events.local_time(iso(occ), zone)}")
        await ctx.send("\n".join(lines[:15]) or "No upcoming events.")


async def setup(bot: commands.Bot) -> None:
    """Entry point used by discord.py to register the activities cog."""
    await bot.add_cog(ActivitiesCog(bot))
