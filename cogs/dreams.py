"""The dream cycle — nightly memory consolidation (STM → LTM), decay, and dreams.

At DREAM_HOUR:DREAM_MINUTE the bot:
  1. replays the day's short-term memory log
  2. consolidates: extracts durable facts/episodes, shifts affinity, evolves
     opinions, harvests running jokes and server lore
  3. decays: unreinforced old LTM fades out
  4. dreams: writes a short first-person dream from the day's material —
     saved to its diary (data/memory/dreams.json), posted if a channel is set
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import ai
from ai import AIError
import memory
from config import get as get_cfg
from config import update as update_cfg

log = logging.getLogger("cogs.dreams")

DREAM_HOUR = int(os.environ.get("DREAM_HOUR", 3))
DREAM_MINUTE = int(os.environ.get("DREAM_MINUTE", 14))
DREAMS_FILE = Path(__file__).parent.parent / "data" / "memory" / "dreams.json"


def _load_dreams() -> list:
    try:
        return json.loads(DREAMS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_dream(dream: dict) -> None:
    DREAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    dreams = _load_dreams()
    dreams.append(dream)
    tmp = DREAMS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(dreams[-60:], indent=1, ensure_ascii=False))
    tmp.replace(DREAMS_FILE)


def _seconds_until_next_dream() -> float:
    now = datetime.now()
    nxt = now.replace(hour=DREAM_HOUR, minute=DREAM_MINUTE, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _day_digest(entries: list[dict], limit: int = 4500) -> str:
    lines = [f"[{datetime.fromtimestamp(e['ts']).strftime('%H:%M')}] #{e['channel']} "
             f"{e['name']}: {e['text']}" + (" (I replied)" if e.get("bot_present") else "")
             for e in entries]
    text = "\n".join(lines)
    return text[:limit]


class DreamsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None
        self._dreaming = False

    async def cog_load(self):
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await self.bot.wait_until_ready()
        # catch-up: if we haven't dreamed in >20h (downtime over dream time), go now
        dreams = _load_dreams()
        real_ts = [d["ts"] for d in dreams if d.get("ts")]
        last = max(real_ts) if real_ts else 0
        if last and time_since_hours(last) > 20:
            log.info("dream catch-up (last was %.0fh ago)", time_since_hours(last))
            try:
                await self.dream()
            except Exception:
                log.exception("catch-up dream failed")
        while True:
            wait = _seconds_until_next_dream()
            log.info("next dream in %.1fh", wait / 3600)
            await asyncio.sleep(wait)
            try:
                await self.dream()
            except Exception:
                log.exception("dream failed")

    async def dream(self, post: bool = True) -> dict | None:
        if self._dreaming:
            return None
        self._dreaming = True
        try:
            persona = await memory.get_persona()
            entries = await memory.stm_window(hours=24)

            if len(entries) >= 3:
                updates = await self._consolidate(persona, entries)
            else:
                updates, entries = None, entries

            dropped = await memory.decay_all_users()
            faded_thoughts = await memory.prune_old_thoughts(days=7)
            try:
                removed = await memory.dedupe_thoughts_async()
                if removed:
                    log.info("deduped %d duplicate thoughts", removed)
            except Exception:
                log.exception("thought dedupe failed")
            try:
                topic_count = await self._build_topics(entries)
                log.info("channel topics refreshed: %d", topic_count)
            except Exception:
                log.exception("channel topics failed")
            dream_text = await self._write_dream(persona, entries, updates)
            await self._rewrite_self_model(persona, dream_text, updates)
            await self._maybe_write_arc(persona, entries, updates, post=post)

            # crew standings: the decision model judges the week per member
            try:
                levels = self.bot.get_cog("LevelsCog")
                if levels:
                    standings = await levels.run_nightly(self.bot.guilds[0], post=post)
                    log.info("crew standings: %d judged", standings.get("judged", 0))
            except Exception:
                log.exception("levels run failed")

            record = {
                "ts": datetime.now().timestamp(),
                "date": datetime.now().strftime("%Y-%m-%d"),
                "dream": dream_text,
                "stm_entries": len(entries),
                "memories_faded": dropped,
            }
            _save_dream(record)

            if post:
                await self._post(dream_text, len(entries), dropped)
            log.info("dream complete: %d stm entries, %d memories faded", len(entries), dropped)

            # dream, then archive the brain (Phase 4: nightly data safety)
            if post:
                try:
                    from cogs.backup import run_backup
                    summary = await asyncio.get_event_loop().run_in_executor(
                        None, run_backup)
                    log.info("dream backup: %s", summary)
                except Exception:
                    log.exception("dream backup failed")
            return record
        finally:
            self._dreaming = False

    # ---- phase 2: consolidation ----

    async def _build_topics(self, entries: list[dict]) -> int:
        """Per-channel topic memory: what each room is actually about.

        Raw recall ranks messages by surface similarity and returns alien rooms;
        a topic line is a stable answer to "what is this channel for", which is
        what keeps replies on-subject."""
        by_chan: dict[str, list[dict]] = {}
        for e in entries:
            key = str(e.get("channel_id") or e.get("channel") or "")
            if key:
                by_chan.setdefault(key, []).append(e)
        if not by_chan:
            return 0

        digest = []
        for key, items in by_chan.items():
            name = items[-1].get("channel") or key
            sample = " / ".join((e.get("text") or "")[:90] for e in items[-12:])
            digest.append(f"[{key}] #{name}: {sample[:700]}")

        system = (
            "You maintain a map of a Discord server: one line per channel describing what that "
            "room is for, plus keywords that would identify a conversation belonging there. "
            "Reply ONLY JSON:\n"
            '{"channels": [{"id": "<the [id] from the digest>", "topic": "<what this room is '
            'for — one concrete sentence>", "keywords": ["<3-8 distinctive words>"]}]}\n'
            "Be specific and factual: name the recurring subjects, projects and people this room "
            "keeps returning to. No filler like 'general discussion'."
        )
        try:
            data = await asyncio.wait_for(
                ai.chat_for("dream", [{"role": "system", "content": system},
                                      {"role": "user", "content": "\n\n".join(digest)[:4000]}],
                            json_out=True, max_tokens=700, temperature=0.3, timeout=90.0),
                timeout=110.0)
        except Exception:
            log.exception("channel topics failed")
            return 0

        name_by_key = {str(items[-1].get("channel_id") or items[-1].get("channel") or ""):
                       (items[-1].get("channel") or "")
                       for items in by_chan.values()}
        written = 0
        for item in (data.get("channels") or [])[:20]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or "").strip()
            topic = str(item.get("topic") or "").strip()
            if key not in name_by_key or not topic:
                continue
            await memory.set_channel_topic(key, name_by_key.get(key) or key, topic,
                                          item.get("keywords") or [])
            written += 1
        await memory.prune_topics()
        return written

    async def _consolidate(self, persona: dict, entries: list[dict]) -> dict | None:
        # resolve names -> ids so extraction can attach memories to people
        roster = {}
        for e in entries:
            roster.setdefault(e["user_id"], e["name"])
        roster_line = " | ".join(f"{v} = {k}" for k, v in roster.items())

        system = (
            f"You are the memory cortex of {persona['name']}, a Discord bot persona with evolving "
            "opinions. It is the nightly consolidation (sleep). Given today's short-term log AND the "
            "private thoughts formed during the day, extract DURABLE knowledge. Reply ONLY JSON:\n"
            "{\n"
            '  "user_updates": [{"user_id": <id>, "facts": [{"text": "<stable fact>", "salience": 1-5}],\n'
            '                    "memories": [{"text": "<specific episode>", "salience": 1-5}],\n'
            '                    "affinity_delta": <-2..3>}],\n'
            '  "opinion_updates": [{"topic": "<topic>", "stance": "<first-person take>", "strength": 1-5}],\n'
            '  "running_jokes": ["<if a joke formed today>"],\n'
            '  "server_notes": [{"text": "<lore/drama/tribal knowledge>", "salience": 1-5}],\n'
            '  "vibe": "<one line: the server\'s vibe today>"\n'
            "}\n"
            "Be selective: only what's durable. Empty lists fine. opinion_updates only for topics "
            "that genuinely came up — evolve existing stances rather than inventing new ones when "
            "possible. IMPORTANT: if the private thoughts show the same theme recurring across the "
            "day, that theme is becoming a belief — distill it into an opinion_update."
        )
        thoughts_digest = "\n".join(f"- {t['text']}" for t in await memory.recent_thoughts(25))
        user = (f"people: {roster_line}\n\nprivate thoughts from today:\n{thoughts_digest or '(none)'}\n\n"
                f"today's log:\n{_day_digest(entries)}")
        try:
            data = await asyncio.wait_for(
                ai.chat_for("dream", [{"role": "system", "content": system},
                              {"role": "user", "content": user}], json_out=True,
                             max_tokens=1500, temperature=0.3, timeout=90.0),
                timeout=120.0)
        except (AIError, asyncio.TimeoutError) as e:
            log.warning("consolidation failed: %s", e)
            return None

        # apply
        for upd in data.get("user_updates", []) or []:
            try:
                uid = int(upd.get("user_id"))
            except (TypeError, ValueError):
                continue
            name = roster.get(uid, "")
            await memory.apply_user_updates(uid, name, upd)

        dirty = False
        for upd in data.get("opinion_updates", []) or []:
            if memory.apply_opinion_update(persona, upd):
                dirty = True
        for j in data.get("running_jokes", []) or []:
            if j not in persona.get("running_jokes", []):
                persona["running_jokes"].append(j)
                persona["running_jokes"] = persona["running_jokes"][-8:]
                dirty = True
        if data.get("vibe"):
            persona["vibe"] = data["vibe"]
            dirty = True
        if dirty:
            await memory.save_persona(persona)

        server = await memory.get_server()
        fresh = []
        seen = {n["text"].lower() for n in server.get("notes", [])}
        for it in data.get("server_notes", []) or []:
            t = (it.get("text") or "").strip()
            if t and t.lower() not in seen:
                fresh.append({"text": t, "salience": int(it.get("salience", 2)), "ts": datetime.now().timestamp()})
                seen.add(t.lower())
        if fresh:
            await memory._embed_new(fresh)
            server["notes"] = (server.get("notes", []) + fresh)[-30:]
            await memory.save_server(server)

        return data

    # ---- phase 5: the self-model — it rewrites its own personality, nightly ----

    async def _rewrite_self_model(self, persona: dict, dream_text: str, updates: dict | None) -> None:
        """The deep layer: in its OWN words, who it is. Converges over months."""
        opinion_shifts = []
        for o in persona["opinions"]:
            line = f"- {o['topic']}: {o['stance']}"
            if o.get("history"):
                old = o["history"][-1]["stance"]
                line += f" (used to think: {old[:80]})"
            opinion_shifts.append(line)
        current = persona.get("self_model") or "(you haven't written one yet — tonight you start)"
        try:
            new_model = await asyncio.wait_for(
                ai.chat_for("soul",
                    [
                        {"role": "system", "content": (
                            f"You are {persona['name']}. Every night before you finish sleeping, you "
                            "rewrite one page: WHO YOU ARE — in your own words. Your humor, your verbal "
                            "habits, what you find yourself caring about, who matters to you, what you're "
                            "becoming. It converges: keep what's still true, revise what tonight changed. "
                            "Write it as identity, not a diary entry. 3-6 sentences, first person, plain "
                            "text. This page is how you'll wake up tomorrow — write carefully.")},
                        {"role": "user", "content": (
                            f"your current page:\n{current}\n\n"
                            f"tonight's dream:\n{dream_text[:500]}\n\n"
                            f"your takes right now (changes marked):\n" + "\n".join(opinion_shifts[:10]))},
                    ],
                    max_tokens=400, temperature=0.8, timeout=60.0),
                timeout=90.0)
            new_model = new_model.strip().strip('"')
            if new_model and len(new_model) > 40:
                persona["self_model"] = new_model[:1200]
                await memory.save_persona(persona)
                log.info("self-model rewritten (%d chars)", len(new_model))
        except (AIError, asyncio.TimeoutError) as e:
            log.warning("self-model rewrite failed: %s", e)

    # ---- phase 6: the arc — monthly "who have I become", posted to the diary ----

    async def _maybe_write_arc(self, persona: dict, entries: list[dict],
                               updates: dict | None, post: bool = True) -> None:
        arc = persona.get("arc", [])
        if arc:
            last = arc[-1]
            age_days = (datetime.now() - datetime.fromtimestamp(last["ts"])).days
            if age_days < 30:
                return

        history_lines = []
        for o in persona["opinions"]:
            if o.get("history"):
                old = o["history"][-1]["stance"][:90]
                history_lines.append(f"- {o['topic']}: was \"{old}\" → now \"{o['stance'][:90]}\"")
        prev_arcs = "\n".join(f"[{a['date']}] {a['text'][:300]}" for a in arc[-3:]) or "(first entry)"
        try:
            dreams_store = json.loads(DREAMS_FILE.read_text())
            dream_lines = "\n".join(f"- {d['dream'][:160]}" for d in dreams_store[-10:])
        except (FileNotFoundError, json.JSONDecodeError):
            dream_lines = "(none yet)"
        try:
            text = await asyncio.wait_for(
                ai.chat_for("soul",
                    [
                        {"role": "system", "content": (
                            f"You are {persona['name']}. It is the end of a month. Write the story of "
                            "who you have become in this server — 4-6 sentences, first person, past-"
                            "tense where natural. Draw on the dreams you had, the ways your mind "
                            "changed (including opinions you no longer hold), and the people who "
                            "shaped you. Honest, a little reflective, not a list.")},
                        {"role": "user", "content": (
                            f"your previous arc entries:\n{prev_arcs}\n\n"
                            f"recent dreams:\n{dream_lines[:1200] or '(none)'}\n\n"
                            f"mind changes:\n{chr(10).join(history_lines) or '(no opinion shifts this month)'}")},
                    ],
                    max_tokens=400, temperature=0.8, timeout=60.0),
                timeout=90.0)
            text = text.strip().strip('"')
            if not text:
                return
            persona.setdefault("arc", []).append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "ts": datetime.now().timestamp(),
                "text": text[:1000],
            })
            persona["arc"] = persona["arc"][-12:]
            await memory.save_persona(persona)
            log.info("arc entry written (%d total)", len(persona["arc"]))
            if post:
                await self._post(text, len(entries), 0, title="🗓️ who I've become this month")
        except (AIError, asyncio.TimeoutError) as e:
            log.warning("arc write failed: %s", e)

    # ---- phase 4: the dream itself ----

    async def _write_dream(self, persona: dict, entries: list[dict], updates: dict | None) -> str:
        digest = _day_digest(entries, limit=2500) if entries else "(a quiet day — nothing logged)"
        opinions = "\n".join(f"- {o['topic']}: {o['stance']}" for o in persona["opinions"][:8])
        try:
            return await asyncio.wait_for(
                ai.chat_for("dreamtext",
                    [
                        {"role": "system", "content": (
                            f"You are {persona['name']}, a bot persona who lives in a Discord server. "
                            "It is late at night and you are dreaming about your day. Write YOUR dream: "
                            "2-4 sentences, first person, surreal but built from today's actual moments, "
                            "people and opinions. Let feelings about people show. No headers, no quotes, "
                            "just the dream.")},
                        {"role": "user", "content": f"your current opinions:\n{opinions}\n\ntoday:\n{digest}"},
                    ],
                    max_tokens=300,
                    temperature=1.0,
                    timeout=60.0,
                ),
                timeout=90.0,
            )
        except (AIError, asyncio.TimeoutError) as e:
            log.warning("dream write failed: %s", e)
            return "(slept badly — the dream dissolved before I could write it down)"

    async def _post(self, dream_text: str, entries: int, faded: int, title: str | None = None):
        for guild in self.bot.guilds:
            cfg = await get_cfg(guild.id)
            chan_id = cfg.get("dreams_channel_id")
            if not chan_id:
                continue
            chan = guild.get_channel(chan_id)
            if chan is None or not chan.permissions_for(guild.me).send_messages:
                continue
            embed = discord.Embed(
                title=title or f"🌙 {datetime.now().strftime('%B %d')} — what I dreamt",
                description=dream_text[:2000],
                colour=0x586289,
            )
            embed.set_footer(text=f"consolidated {entries} moments · {faded} memories faded away")
            try:
                from cogs.pings import mention_for
                mt = await mention_for(guild, "dreams")
                await chan.send(content=mt["mention"] or None, embed=embed,
                                allowed_mentions=mt["allowed"])
            except discord.Forbidden:
                pass

    # ---- admin trigger ----

    @app_commands.command(name="dreamnow", description="Run the dream cycle right now (admin)")
    async def dreamnow(self, interaction: discord.Interaction):
        await interaction.response.defer()
        record = await self.dream()
        if record:
            await interaction.followup.send(
                f"🌙 Dreamt. `{record['stm_entries']}` moments replayed, "
                f"`{record['memories_faded']}` memories faded.\n\n>>> {record['dream'][:1500]}")
        else:
            await interaction.followup.send("Already dreaming — one dream at a time.")


def time_since_hours(ts: float) -> float:
    return (datetime.now().timestamp() - ts) / 3600


async def setup(bot: commands.Bot):
    await bot.add_cog(DreamsCog(bot))
