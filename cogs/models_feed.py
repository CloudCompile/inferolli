"""Model market watcher: tracks GET /v1/models (catalog + pricing), posts
diffs (new models / price changes / removals) to a configured channel, and
offers /models for on-demand lookups."""

import asyncio
import json
import logging
from pathlib import Path

import discord
import httpx
from discord import app_commands
from discord.ext import commands

import ai
from ai import API_KEY, BASE_URL
from config import get as get_cfg

log = logging.getLogger("cogs.models_feed")

SNAPSHOT = Path(__file__).parent.parent / "data" / "models-snapshot.json"
INTERVAL_S = 6 * 3600


async def fetch_catalog() -> dict[str, dict]:
    """id -> {name, prompt, completion, context} from the P2G catalog."""
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(f"{BASE_URL}/models", headers=headers)
        r.raise_for_status()
        data = r.json()
    out = {}
    for m in data.get("data", []):
        p = m.get("pricing") or {}
        out[m["id"]] = {
            "name": m.get("name") or m["id"],
            "prompt": p.get("prompt"),
            "completion": p.get("completion"),
            "context": m.get("context_length"),
            "type": m.get("type"),
        }
    return out


def _per_m(price: str | float | None) -> str | None:
    if price in (None, "", "0"):
        return None
    try:
        v = float(price) * 1_000_000
    except (TypeError, ValueError):
        return None
    return f"${v:.2f}/M" if v >= 0.01 else f"${v:.4f}/M"


def diff(old: dict, new: dict) -> dict:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    price_changes = []
    for mid in sorted(set(old) & set(new)):
        for field in ("prompt", "completion"):
            a, b = old[mid].get(field), new[mid].get(field)
            if a is not None and b is not None and abs(float(a) - float(b)) > 1e-12:
                price_changes.append((mid, field, a, b))
    return {"added": added, "removed": removed, "price_changes": price_changes}


def _load() -> dict:
    if SNAPSHOT.exists():
        return json.loads(SNAPSHOT.read_text())
    return {}


def _save(catalog: dict) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(catalog, indent=1))
    tmp.replace(SNAPSHOT)


class ModelsFeedCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None

    async def cog_load(self):
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await self.bot.wait_until_ready()
        # small delay so restarts don't stampede the API
        await asyncio.sleep(30)
        # first cycle after boot = SILENT baseline refresh: a fresh sandbox
        # restores the last backup's snapshot, so boot-time "diffs" are restore
        # replays (the same models announced again after every relaunch)
        try:
            await self.check_once(announce=False)
        except Exception:
            log.exception("model feed boot sync failed")
        while True:
            try:
                await self.check_once(announce=True)
            except Exception:
                log.exception("model feed check failed")
            await asyncio.sleep(INTERVAL_S)

    async def check_once(self, announce: bool = True):
        catalog = await fetch_catalog()
        old = _load()

        if not old:
            _save(catalog)
            log.info("model feed: baseline snapshot saved (%d models)", len(catalog))
            return

        d = diff(old, catalog)
        _save(catalog)
        if announce and (d["added"] or d["removed"] or d["price_changes"]):
            await self.post_diff(d)

    async def post_diff(self, d: dict):
        for guild in self.bot.guilds:
            cfg = await get_cfg(guild.id)
            channel_id = cfg.get("models_channel_id")
            board = guild.get_channel(channel_id) if channel_id else None
            if board is None or not board.permissions_for(guild.me).send_messages:
                continue

            embed = discord.Embed(
                title="📡 Model market update",
                description=f"`{BASE_URL}`/models changed · {len(d['added'])} new, "
                            f"{len(d['price_changes'])} repriced, {len(d['removed'])} removed",
                colour=discord.Colour.gold() if d["added"] else discord.Colour.dark_teal(),
            )
            if d["added"]:
                lines = [f"**{mid}** — {catalog_name}" for mid, catalog_name in
                         [(m, _load().get(m, {}).get("name", "")) for m in d["added"]]]
                embed.add_field(name=f"🆕 New models ({len(d['added'])})",
                                value="\n".join(lines)[:1000] or "*—*", inline=False)
            if d["price_changes"]:
                lines = [f"`{mid}` {field}: {_per_m(a) or a} → {_per_m(b) or b}"
                         for mid, field, a, b in d["price_changes"][:15]]
                embed.add_field(name=f"💸 Price changes ({len(d['price_changes'])})",
                                value="\n".join(lines)[:1000], inline=False)
            if d["removed"]:
                embed.add_field(name=f"🗑️ Removed ({len(d['removed'])})",
                                value=", ".join(f"`{m}`" for m in d["removed"])[:1000], inline=False)

            from cogs.pings import mention_for
            mt = await mention_for(board.guild, "models")
            await board.send(content=mt["mention"] or None, embed=embed,
                             allowed_mentions=mt["allowed"])

    # ---- on-demand lookup -------------------------------------------------

    @app_commands.command(name="models", description="Look up models and pricing from the InferencePort catalog")
    @app_commands.describe(search="Filter by name/id substring (shows 10 best matches)")
    async def models_cmd(self, interaction: discord.Interaction, search: str | None = None):
        await interaction.response.defer()
        try:
            catalog = await fetch_catalog()
        except Exception as e:
            await interaction.followup.send(f"Catalog fetch failed: {e}")
            return

        if search:
            s = search.lower()
            ids = [mid for mid in catalog if s in mid.lower() or s in (catalog[mid]["name"] or "").lower()]
            ids.sort(key=lambda mid: (not mid.startswith(s), mid))
        else:
            ids = sorted(catalog)

        if not ids:
            await interaction.followup.send(f"No models match `{search}`.")
            return

        lines = []
        for mid in ids[:10]:
            m = catalog[mid]
            pin, cout = _per_m(m["prompt"]), _per_m(m["completion"])
            price = f"{pin or 'free?'} in · {cout or '—'} out" if pin or cout else "pricing n/a"
            lines.append(f"**{mid}** — {m['name']}\n  {price}" + (f" · ctx {m['context']:,}" if m["context"] else ""))

        more = f"\n*…and {len(ids) - 10} more — narrow with a search term.*" if len(ids) > 10 else ""
        payload = f"**Model catalog** ({len(ids)} match{'es' if len(ids) != 1 else ''}):\n" + "\n".join(lines) + more
        await interaction.followup.send(payload[:2000])


async def setup(bot: commands.Bot):
    await bot.add_cog(ModelsFeedCog(bot))
