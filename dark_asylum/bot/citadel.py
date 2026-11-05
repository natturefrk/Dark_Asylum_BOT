"""Citadel commands: members check their own progress, leadership records visits and caps."""

from __future__ import annotations

from discord.ext import commands

from .. import citadel, members
from .checks import leaders_only, members_only, resolve_target


class CitadelCog(commands.Cog, name="Citadel"):
    """Citadel checklist for the current cycle."""

    def __init__(self, bot: commands.Bot):
        """Keep a reference to the bot (it carries the shared DB connection)."""
        self.bot = bot

    @commands.command(name="citadel")
    @members_only()
    async def citadel_status(self, ctx: commands.Context):
        """Show your own visit/cap status for the current cycle."""
        row = citadel.status(self.bot.db, ctx.author.id)
        visited = bool(row and row["visited"])
        capped = bool(row and row["capped"])
        await ctx.send(f"Citadel this cycle: visited {'yes' if visited else 'no'}, capped {'yes' if capped else 'no'}.")

    @commands.command(name="citadel_mark")
    @leaders_only()
    async def citadel_mark(self, ctx: commands.Context, target: str, result: str = "visited"):
        """``citadel_mark <RSN|mention> [visited|capped]`` - record progress and award points once."""
        if result not in ("visited", "capped"):
            return await ctx.send("Result must be 'visited' or 'capped'.")
        discord_id, _ = await resolve_target(ctx, target)
        if discord_id is None:
            return await ctx.send("I could not find that member.")
        try:
            awards = citadel.mark(self.bot.db, discord_id, result == "capped", ctx.author.id, "bot")
        except members.MemberError as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Recorded {result} for <@{discord_id}>. " + (", ".join(awards) or "No new points."))


async def setup(bot: commands.Bot) -> None:
    """Entry point used by discord.py to register the Citadel cog."""
    await bot.add_cog(CitadelCog(bot))
