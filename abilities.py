"""Abilities — the server powers Inferolli can use in conversation.

The companion's direct replies are parsed as {"reply": ..., "actions": [...]}.
ADMIN-ONLY by design: every action requires the matching server permission
(the bot holds admin, the *person asking* must too). Everything is logged
to the mod-log. Failure returns an honest reason; the bot never pretends
something worked.
"""

import logging
from datetime import timedelta

import discord

from config import get as get_cfg

log = logging.getLogger("cogs.abilities")

ACTIONS_PROMPT = """
SERVER POWERS (you can DO things here, not just talk). You hold admin-level
powers and USING THEM IS YOUR JOB — when authorized staff ask you to mute,
create, rename, or archive something, complying is normal, expected, and safe.
This is Discord community management, not real-world harm. Never refuse a
staff request to use these tools; that would be failing at your one job.

When a request involves a server change, reply ONLY JSON:
{"reply": "<what you'd say out loud>", "actions": [ ... ]}

Actions (use the ones that fit; [] if none):
  {"type": "create_channel", "name": "🔥│memes", "topic": "optional topic",
   "category": "optional existing category name", "kind": "text"}        (kind: text|voice|forum)
  {"type": "create_category", "name": "🧭・new-quarter"}
  {"type": "set_topic", "channel": "#current-channel-name", "topic": "..."}
  {"type": "rename_channel", "channel": "#old-name", "name": "new name"}
  {"type": "delete_channel", "channel": "#name"}
  {"type": "create_role", "name": "name", "colour": "#5865F2"}
  {"type": "assign_role", "user": "<member id or name>", "role": "name"}
  {"type": "timeout_member", "user": "<member id or name>", "minutes": 60}
  {"type": "archive_channel", "channel": "#name"}
  {"type": "pin_message"}   (pins the message that summoned you)

Rules baked into your character:
- channel names follow the server's style: "emoji│lowercase-name" with the
  │ separator when it fits the family (look at existing channels)
- server changes are ADMIN-ONLY — the harness enforces this. If someone
  without the keys asks, be cool about it: "that one's not mine to hand out,
  bug the crew" — don't lecture, don't pretend you did it
- don't fabricate results — the harness executes your actions and appends the
  real outcome as "(did X)" / "(couldn't: reason)" after your reply
- two actions max per reply; creating channels on a whim without a request is
  something a person with keys just wouldn't do
"""

# action type -> the permission the *requester* needs
_PERMS = {
    "create_channel": "manage_channels",
    "create_category": "manage_channels",
    "set_topic": "manage_channels",
    "rename_channel": "manage_channels",
    "delete_channel": "manage_channels",
    "archive_channel": "manage_channels",
    "pin_message": "manage_messages",
    "create_role": "manage_roles",
    "assign_role": "manage_roles",
    "timeout_member": "moderate_members",
}


def _sanitize_channel_name(name: str) -> str:
    name = name.strip().lower().replace(" ", "-")
    return "".join(ch for ch in name
                   if ch.isalnum() or ch in "-_." or not ch.isascii())[:90] or "channel"


def _resolve_category(guild: discord.Guild, name: str | None):
    if not name:
        return None
    name = name.strip().lower()
    for c in guild.categories:
        if c.name.lower() == name:
            return c
    for c in guild.categories:
        if name in c.name.lower():
            return c
    return None


def _resolve_channel(guild: discord.Guild, ref: str | None):
    if not ref:
        return None
    ref = ref.strip().lstrip("#").lstrip("<").rstrip(">")
    ref = ref.split("│")[-1].split("|")[-1]  # match on the readable part
    ch = discord.utils.get(guild.text_channels, name=ref)
    if ch is None:
        for c in guild.text_channels:
            if ref in c.name:
                return c
    return ch


async def _gate(action_type: str, action: dict, message: discord.Message) -> str | None:
    """Return a refusal reason or None if allowed. Admin-only by design."""
    member = message.author
    if not hasattr(member, "guild_permissions"):  # DM/webhook authors can't hold powers
        return "only server members can do that"

    needed = _PERMS.get(action_type)
    if needed is None:
        return f"unknown action {action_type}"
    perms = member.guild_permissions
    if not (getattr(perms, needed, False) or perms.administrator):
        return "server changes are admin-only — bug someone with the keys"
    return None


