"""Reaction roles: validate portal-configured mappings, seed reactions, grant/remove roles."""

from __future__ import annotations

import discord
from discord.ext import commands, tasks

from .. import reaction_roles as rr


class ReactionRoleCog(commands.Cog, name="ReactionRoles"):
    """Applies mappings stored in the shared database."""

    def __init__(self, bot: commands.Bot):
        """Store the bot and start the periodic mapping validator."""
        self.bot = bot
        self.validate.start()

    def cog_unload(self) -> None:
        """Stop the validator loop when the cog is unloaded."""
        self.validate.cancel()

    def _problem(self, guild: discord.Guild, role: discord.Role | None) -> str | None:
        """Check a role against the privileged-role rules."""
        if role is None:
            return "Role no longer exists."
        perms = {name: getattr(role.permissions, name) for name in rr.PRIVILEGED_PERMISSIONS}
        return rr.role_problem(self.bot.db, role.id, role.name, perms, role.is_default(), role >= guild.me.top_role)

    @tasks.loop(minutes=1)
    async def validate(self) -> None:
        """Mark each mapping ok/blocked and add the bot's own reaction so people can click it."""
        for row in self.bot.db.execute("SELECT * FROM reaction_roles").fetchall():
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None:
                continue
            role = channel.guild.get_role(row["role_id"])
            problem = self._problem(channel.guild, role)
            status, note = ("blocked", problem) if problem else ("ok", None)
            if problem is None and row["status"] != "ok":
                try:
                    message = await channel.fetch_message(row["message_id"])
                    await message.add_reaction(row["emoji"])
                except discord.HTTPException:
                    status, note = "blocked", "Could not find the message or add the emoji."
            self.bot.db.execute(
                "UPDATE reaction_roles SET status=?, status_note=? WHERE id=?", (status, note, row["id"])
            )

    @validate.before_loop
    async def _wait(self) -> None:
        """Delay the validator's first run until the bot is ready."""
        await self.bot.wait_until_ready()

    async def _apply(self, payload: discord.RawReactionActionEvent, add: bool) -> None:
        """Grant or remove the mapped role; multiple selections are independent."""
        if payload.user_id == self.bot.user.id or payload.guild_id is None:
            return
        row = rr.lookup(self.bot.db, payload.message_id, str(payload.emoji))
        if row is None or row["status"] != "ok":
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        role = guild.get_role(row["role_id"]) if guild else None
        # Re-check at click time: permissions may have changed since validation.
        if member is None or role is None or self._problem(guild, role):
            return
        try:
            await (member.add_roles if add else member.remove_roles)(role, reason="reaction role")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """A reaction was added: grant the mapped role."""
        await self._apply(payload, True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """A reaction was removed: take the mapped role away again."""
        await self._apply(payload, False)


async def setup(bot: commands.Bot) -> None:
    """Entry point used by discord.py to register the reaction-role cog."""
    await bot.add_cog(ReactionRoleCog(bot))
