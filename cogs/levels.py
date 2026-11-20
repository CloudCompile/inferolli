"""Crew standings — an XP ladder with a nightly AI spark bonus.

The old system was judged nightly by the decision model on a 0-100 "score":
opaque to members, and roles flapped up and down on the model's mood. The
redesign keeps the character but changes the physics:

* XP is earned deterministically — talking (+5, at most once a minute) and
  getting reacted on (+2). Nobody can be judged downward out of a rank.
* Tiers sit at fixed XP thresholds, and the ladder absorbed the stray rank
  roles that had drifted in over the years (Crew, Research Diver, First
  Mate, Captain) so there is exactly one ladder, ten rungs, no duplicates.
* The nightly decision model still reads the week — but all it can do now
  is hand a small SPARK bonus (+25) to members whose presence made the
  server better, with a one-line citation. Demotion is no longer a thing.

Standings post nightly, right after the dream consolidates.
"""

import asyncio
import json
import logging
from pathlib import Path
import time
from collections import Counter
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import ai
from ai import AIError
import memory
from config import get as get_cfg

log = logging.getLogger("cogs.levels")

TIER_NAMES = [t[0] for t in memory.TIERS]
MAX_JUDGED_PER_NIGHT = 15          # top-N active members reviewed (cost bound)
SPARK_CONFIDENCE = 0.6

XP_PER_MESSAGE = 5
XP_PER_REACTION = 2
XP_COOLDOWN_S = 60                 # at most one message award per minute
SPARK_XP = 25                      # the nightly model's bonus for being a spark

# nautical gradient: driftwood → seabed teals → brass → gold
TIER_COLOURS = {
    "🪵 Landlubber": 0x9AA0A6,
    "🪝 Deckhand": 0xA0785A,
    "🚢 Crew": 0xC4A67A,
    "⚓ Sailor": 0x4A90D9,
    "🧭 Navigator": 0x2EC4B6,
    "🔬 Research Diver": 0x1F8A70,
    "⛵ Bosun": 0xE8A33D,
    "🎖️ First Mate": 0xD98E32,
    "🚩 Captain": 0xE74C3C,
    "👑 Admiral": 0xF1C40F,
}

# The strays folded into the ladder. ⚓ and 🧭 were each used twice (Sailor vs
# Captain, Navigator vs First Mate), so the absorbed roles get new emojis
# rather than ladder twins that render identically.
LADDER_RENAMES = {
    "🧭 First Mate": "🎖️ First Mate",
    "⚓ Captain": "🚩 Captain",
}


async def ensure_level_roles(guild: discord.Guild) -> dict:
    """Find-or-create the ladder roles, merging the strays in. Returns
    {tier_name: role}. Also repaints roles that kept an old colour."""
    roles = {}
    for tier, colour in TIER_COLOURS.items():
        hoist = tier in ("🎖️ First Mate", "🚩 Captain", "👑 Admiral")
        role = discord.utils.get(guild.roles, name=tier)
        if role is None:
            old_name = LADDER_RENAMES.get(tier)
            if old_name:
                role = discord.utils.get(guild.roles, name=old_name)
                if role is not None:
                    try:
                        await role.edit(
                            name=tier, colour=discord.Colour(colour),
                            reason="ladder merge: stray rank role folded in")
                        log.info("ladder merge: renamed %r to %r", old_name, tier)
                    except discord.Forbidden:
                        log.warning("ladder merge refused for %r (perms)", old_name)
        if role is None:
            try:
                # only the top of the ladder hoists — ten hoisted roles would
                # shred the member sidebar
                role = await guild.create_role(
                    name=tier, colour=discord.Colour(colour),
                    hoist=tier in ("🎖️ First Mate", "🚩 Captain", "👑 Admiral"),
                    reason="crew standings ladder")
            except discord.Forbidden:
                return {}
        elif role.colour.value != colour:
            try:
                await role.edit(colour=discord.Colour(colour),
                                reason="crew standings ladder repaint")
            except (discord.Forbidden, discord.HTTPException):
                pass
        roles[tier] = role
    return roles


async def sync_member_tier(guild: discord.Guild, uid: int, tier: str, all_roles: dict) -> None:
    """Make sure the member wears exactly their tier's role."""
    member = guild.get_member(uid)
    if member is None or member.bot:
        return
    wanted = all_roles.get(tier)
    if wanted is None:
        return
    try:
        current = [r for r in member.roles
                    if r.name in TIER_COLOURS and r.id != wanted.id]
        if wanted not in member.roles:
            await member.add_roles(wanted, reason="crew standings ladder")
        for old in current:
            await member.remove_roles(old, reason="crew standings ladder")
    except discord.Forbidden:
        log.warning("role sync refused for %s (hierarchy or perms)", uid)