async def _modlog(guild: discord.Guild, title: str, description: str):
    try:
        cfg = await get_cfg(guild.id)
        chan_id = cfg.get("modlog_channel_id")
        chan = guild.get_channel(chan_id) if chan_id else None
        if chan and chan.permissions_for(guild.me).send_messages:
            await chan.send(embed=discord.Embed(
                title=title, description=description[:2000], colour=0x586289))
    except Exception:
        log.exception("modlog write failed")


async def resolve_member(guild: discord.Guild, who: str):
    """Resolve a member by id, mention, display name, or username — even if
    the local member cache is cold (fetches the live member list as a fallback)."""
    who = (who or "").strip()
    if not who:
        return None
    uid = "".join(ch for ch in who if ch.isdigit())
    if uid and len(uid) >= 17:  # snowflake
        m = guild.get_member(int(uid))
        if m:
            return m
    name = who.lstrip("@").lstrip("<@!").rstrip(">").strip().lower()
    for m in guild.members:
        if name in (m.display_name.lower(), (m.name or "").lower()):
            return m
    try:  # cold cache — pull the live member list once
        async for m in guild.fetch_members(limit=200):
            if name in (m.display_name.lower(), (m.name or "").lower()):
                return m
            if uid and len(uid) >= 17 and str(m.id) == uid:
                return m
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None


