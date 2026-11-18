"""Proposal flow — the bot proposes governance actions; admins approve.

Anyone can ask (in chat or via /propose) for a server change they don't have
the keys for: the bot starts a proposal — an embed with ✅ / ❌. An admin
reacting ✅ executes it (the approving admin is the accountable actor);
❌ rejects. Proposals expire after 24h. This is how Inferolli manages the
server with initiative while humans keep the keys.
"""

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import abilities
import permissions
from config import get as get_cfg

log = logging.getLogger("cogs.proposals")

PROPOSAL_TTL = 24 * 3600
CHECK_EMOJI = "✅"
REJECT_EMOJI = "❌"


class ProposalsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._open: dict[int, dict] = {}   # message_id -> {action, proposer, expires, title}
        self._expiry: dict[int, asyncio.Task] = {}  # message_id -> expiry task

    # ---------- creation ----------

    async def create(self, guild: discord.Guild, action: dict, proposer: discord.Member,
                     channel: discord.TextChannel | None = None) -> discord.Message | None:
        """Start a proposal; returns the vote message (or None if unsupported)."""
        atype = (action.get("type") or "").strip()
        if atype not in ("create_channel", "create_category", "create_role",
                         "archive_channel", "timeout_member", "set_topic"):
            return None
        title = {
            "create_channel": "open a new channel",
            "create_category": "open a new category",
            "create_role": "create a role",
            "archive_channel": "archive a channel",
            "timeout_member": "mute a member",
            "set_topic": "set a channel topic",
        }.get(atype, atype)
        detail = ", ".join(f"{k}: {v}" for k, v in action.items()
                           if k not in ("type", "_message") and v)

        target = channel or self._proposal_channel(guild)
        if target is None:
            return None
        e = discord.Embed(
            title=f"🗳️ proposal — {title}",
            description=(f"proposed by {proposer.mention}\n{detail}\n\n"
                         "admins: ✅ to approve · ❌ to reject · expires in 24h"),
            colour=0x8C6D2B)
        msg = await target.send(embed=e)
        try:
            await msg.add_reaction(CHECK_EMOJI)
            await msg.add_reaction(REJECT_EMOJI)
        except discord.HTTPException:
            pass
        self._open[msg.id] = {"action": action, "proposer": proposer.id,
                              "expires": time.time() + PROPOSAL_TTL, "title": title}
        # hold the task so it can be cancelled on unload instead of leaking
        task = asyncio.get_event_loop().create_task(self._expire_later(msg.id))
        self._expiry[msg.id] = task
        return msg

    async def cog_unload(self):
        for task in self._expiry.values():
            task.cancel()
        self._expiry.clear()

    def _proposal_channel(self, guild: discord.Guild):
        # bot-commands is admin-only (fits the approver audience); fall back to system channel
        ch = discord.utils.get(guild.text_channels, name="🤖│bot-commands")
        return ch or guild.system_channel

    async def _expire_later(self, msg_id: int):
        try:
            await asyncio.sleep(PROPOSAL_TTL)
        except asyncio.CancelledError:
            return
        finally:
            self._expiry.pop(msg_id, None)
        p = self._open.pop(msg_id, None)
        if p:  # expired un-voted
            log.info("proposal %s expired", msg_id)

    # ---------- voting ----------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.message_id not in self._open or payload.member is None:
            return
        if payload.emoji.name not in (CHECK_EMOJI, REJECT_EMOJI):
            return
        prop = self._open[payload.message_id]
        if time.time() > prop["expires"]:
            self._open.pop(payload.message_id, None)
            return
        member = payload.member
        if member.bot:
            return
        # Two independent checks, both required:
        #   1. the proposer can never approve their own proposal, and
        #   2. the approver must hold the tier THIS action requires — a staff
        #      member with manage_channels must not approve an admin-tier action
        #      just because they can see the vote message.
        if member.id == prop.get("proposer"):
            log.info("proposal %s: proposer cannot approve their own proposal",
                     payload.message_id)
            return
        action_key = abilities.ACTION_KEYS.get((prop["action"].get("type") or "").strip())
        if action_key is None:
            self._open.pop(payload.message_id, None)
            return
        if not permissions.allows(member, action_key):
            log.info("proposal %s: %s lacks the %s tier for %s",
                     payload.message_id, member.display_name,
                     permissions.required_tier(action_key), action_key)
            return

        channel = self.bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        self._open.pop(payload.message_id, None)

        if payload.emoji.name == REJECT_EMOJI:
            e = msg.embeds[0]
            e.title = f"🗳️ rejected — {prop['title']}"
            e.colour = 0x8C2B39
            e.set_footer(text=f"rejected by {member.display_name}")
            await msg.edit(embed=e)
            return

        # approved → execute with the approving admin as accountable actor
        result = await abilities.do_action(prop["action"], member.guild, member)
        e = msg.embeds[0]
        e.title = f"🗳️ {'approved' if 'couldn' not in result else 'failed'} — {prop['title']}"
        e.description += f"\n\n**result:** {result}"
        e.colour = 0x2B8C4F if "couldn" not in result else 0x8C2B39
        e.set_footer(text=f"approved by {member.display_name}")
        await msg.edit(embed=e)
        log.info("proposal approved: %s → %s", prop["title"], result)

    # ---------- command ----------

    @app_commands.command(name="propose", description="Propose a server change — admins approve it (✅/❌)")
    @app_commands.describe(kind="what to do", name="channel/role name", detail="extra info (topic, colour, member to mute...)")
    @app_commands.choices(kind=[
        app_commands.Choice(name="open a channel", value="create_channel"),
        app_commands.Choice(name="create a role", value="create_role"),
        app_commands.Choice(name="archive a channel", value="archive_channel"),
        app_commands.Choice(name="mute a member", value="timeout_member"),
    ])
    async def propose(self, interaction: discord.Interaction, kind: app_commands.Choice[str],
                      name: str, detail: str | None = None):
        action = {"type": kind.value, "name": name}
        if kind.value == "timeout_member" and detail:
            action["user"] = detail
        if detail and kind.value in ("create_channel", "set_topic"):
            action["topic"] = detail
        msg = await self.create(interaction.guild, action, interaction.user,
                                channel=interaction.channel)
        if msg is None:
            return await interaction.response.send_message(
                "that proposal type isn't supported here 🤷", ephemeral=True)
        await interaction.response.send_message(
            f"proposal started in {msg.channel.mention} — needs an admin ✅", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProposalsCog(bot))