def _progress_bar(xp: float) -> str:
    """▓ toward the next rung, ░ what's left."""
    idx = next((i for i, (name, _) in enumerate(memory.TIERS) if name == memory.tier_for_xp(xp)), 0)
    if idx + 1 >= len(memory.TIERS):
        return "▓" * 8                      # top of the ladder
    lo = memory.TIERS[idx][1]
    hi = memory.TIERS[idx + 1][1]
    frac = max(0.0, min(1.0, (xp - lo) / (hi - lo)))
    filled = int(frac * 8)
    return "▓" * filled + "░" * (8 - filled)


def xp_to_next(xp: float) -> str:
    idx = next((i for i, (name, _) in enumerate(memory.TIERS) if name == memory.tier_for_xp(xp)), 0)
    if idx + 1 >= len(memory.TIERS):
        return "top of the ladder ⚓"
    return f"{memory.TIERS[idx + 1][1] - xp:.0f} XP to {memory.TIERS[idx + 1][0]}"


async def tally_likes(guild: discord.Guild, top_channels: list, per_channel: int = 60) -> dict:
    """Reactions received, by author — from recent channel history."""
    tally = Counter()
    for ch in top_channels[:8]:
        try:
            async for m in ch.history(limit=per_channel):
                if m.author.bot or not m.reactions:
                    continue
                for r in m.reactions:
                    tally[m.author.id] += r.count
        except Exception:
            continue
    return dict(tally)


class LevelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_award = {}       # uid -> monotonic time of last message award

    # ---------- the XP engine ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        now = time.monotonic()
        if now - self._last_award.get(message.author.id, 0.0) < XP_COOLDOWN_S:
            return
        self._last_award[message.author.id] = now
        await self._award(message.guild, message.author.id, XP_PER_MESSAGE)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        msg, author = reaction.message, reaction.message.author
        if (user.bot or author.bot or user.id == author.id
                or getattr(msg, "guild", None) is None):
            return
        # no isinstance(Member) guard here on purpose: the message author
        # may have left the guild (a User, not a Member), but their XP bank
        # doesn't care
        await self._award(msg.guild, author.id, XP_PER_REACTION)

    async def _award(self, guild, uid: int, amount: float) -> str | None:
        """Add XP, persist, and pin the ladder role if the tier changed.
        Returns the new tier if it changed, else None. XP only ever goes UP —
        a negative or zero amount is rejected outright, which is what makes
        demotion structurally impossible."""
        if amount <= 0:
            return None
        r = await memory.get_rank(uid)
        old_tier = r["tier"]
        r["xp"] = r.get("xp", 0) + amount
        r["tier"] = memory.tier_for_xp(r["xp"])
        await memory.save_rank(uid, r)
        if r["tier"] != old_tier:
            tier_roles = await ensure_level_roles(guild)
            if tier_roles:
                await sync_member_tier(guild, uid, r["tier"], tier_roles)
            log.info("crew ladder: uid %s → %s", uid, r["tier"])
        return r["tier"] if r["tier"] != old_tier else None

    # ---------- staff adjustments ----------

    def _is_crew_admin(self, member: discord.Member) -> bool:
        """Admins and the staff role can adjust XP by hand. The nightly
        model can't — only people with a finger on the wheel can move a
        member down, and it's logged in their history as a hand move."""
        import helpers
        if helpers.is_admin(member):
            return True
        return any(r.name.lower() == "staff" for r in getattr(member, "roles", []))

    async def adjust_xp(self, guild: discord.Guild, uid: int, amount: float,
                        by_name: str = "staff") -> dict:
        """Staff-only XP move — the one path that may subtract. Returns
        the updated rank with a `changed` flag for the tier."""
        r = await memory.get_rank(uid)
        old_tier = r["tier"]
        r["xp"] = max(0.0, r.get("xp", 0) + amount)
        r["tier"] = memory.tier_for_xp(r["xp"])
        r["history"].append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "xp": round(r["xp"]), "tier": r["tier"],
            "note": "hand", "reason": f"{by_name}: {'+' if amount >= 0 else ''}{amount:.0f} XP",
        })
        r["history"] = r["history"][-30:]
        await memory.save_rank(uid, r)
        if r["tier"] != old_tier:
            tier_roles = await ensure_level_roles(guild)
            if tier_roles:
                await sync_member_tier(guild, uid, r["tier"], tier_roles)
            log.info("crew ladder: staff moved uid %s %+.0f XP → %s",
                     uid, amount, r["tier"])
        return r

    @app_commands.command(name="xp",
                          description="Add or remove crew XP by hand (admin/staff)")
    @app_commands.describe(member="Whose XP", amount="XP to add — negative to remove")
    async def xp(self, interaction: discord.Interaction,
                 member: discord.Member,
                 amount: app_commands.Range[int, -100000, 100000]):
        if not self._is_crew_admin(interaction.user):
            return await interaction.response.send_message(
                "that wheel is for admins and staff only", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        if member.bot:
            return await interaction.followup.send("bots don't sail the ladder", ephemeral=True)
        r = await self.adjust_xp(interaction.guild, member.id, amount,
                                 by_name=interaction.user.display_name)
        arrow = "⬆️" if r["tier"] != memory.tier_for_xp(
            r["xp"] - amount) else ("⬇️" if amount < 0 else "•")
        await interaction.followup.send(
            f"{arrow} **{member.display_name}** is now **{r['tier']}** · "
            f"{r['xp']:.0f} XP · {xp_to_next(r['xp'])}", ephemeral=True)

    # ---------- nightly spark pass ----------

    async def run_nightly(self, guild: discord.Guild, post: bool = True) -> dict:
        """Called by the dream after consolidation. Sparks only — the model
        can hand a bonus; it can never demote."""
        entries = await memory.stm_window(hours=24 * 7)
        if not entries:
            return {"judged": 0}

        activity = Counter(e["user_id"] for e in entries)
        names = {e["user_id"]: e["name"] for e in entries}
        me_id = guild.me.id
        active = [(uid, n) for uid, n in activity.most_common(MAX_JUDGED_PER_NIGHT)
                  if uid != me_id]

        chans = [c for c in guild.text_channels
                 if c.permissions_for(guild.me).read_messages]
        chans.sort(key=lambda c: -(c.last_message_id or 0))
        likes = await tally_likes(guild, chans)

        standings = []
        judged = 0
        for uid, msg_count in active:
            name = names.get(uid, f"member-{uid}")
            r = await memory.get_rank(uid)
            sample = [e["text"][:90] for e in entries if e["user_id"] == uid][-5:]
            chans_they = sorted({e["channel"] for e in entries if e["user_id"] == uid})

            state = (
                f"member: {name}\n"
                f"messages last 7d: {msg_count} across {len(chans_they)} channels ({', '.join(chans_they[:5])})\n"
                f"reactions received (recent): {likes.get(uid, 0)}\n"
                f"current rank: {r['tier']} ({r['xp']:.0f} XP)\n"
                f"sample of what they actually said:\n" + "\n".join(f"  - {s}" for s in sample)
            )
            questions = {
                "spark": {
                    "type": "choice",
                    "instructions": (
                        "Is this member a positive SPARK of this community this week — someone "
                        "whose presence made the server better (helpful, funny, kind, building "
                        "things)? Not just active — sparky."),
                    "criteria": {"yes": "a spark", "no": "just present"},
                },
            }
            try:
                answers = await ai.decide(state[:1800], questions, timeout=25.0)
            except AIError as e:
                log.warning("spark judgment failed for %s: %s", name, e)
                continue
            judged += 1

            spark = (answers.get("spark", {}).get("choice") == "yes"
                     and float(answers.get("spark", {}).get("confidence", 0)) >= SPARK_CONFIDENCE)
            prev_tier = r["tier"]
            note, reason = "", ""

            if spark:
                note = "spark"
                try:
                    reason = await ai.chat_for("quick", [
                        {"role": "system", "content": (
                            "You keep the crew standings. In AT MOST 10 words, one dry-witted "
                            "lowercase line: what made this member a spark this week. No quotes, "
                            "no name, no period at the end unless it's funny")},
                        {"role": "user", "content": state[:800]},
                    ], max_tokens=40, temperature=0.9, timeout=20.0)
                    reason = reason.strip().strip('"').strip(".")
                except (AIError, asyncio.TimeoutError):
                    reason = ""
                r["xp"] = r.get("xp", 0) + SPARK_XP
                r["tier"] = memory.tier_for_xp(r["xp"])
                r["last_eval"] = time.time()
                r["last_reason"] = reason or r.get("last_reason", "")
                r["history"].append({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "xp": round(r["xp"]), "tier": r["tier"],
                    "note": "spark", "reason": reason,
                })
                r["history"] = r["history"][-30:]
                await memory.save_rank(uid, r)

            standings.append({
                "uid": uid, "name": name, "tier": r["tier"], "xp": r["xp"],
                "prev_tier": prev_tier, "note": note, "reason": reason,
                "spark": spark, "likes": likes.get(uid, 0), "msgs": msg_count,
            })

        standings.sort(key=lambda s: -s["xp"])

        # role-sync: make the ladder physical — wear the rank you earned
        if standings:
            tier_roles = await ensure_level_roles(guild)
            if tier_roles:
                for s in standings:
                    await sync_member_tier(guild, s["uid"], s["tier"], tier_roles)

        result = {"judged": judged, "standings": standings}
        if post:
            await self.post_standings(guild, result)
        return result

    # ---------- standings post ----------

    async def post_standings(self, guild: discord.Guild, result: dict):
        cfg = await get_cfg(guild.id)
        chan_id = cfg.get("levels_channel_id") or cfg.get("dreams_channel_id")
        chan = guild.get_channel(chan_id) if chan_id else None
        if chan is None or not chan.permissions_for(guild.me).send_messages:
            return

        standings = sorted(result.get("standings", []), key=lambda s: -s["xp"])
        now = datetime.now(timezone.utc).strftime("%b %d")

        # ---- the living board: one pinned plain-text message, edited nightly ----
        lines = [f"⚓ CREW STANDINGS — {now}", "─" * 34]
        for i, s in enumerate(standings[:12]):
            bar = _progress_bar(s["xp"])
            spark = " ✨" if s["spark"] else ""
            lines.append(f"{i+1:>2}. {s['name'][:14]:<14} {s['tier']:<18} "
                         f"{s['xp']:>6.0f} XP {bar}{spark}")
        if not standings:
            lines.append("(the tide hasn't judged anyone yet)")
        lines += ["", f"*ranks are earned by talking and being reacted on · the nightly "
                      f"model hands ✨ spark bonuses · {result.get('judged', 0)} crew reviewed*"]

        # the board's id lives in the BACKED-UP data dir — in cogs/data/ it
        # survived in-process but not migration, so every migration pinned a
        # brand-new board (duplicates). memory.MEM_DIR is /bot/data/memory.
        board_state = memory.MEM_DIR / "standings.json"
        board_id = None
        try:
            board_id = json.loads(board_state.read_text()).get(str(guild.id), {}).get("board_id")
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        board_msg = None
        if board_id:
            try:
                board_msg = await chan.fetch_message(board_id)
            except (discord.NotFound, discord.HTTPException):
                board_msg = None

        if board_msg:
            await board_msg.edit(content="\n".join(lines))
        else:
            board_msg = await chan.send("\n".join(lines))
            try:
                await board_msg.pin()
            except discord.HTTPException:
                pass
            board_state.parent.mkdir(parents=True, exist_ok=True)
            state = {}
            try:
                state = json.loads(board_state.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            state.setdefault(str(guild.id), {})["board_id"] = board_msg.id
            board_state.write_text(json.dumps(state, indent=1))

        # ---- tonight's moves: small plain-text message under the board ----
        moves = []
        for s in standings:
            if s["note"] == "spark":
                moves.append(f"✨ **{s['name']}** +{SPARK_XP} XP"
                             + (f" — *“{s['reason']}”*" if s["reason"] else ""))
            elif s["tier"] != s["prev_tier"]:
                moves.append(f"⬆️ **{s['name']}** → {s['tier']}")
        if moves:
            from cogs.pings import mention_for
            mt = await mention_for(guild, "standings")
            await chan.send((mt["mention"] + "\n" if mt["mention"] else "") + "\n".join(moves),
                            allowed_mentions=mt["allowed"])

    # ---------- commands ----------

    @app_commands.command(name="rank", description="Check your (or someone's) crew rank")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        who = member or interaction.user
        r = await memory.get_rank(who.id)
        e = discord.Embed(
            title=f"⚓ {who.display_name} — {r['tier']}",
            description=f"**{r['xp']:.0f} XP** · {xp_to_next(r['xp'])}\n{_progress_bar(r['xp'])}",
            colour=0x2B6D8C)
        if r.get("last_reason"):
            e.add_field(name="tonight's note", value=f"*“{r['last_reason']}”*", inline=False)
        for o in r.get("history", [])[-3:][::-1]:
            e.add_field(name=o["date"],
                        value=f"{o['tier']} ({o.get('xp', '?')} XP)"
                              + (f" — *“{o['reason']}”*" if o.get("reason") else ""),
                        inline=False)
        await interaction.response.send_message(embed=e, ephemeral=member is None)

    @app_commands.command(name="leaderboard", description="The crew standings")
    async def leaderboard(self, interaction: discord.Interaction):
        store = await memory.all_ranks()
        ranked = sorted(store.items(), key=lambda kv: -kv[1].get("xp", 0))[:10]
        if not ranked:
            return await interaction.response.send_message("the tide hasn't judged anyone yet ⚓",
                                                           ephemeral=True)
        body = "\n".join(
            f"**{i+1}.** <@{uid}> — {r['tier']} ({r.get('xp', 0):.0f} XP)"
            for i, (uid, r) in enumerate(ranked))
        await interaction.response.send_message("⚓ **crew standings**\n" + body)


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelsCog(bot))