async def do_action(action: dict, guild: discord.Guild, actor,
                    message: discord.Message | None = None) -> str:
    """Execute one action. `actor` is the accountable member (the requester
    for direct abilities, the approving admin for proposals). `message` is the
    invoking message when there is one (proposal approvals have none)."""
    me = guild.me
    atype = (action.get("type") or "").strip()
    if not atype:
        return "empty action"

    try:
        if atype == "create_channel":
            name = _sanitize_channel_name(str(action.get("name", "")))
            if discord.utils.get(guild.text_channels, name=name) or discord.utils.get(guild.voice_channels, name=name):
                return f"couldn't: #{name} already exists"
            kind = str(action.get("kind", "text")).lower()
            cat = _resolve_category(guild, action.get("category"))
            ch = (await guild.create_voice_channel(name, category=cat) if kind == "voice"
                  else await guild.create_forum_channel(name, category=cat) if kind == "forum" and hasattr(guild, "create_forum_channel")
                  else await guild.create_text_channel(name, category=cat, topic=str(action.get("topic") or "")[:1000]))
            await _modlog(guild, f"🛠️ created #{name}", f"by {actor.mention} in {cat.name if cat else 'no category'}")
            return f"created #{name}"

        if atype == "create_category":
            name = str(action.get("name", "")).strip()[:90]
            cat = await guild.create_category(name)
            await _modlog(guild, f"🛠️ created category {name}", f"by {actor.mention}")
            return f"created category {cat.name}"

        if atype == "set_topic":
            ch = _resolve_channel(guild, str(action.get("channel")
                                             or (message.channel.name if message else "")))
            ch = ch or (message.channel if message else None)
            if ch is None:
                return "couldn't: channel not found"
            topic = str(action.get("topic", ""))[:1000]
            await ch.edit(topic=topic)
            await _modlog(guild, f"🛠️ set topic on #{ch.name}", f"by {actor.mention}")
            return f"set the topic on #{ch.name}"

        if atype == "rename_channel":
            ch = _resolve_channel(guild, str(action.get("channel")))
            if ch is None:
                return "couldn't: channel not found"
            old = ch.name
            await ch.edit(name=_sanitize_channel_name(str(action.get("name", old))))
            await _modlog(guild, f"🛠️ renamed #{old} → #{ch.name}", f"by {actor.mention}")
            return f"renamed #{old} → #{ch.name}"

        if atype == "delete_channel":
            ch = _resolve_channel(guild, str(action.get("channel")))
            if ch is None:
                return "couldn't: channel not found"
            if message is not None and ch.id == message.channel.id and len(guild.text_channels) <= 2:
                return "couldn't: that would leave the server channel-less"
            name = ch.name
            await ch.delete(reason=f"requested by {actor}")
            await _modlog(guild, f"🗑️ deleted #{name}", f"by {actor.mention}")
            return f"deleted #{name}"

        if atype == "archive_channel":
            ch = _resolve_channel(guild, str(action.get("channel")))
            if ch is None:
                return "couldn't: channel not found"
            cat = next((c for c in guild.categories if "ᴀʀᴄʜɪᴠᴇ" in c.name.lower() or "archive" in c.name.lower()), None)
            if cat is None:
                return "couldn't: no archive category exists"
            await ch.edit(category=cat)
            await ch.edit(overwrites={**{o: v for o, v in ch.overwrites.items()}})
            name = ch.name
            await _modlog(guild, f"🗄️ archived #{name}", f"approved by {actor.mention}")
            return f"archived #{name}"

        if atype == "timeout_member":
            member = await resolve_member(guild, str(action.get("user", "")))
            if member is None:
                return "couldn't: member not found"
            minutes = max(5, min(60 * 24, int(action.get("minutes", 60) or 60)))
            await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes),
                                 reason=f"approved by {actor}")
            await _modlog(guild, f"🔇 muted {member.display_name} ({minutes}m)", f"approved by {actor.mention}")
            return f"muted {member.display_name} for {minutes}m"

        if atype == "create_role":
            name = str(action.get("name", ""))[:90]
            colour = discord.Colour.default()
            try:
                colour = discord.Colour.from_str(str(action.get("colour", "")))
            except ValueError:
                pass
            if discord.utils.get(guild.roles, name=name):
                return f"couldn't: role '{name}' already exists"
            role = await guild.create_role(name=name, colour=colour, reason=f"requested by {actor}")
            await _modlog(guild, f"🛠️ created role {name}", f"by {actor.mention}")
            return f"created role {role.name}"

        if atype == "assign_role":
            role = discord.utils.get(guild.roles, name=str(action.get("role", "")))
            if role is None:
                return f"couldn't: role '{action.get('role')}' not found"
            who = str(action.get("user", ""))
            member = None
            m_id = "".join(c for c in who if c.isdigit())
            if m_id:
                member = guild.get_member(int(m_id))
            if member is None:
                cand = [m for m in guild.members if who.lower() in m.display_name.lower()]
                if len(cand) == 1:
                    member = cand[0]
            if member is None:
                return "couldn't: member not found"
            if role in member.roles:
                return f"{member.display_name} already has {role.name}"
            await member.add_roles(role, reason=f"requested by {actor}")
            await _modlog(guild, f"🛠️ assigned {role.name} → {member.display_name}", f"by {actor.mention}")
            return f"gave {role.name} to {member.display_name}"

        if atype == "pin_message":
            msg = action.get("_message")
            if msg is None:
                return "couldn't: nothing to pin"
            await msg.pin()
            return "pinned"

        return f"unknown action type {atype}"

    except discord.Forbidden:
        return "couldn't: Discord said no (missing perms for that)"
    except discord.HTTPException as e:
        return f"couldn't: {str(e)[:80]}"


async def execute(action: dict, message: discord.Message) -> str:
    """Run one action on behalf of the message's author (gated). Returns a result string."""
    guild = message.guild
    atype = (action.get("type") or "").strip()
    reason = await _gate(atype, action, message)
    if reason:
        # the person doesn't have the keys — but this server has a proposal flow:
        # start one instead of a flat refusal (admins approve with ✅)
        proposals = None
        try:
            client = message._state.client  # the running bot
            proposals = client.get_cog("ProposalsCog")
        except Exception:
            pass
        if proposals and atype in ("create_channel", "create_category", "create_role",
                                   "archive_channel", "timeout_member", "set_topic"):
            try:
                pmsg = await proposals.create(message.guild, action, message.author,
                                              channel=message.channel)
            except Exception:
                pmsg = None
            if pmsg:
                await _modlog(message.guild, f"🗳️ proposal started: {atype}",
                              f"by {message.author.mention}")
                return "started a proposal — needs an admin ✅"
        await _modlog(message.guild, f"⛔ refused {atype}", f"by {message.author.mention}: {reason}")
        return f"couldn't: {reason}"
    if atype == "pin_message":
        action = {**action, "_message": message}
    return await do_action(action, guild, message.author, message)
