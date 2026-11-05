"""Ticket system: dropdown panel, choose-then-submit, private threads, claim/close buttons, archive."""

from __future__ import annotations

import io

import discord
from discord.ext import commands

from .. import tickets
from ..db import get_setting
from .checks import is_leader, leaders_only, notify_staff, role_matches


def is_ticket_staff(bot: commands.Bot, member: discord.abc.User) -> bool:
    """The single shared ticket staff team (leadership also counts)."""
    return isinstance(member, discord.Member) and (
        role_matches(member, get_setting(bot.db, "ticket_staff_role")) or is_leader(bot.db, member)
    )


class SubmitView(discord.ui.View):
    """Ephemeral second step: shows the chosen category and waits for an explicit Submit."""

    def __init__(self, bot: commands.Bot, type_name: str):
        """Store the bot (and, where relevant, the chosen category) for the callbacks."""
        super().__init__(timeout=120)
        self.bot, self.type_name = bot, type_name

    @discord.ui.button(label="Submit ticket", style=discord.ButtonStyle.success)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Create the private thread, add the opener and post the staff controls."""
        channel_id = get_setting(self.bot.db, "ticket_channel_id")
        parent = self.bot.get_channel(int(channel_id)) if channel_id.isdigit() else interaction.channel
        if not isinstance(parent, discord.TextChannel):
            return await interaction.response.edit_message(content="Ticket channel is not configured.", view=None)
        thread = await parent.create_thread(
            name=f"{self.type_name}-{interaction.user.name}"[:90],
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        await thread.add_user(interaction.user)
        ticket_id = tickets.create_ticket(self.bot.db, interaction.user.id, self.type_name, thread.id)
        await thread.send(
            f"Ticket #{ticket_id} ({self.type_name}) opened by {interaction.user.mention}. "
            "Staff will claim it shortly.",
            view=TicketControls(self.bot),
        )
        await notify_staff(self.bot, f"New ticket #{ticket_id} ({self.type_name}): {thread.mention}")
        await interaction.response.edit_message(content=f"Your ticket is open: {thread.mention}", view=None)


class TypeSelect(discord.ui.Select):
    """Dropdown listing enabled ticket categories with their descriptions."""

    def __init__(self, bot: commands.Bot):
        """Store the bot (and, where relevant, the chosen category) for the callbacks."""
        self.bot = bot
        options = [
            discord.SelectOption(label=t["name"][:100], description=t["description"][:100])
            for t in tickets.enabled_types(bot.db)
        ] or [discord.SelectOption(label="No categories configured")]
        super().__init__(placeholder="Choose a ticket type...", options=options, custom_id="da:ticket:select")

    async def callback(self, interaction: discord.Interaction):
        """Step one: show the description and a Submit button (only the chooser sees it)."""
        row = self.bot.db.execute("SELECT * FROM ticket_types WHERE name=? AND enabled=1", (self.values[0],)).fetchone()
        if row is None:
            return await interaction.response.send_message("That category is unavailable.", ephemeral=True)
        await interaction.response.send_message(
            f"**{row['name']}**: {row['description']}\nPress Submit to open the ticket.",
            view=SubmitView(self.bot, row["name"]),
            ephemeral=True,
        )


class TicketPanel(discord.ui.View):
    """Persistent panel posted by leadership (survives bot restarts)."""

    def __init__(self, bot: commands.Bot):
        """Store the bot (and, where relevant, the chosen category) for the callbacks."""
        super().__init__(timeout=None)
        self.add_item(TypeSelect(bot))


class TicketControls(discord.ui.View):
    """Persistent Claim / Close buttons inside each ticket thread."""

    def __init__(self, bot: commands.Bot):
        """Store the bot (and, where relevant, the chosen category) for the callbacks."""
        super().__init__(timeout=None)
        self.bot = bot

    async def _staff_ticket(self, interaction: discord.Interaction):
        """Return the open ticket row if the clicker is staff, else tell them why not."""
        ticket = tickets.get_by_thread(self.bot.db, interaction.channel_id)
        if ticket is None or ticket["status"] != "open":
            await interaction.response.send_message("This ticket is closed.", ephemeral=True)
            return None
        if not is_ticket_staff(self.bot, interaction.user):
            await interaction.response.send_message("Only ticket staff can do that.", ephemeral=True)
            return None
        return ticket

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="da:ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        """One claimant at a time; any staff member may take over."""
        ticket = await self._staff_ticket(interaction)
        if ticket is None:
            return
        try:
            previous = tickets.claim_ticket(self.bot.db, ticket["id"], interaction.user.id)
        except tickets.TicketError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        note = f" (took over from <@{previous}>)" if previous else ""
        await interaction.response.send_message(f"{interaction.user.mention} claimed this ticket{note}.")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="da:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close, archive transcript + attachments locally, try to DM the opener, lock the thread."""
        ticket = await self._staff_ticket(interaction)
        if ticket is None:
            return
        await interaction.response.defer()
        thread = interaction.channel
        tickets.close_ticket(self.bot.db, ticket["id"], interaction.user.id)
        messages = []
        async for m in thread.history(limit=None, oldest_first=True):
            files = []
            for a in m.attachments:
                if a.size <= tickets.MAX_ATTACHMENT_BYTES:
                    files.append((a.filename, await a.read()))
            messages.append(
                {
                    "author_id": m.author.id,
                    "author_name": str(m.author),
                    "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "content": m.content,
                    "attachments": files,
                }
            )
        path = tickets.archive_ticket(self.bot.db, self.bot.settings.archive_dir, ticket["id"], messages)
        delivered = False
        opener = self.bot.get_user(ticket["opener_id"]) or await self.bot.fetch_user(ticket["opener_id"])
        try:
            await opener.send(
                "Your ticket was closed. Transcript attached.",
                file=discord.File(io.BytesIO(path.read_bytes()), filename=f"ticket-{ticket['id']}.txt"),
            )
            delivered = True
        except (discord.Forbidden, discord.HTTPException):
            pass  # DMs may be closed; the archive remains available in the portal
        tickets.set_dm_status(self.bot.db, ticket["id"], delivered)
        await thread.send(
            f"Ticket closed by {interaction.user.mention}. Transcript archived "
            f"(DM {'sent' if delivered else 'failed'})."
        )
        await thread.edit(locked=True, archived=True)


class TicketCog(commands.Cog, name="Tickets"):
    """Leadership command that posts the panel."""

    def __init__(self, bot: commands.Bot):
        """Store the bot (and, where relevant, the chosen category) for the callbacks."""
        self.bot = bot

    @commands.command(name="ticketpanel")
    @leaders_only()
    async def ticketpanel(self, ctx: commands.Context):
        """Post the ticket dropdown in this channel."""
        await ctx.send("Need help? Choose a ticket type below.", view=TicketPanel(self.bot))


async def setup(bot: commands.Bot) -> None:
    """Register the cog and the persistent views so old panels keep working after restarts."""
    await bot.add_cog(TicketCog(bot))
    bot.add_view(TicketPanel(bot))
    bot.add_view(TicketControls(bot))
