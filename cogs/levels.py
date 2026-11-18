"""Crew standings — the nightly level system, judged by the decision model.

After the dream consolidates memories, each ACTIVE member's week is reviewed:
their messages (quality, from the 7-day archive), interactivity (channels,
distinct partners), and likedness (reactions tallied from channel history).

The decision model (microsoft/decision-1) places each member in a tier and
flags "sparks" of the community. Score movement is transparent math on top —
promotions require decision confidence, demotions are gentler. The chat
model writes each promotion's one-line citation. Standings post nightly.
"""

import asyncio
import json
import logging
from pathlib import Path
import math
import time
from collections import Counter, defaultdict
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
MAX_JUDGED_PER_NIGHT = 15          # top-N active members evaluated (cost bound)
PROMOTE_CONFIDENCE = 0.65
DEMOTE_CONFIDENCE = 0.55
STANDINGS_SIZE = 8

ROLE_COLOURS = {                    # nautical gradient: driftwood → gold
    "🪵 Landlubber": 0x9AA0A6,
    "🪝 Deckhand": 0xA0785A,
    "⚓ Sailor": 0x4A90D9,
    "🧭 Navigator": 0x2EC4B6,
    "⛵ Bosun": 0xE8A33D,
    "👑 Admiral": 0xF1C40F,
}


async def ensure_level_roles(guild: discord.Guild) -> dict:
    """Find-or-create the six level roles. Returns {tier_name: role}."""
    roles = {}
    for tier, colour in ROLE_COLOURS.items():
        role = discord.utils.get(guild.roles, name=tier)
        if role is None:
            try:
                role = await guild.create_role(
                    name=tier, colour=discord.Colour(colour),
                    hoist=True, reason="crew standings ladder")
            except discord.Forbidden:
                return {}
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
        current = [r for r in member.roles if r.name in ROLE_COLOURS and r.id != wanted.id]
        if wanted not in member.roles:
            await member.add_roles(wanted, reason="nightly crew standings")
        for old in current:
            await member.remove_roles(old, reason="nightly crew standings")
    except discord.Forbidden:
        log.warning("role sync refused for %s (hierarchy or perms)", uid)


def _tier_index(name: str) -> int:
    try:
        return TIER_NAMES.index(name)
    except ValueError:
        return 0


async def tally_likes(guild: discord.Guild, top_channels: list[discord.TextChannel],
                      per_channel: int = 60) -> dict[int, int]:
    """Reactions received, by author — from recent channel history."""
    tally = Counter()
    for ch in top_channels[:8]:
        try:
            async for m in ch.history(limit=per_channel):
                if m.author.bot or not m.reactions:
                    continue
                for r in m.reactions:
                    tally[m.author.id] += r.count
        except (discord.HTTPException, Exception):
            continue
    return dict(tally)


def _movement(score: float, tier_idx: int, conf: float) -> float:
    """Move score toward the judged tier's band center; promotions need confidence."""
    name, lo, hi = memory.TIERS[tier_idx]
    center = (lo + hi) / 2
    delta = (center - score) * 0.25          # pull 25% toward the judged center
    return max(-6.0, min(8.0, delta))         # bounded nightly movement


class LevelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_run = 0.0

    # ---------- nightly judgment ----------

    async def run_nightly(self, guild: discord.Guild, post: bool = True) -> dict:
        """Called by the dream after consolidation. Judge, move, post."""
        entries = await memory.stm_window(hours=24 * 7)
        if not entries:
            return {"judged": 0}

        activity = Counter(e["user_id"] for e in entries)
        names = {e["user_id"]: e["name"] for e in entries}
        # top active humans (the bot doesn't rank itself)
        me_id = guild.me.id
        active = [(uid, n) for uid, n in activity.most_common(MAX_JUDGED_PER_NIGHT)
                  if uid != me_id]

        # likedness: reactions across the busiest channels
        chans = [c for c in guild.text_channels
                 if c.permissions_for(guild.me).read_messages]
        chans.sort(key=lambda c: -(c.last_message_id or 0))
        likes = await tally_likes(guild, chans)

        try:
            pulse = self.bot.get_cog("ServerPulseCog")
        except Exception:
            pulse = None
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
                f"current standing: {r['tier']} ({r['score']:.0f}/100)\n"
                f"sample of what they actually said:\n" + "\n".join(f"  - {s}" for s in sample)
            )
            questions = {
                "tier": {
                    "type": "choice",
                    "instructions": (
                        "I keep the crew standings of this Discord server. Based on this member's "
                        "week — what they said (quality and spirit), how much they engage, and how "
                        "the community reacts to them — which tier have they EARNED? Judge the "
                        "person, not just the counts. Quiet-but-gold beats loud-but-empty."),
                    "criteria": {t: t for t in TIER_NAMES},
                },
                "spark": {
                    "type": "choice",
                    "instructions": (
                        "Is this member a positive SPARK of the community this week — someone "
                        "whose presence made the server better (helpful, funny, kind, building "
                        "things)? Not just active — sparky."),
                    "criteria": {"yes": "a spark", "no": "just present"},
                },
            }
            try:
                answers = await ai.decide(state[:1800], questions, timeout=25.0)
            except AIError as e:
                log.warning("tier judgment failed for %s: %s", name, e)
                continue
            judged += 1

            tier = answers.get("tier", {})
            chosen = tier.get("choice", r["tier"])
            conf = float(tier.get("confidence", 0))
            spark = answers.get("spark", {}).get("choice") == "yes"

            t_idx = _tier_index(chosen)
            cur_idx = _tier_index(r["tier"])
            delta = _movement(r["score"], t_idx, conf)
            score = max(0.0, min(100.0, r["score"] + delta))

            # promotion/demotion gates: decision confidence required
            new_tier = r["tier"]
            note = ""
            if t_idx > cur_idx and conf >= PROMOTE_CONFIDENCE:
                new_tier = chosen
                note = "promoted"
            elif t_idx < cur_idx and conf >= DEMOTE_CONFIDENCE:
                new_tier = chosen
                note = "stepped down"
            elif spark and conf >= PROMOTE_CONFIDENCE:
                note = "spark"

            reason = ""
            if note or spark:
                try:
                    reason = await ai.chat_for("quick", [
                        {"role": "system", "content": (
                            "You keep the crew standings. In AT MOST 10 words, one dry-witted "
                            "lowercase line: why this member earned their place tonight. No quotes, "
                            "no name, no period at the end unless it's funny")},
                        {"role": "user", "content": state[:800]},
                    ], max_tokens=40, temperature=0.9, timeout=20.0)
                    reason = reason.strip().strip('"').strip(".")
                except (AIError, asyncio.TimeoutError):
                    reason = ""

            r_prev = r["score"]
            prev_tier = r["tier"]
            r.update(score=score, tier=new_tier, last_eval=time.time(),
                     last_reason=reason or r.get("last_reason", ""))
            r["history"].append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "score": round(score), "tier": new_tier,
                "delta": round(score - r_prev), "reason": reason,
            })
            r["history"] = r["history"][-30:]
            await memory.save_rank(uid, r)
            standings.append({
                "uid": uid, "name": name, "tier": new_tier, "score": score,
                "prev_tier": prev_tier, "note": note, "reason": reason,
                "spark": spark, "likes": likes.get(uid, 0), "msgs": msg_count,
            })

        standings.sort(key=lambda s: -s["score"])

        # role-sync: make the ladder physical — wear the role you earned
        if standings:
            tier_roles = await ensure_level_roles(guild)
            if tier_roles:
                for s in standings:
                    await sync_member_tier(guild, s["uid"], s["tier"], tier_roles)

        result = {"judged": judged, "standings": standings}
        if post:
            await self.post_standings(guild, result)
        self._last_run = time.time()
        return result

    # ---------- standings post ----------

    async def post_standings(self, guild: discord.Guild, result: dict):
        cfg = await get_cfg(guild.id)
        chan_id = cfg.get("levels_channel_id") or cfg.get("dreams_channel_id")
        chan = guild.get_channel(chan_id) if chan_id else None
        if chan is None or not chan.permissions_for(guild.me).send_messages:
            return

        standings = sorted(result.get("standings", []), key=lambda s: -s["score"])
        promos = [s for s in standings if s["note"] == "promoted"]
        now = datetime.now(timezone.utc).strftime("%b %d")

        # ---- the living board: one pinned plain-text message, edited nightly ----
        lines = [f"⚓ CREW STANDINGS — {now}", "─" * 34]
        for i, s in enumerate(standings[:12]):
            bar = "▓" * int(s["score"] / 12) + "░" * (8 - int(s["score"] / 12))
            spark = " ✨" if s["spark"] else ""
            lines.append(f"{i+1:>2}. {s['name'][:14]:<14} {s['tier']:<13} {s['score']:>3.0f} {bar}{spark}")
        if not standings:
            lines.append("(the tide hasn't judged anyone yet)")
        lines += ["", f"*judged nightly by the decision model · {result.get('judged', 0)} crew reviewed*"]

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
        for s in promos[:4]:
            moves.append(f"⬆️ **{s['name']}** → {s['tier']}" + (f" — *“{s['reason']}”*" if s["reason"] else ""))
        for s in standings:
            if s["note"] == "stepped down":
                moves.append(f"⬇️ **{s['name']}** → {s['tier']}")
        if moves:
            from cogs.pings import mention_for
            mt = await mention_for(guild, "standings")
            await chan.send((mt["mention"] + "\n" if mt["mention"] else "") + "\n".join(moves),
                            allowed_mentions=mt["allowed"])

    # ---------- commands ----------

    @app_commands.command(name="rank", description="Check your (or someone's) crew standing")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        who = member or interaction.user
        r = await memory.get_rank(who.id)
        e = discord.Embed(
            title=f"⚓ {who.display_name} — {r['tier']}",
            description=f"standing: **{r['score']:.0f}/100**",
            colour=0x2B6D8C)
        if r.get("last_reason"):
            e.add_field(name="tonight's note", value=f"*“{r['last_reason']}”*", inline=False)
        for o in r.get("history", [])[-3:][::-1]:
            e.add_field(name=o["date"],
                        value=f"{o['tier']} ({o['score']}) {o['reason'] and '— “' + o['reason'] + '”'}",
                        inline=False)
        await interaction.response.send_message(embed=e, ephemeral=member is None)

    @app_commands.command(name="leaderboard", description="The crew standings")
    async def leaderboard(self, interaction: discord.Interaction):
        store = await memory.all_ranks()
        ranked = sorted(store.items(), key=lambda kv: -kv[1].get("score", 0))[:10]
        if not ranked:
            return await interaction.response.send_message("the tide hasn't judged anyone yet ⚓",
                                                           ephemeral=True)
        body = "\n".join(
            f"**{i+1}.** <@{uid}> — {r['tier']} ({r['score']:.0f})"
            for i, (uid, r) in enumerate(ranked))
        await interaction.response.send_message("⚓ **crew standings**\n" + body)


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelsCog(bot))
