"""Bot entry point: builds the client, global checks, custom commands and background jobs."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from .. import backup
from ..config import Settings
from ..db import audit, connect, get_setting, heartbeat, migrate, now_iso, purge_old_audit
from .checks import is_leader, notify_staff

log = logging.getLogger("dark_asylum")


class DarkAsylumBot(commands.Bot):
    """Discord client with a database-backed prefix and custom commands."""

    def __init__(self, settings: Settings):
        """Build the bot with the intents it needs and attach settings and the shared DB."""
        intents = discord.Intents.default()
        intents.message_content = True  # needed for prefix commands
        intents.members = True  # needed for role checks and moderation
        self.settings = settings
        self.db = connect(settings.database_path)
        migrate(self.db)
        super().__init__(command_prefix=self._prefix, intents=intents, help_command=None)
        self.add_check(self._channel_gate)

    def _prefix(self, bot, message) -> list[str]:
        """Built-in and custom prefixes both come from the shared database settings."""
        return [get_setting(self.db, "prefix", "!")]

    async def _channel_gate(self, ctx: commands.Context) -> bool:
        """Members may only run commands in the shared command channel; leadership anywhere."""
        if ctx.guild is None:
            return False
        if self.settings.guild_id and ctx.guild.id != self.settings.guild_id:
            return False
        allowed = get_setting(self.db, "command_channel_id")
        if allowed.isdigit() and ctx.channel.id != int(allowed) and not is_leader(self.db, ctx.author):
            return False
        return True

    async def setup_hook(self) -> None:
        """Load command groups and start background loops."""
        await self.load_extension("dark_asylum.bot.clan")
        await self.load_extension("dark_asylum.bot.moderation")
        for ext in ("tickets", "reactions", "citadel", "activities", "rewards", "rs3"):
            await self.load_extension(f"dark_asylum.bot.{ext}")
        self.beat.start()
        self.expire_roles.start()
        self.daily.start()

    async def on_message(self, message: discord.Message) -> None:
        """Dispatch built-in commands, falling back to saved-text custom commands."""
        if message.author.bot or message.guild is None:
            return
        await self.process_commands(message)
        cprefix = get_setting(self.db, "cprefix", "!")
        if message.content.startswith(cprefix):
            name = message.content[len(cprefix) :].split(maxsplit=1)[0:1]
            if name and self.get_command(name[0]) is None:
                ctx = await self.get_context(message)
                if await self._channel_gate(ctx):
                    row = self.db.execute(
                        "SELECT response FROM custom_commands WHERE name=? AND enabled=1", (name[0],)
                    ).fetchone()
                    if row:
                        # allowed_mentions off so a saved text cannot mass-ping.
                        await message.channel.send(row["response"], allowed_mentions=discord.AllowedMentions.none())

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Friendly plain-text errors; unexpected ones are logged, not leaked."""
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)) and not isinstance(
            error, commands.MissingRole
        ):
            if isinstance(error, commands.CheckFailure) and str(error):
                await ctx.send(str(error))
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.RoleNotFound)):
            await ctx.send(f"Check your command arguments: {error}")
            return
        if isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
            await ctx.send("I don't have permission to do that (check my role position).")
            return
        log.exception("Command error", exc_info=error)
        await ctx.send("Something went wrong; leadership has been notified.")
        await notify_staff(self, f"Command error in {ctx.command}: {type(error).__name__}")

    # ---- background jobs ----------------------------------------------

    @tasks.loop(seconds=30)
    async def beat(self) -> None:
        """Heartbeat so the portal can tell the bot is alive."""
        heartbeat(self.db, "bot", "ok", f"{len(self.guilds)} guild(s)")

    @tasks.loop(minutes=1)
    async def expire_roles(self) -> None:
        """Remove temporary roles whose time is up."""
        due = self.db.execute(
            "SELECT * FROM temp_roles WHERE removed_at IS NULL AND expires_at <= ?", (now_iso(),)
        ).fetchall()
        for row in due:
            guild = self.get_guild(row["guild_id"])
            member = guild.get_member(row["discord_id"]) if guild else None
            role = guild.get_role(row["role_id"]) if guild else None
            try:
                if member and role:
                    await member.remove_roles(role, reason="temp_role expired")
            except discord.HTTPException:
                audit(self.db, "bot", "moderation", "temp_role_removal_failed", None, str(row["discord_id"]))
                continue  # retry next minute
            self.db.execute("UPDATE temp_roles SET removed_at=? WHERE id=?", (now_iso(), row["id"]))

    @tasks.loop(hours=24)
    async def daily(self) -> None:
        """Backup, then purge old audit rows. Failures alert staff (never silent)."""
        errors = backup.run_backup(self.db, self.settings.backup_dir, self.settings.backup_secondary_dir)
        if errors:
            await notify_staff(self, "Backup problem: " + "; ".join(errors))
        purge_old_audit(self.db)

    @beat.before_loop
    @expire_roles.before_loop
    @daily.before_loop
    async def _wait(self) -> None:
        """Delay the loop's first run until the bot has connected to Discord."""
        await self.wait_until_ready()


def run_bot(settings: Settings) -> None:
    """Start the bot (blocking)."""
    logging.basicConfig(level=logging.INFO)
    DarkAsylumBot(settings).run(settings.discord_token)
