"""Music cog — Inferolli as the server's DJ, powered by tonearm.

tonearm drives the user's own logged-in Apple Music account and exposes it as a
REST API plus live audio streams. The studio runs INSIDE this sandbox
(start.sh launches it), so /play never dials a stale box.

  /play <song|album|playlist|station>  search and play — autocomplete covers
                                       catalog songs/albums/playlists/stations
                                       AND the playlists already in the library
  /search <query>                      browse results by kind, pick one to play
  /station [query]                     your own stations (CJ's Station,
                                       Discovery Station…) plus Apple's catalog
  /playlist list|show|play|new|add|remove|rename|delete
                                       the bot CREATES and EDITS its own
                                       playlists only. Playlists that were
                                       already in the library are read-only to
                                       it BY DESIGN — it can play and search
                                       them, never change them.
  /np /skip /pause /resume /queue /volume /party

While a voice party is active, track changes are posted to the
🎧│listening-post channel (auto-created by admins via abilities).
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import httpx

log = logging.getLogger("cogs.music")

URL = os.environ.get("TONEARM_URL", "").rstrip("/")
KEY = os.environ.get("TONEARM_API_KEY", "")
LISTENING_POST = "listening-post"

COLOUR = 0xFA243C  # apple music red

# playlist create drives Apple's UI automation — the server documents up to
# ~2 minutes before it returns. Never time out early: a timed-out client can't
# see the id of a playlist the server goes on to create.
CREATE_TIMEOUT = 200.0
WRITE_TIMEOUT = 90.0

# what the bot made (and may therefore edit). Apple is the source of truth for
# the playlists themselves; this file is the ownership ledger.
STATE_FILE = Path(__file__).parent.parent / "data" / "music.json"

LIB_CACHE_TTL = 60.0
STATION_CACHE_TTL = 300.0

EMOJI = {"songs": "🎵", "albums": "💿", "playlists": "📃",
         "stations": "📻", "artists": "🎤"}
KIND_WORD = {"songs": "song", "albums": "album", "playlists": "playlist",
             "stations": "station", "artists": "artist"}

PLAYABLE = {"songs", "song", "albums", "album", "playlists", "playlist",
            "stations", "station", "artists", "library-playlists",
            "library-albums", "library-songs"}


class StudioError(RuntimeError):
    """tonearm refused — the message is safe to show a person."""


def _configured() -> bool:
    return bool(URL and KEY)


def _headers() -> dict:
    return {"Authorization": f"Bearer {KEY}"}


def _friendly(status: int, body) -> str:
    """tonearm's error envelope → one line a person can act on."""
    try:
        d = json.loads(body) if isinstance(body, (str, bytes)) else body
    except Exception:
        d = None
    if isinstance(d, dict):
        err = d.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            if code == "writes_disabled":
                return ("the studio has writes switched off, so it can't change "
                        "playlists right now")
            msg = err.get("message") or err.get("hint") or code
            if msg:
                return str(msg)[:180]
        if d.get("detail"):
            return str(d["detail"])[:180]
    txt = body.decode() if isinstance(body, bytes) else str(body)
    return f"HTTP {status}: {txt[:160]}"


async def _request(method: str, path: str, *, params: dict | None = None,
                   body=None, timeout: float = 20.0) -> dict:
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, f"{URL}/api{path}", headers=_headers(),
                            params=clean or None, json=body)
    if r.status_code >= 400:
        raise StudioError(_friendly(r.status_code, r.text))
    if not r.content:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


async def _get(path: str, **params) -> dict:
    return await _request("GET", path, params=params)


async def _post(path: str, **params) -> dict:
    return await _request("POST", path, params=params)


# ---------------------------------------------------------------- ownership

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def owned() -> dict:
    """{playlist_id: {"name", "by", "at"}} — playlists the bot created."""
    return _load_state().get("playlists", {}) or {}


def is_owned(playlist_id: str) -> bool:
    return bool(playlist_id) and playlist_id in owned()


def mark_owned(playlist_id: str, name: str, by) -> None:
    st = _load_state()
    st.setdefault("playlists", {})[playlist_id] = {
        "name": name, "by": str(by), "at": int(time.time())}
    _save_state(st)


def unmark_owned(playlist_id: str) -> None:
    st = _load_state()
    (st.get("playlists") or {}).pop(playlist_id, None)
    _save_state(st)


def not_owned_message(name: str) -> str:
    return (f"**{name}** is one of yours, not mine — I only edit playlists I "
            f"created, so I can never wreck one you built. I can play it, "
            f"search it, and add it to a queue; if you want changes, make a "
            f"new one with `/playlist new`.")


async def _refusal(pid: str, fallback: str) -> str:
    """Say WHICH playlist was refused. The id in a slash command is friendly
    to the bot and meaningless to a person, so look the name up when we can."""
    name = owned().get(pid, {}).get("name")
    if not name:
        try:
            name = next((r["name"] for r in await _library_playlists()
                         if r["id"] == pid), None)
        except Exception:
            name = None
    if not name:
        cleaned = (fallback or "").split(":", 1)[-1]
        name = cleaned if cleaned and not cleaned.startswith(("p.", "pl.")) else "that playlist"
    return not_owned_message(name)


# ---------------------------------------------------------------- rows

def _row(kind: str, r: dict) -> dict:
    """Normalize any tonearm row (already flat, but tolerate raw Apple)."""
    r = r or {}
    attrs = r.get("attributes") or {}
    art = r.get("artwork") or attrs.get("artwork")
    url = art.get("url") if isinstance(art, dict) else art
    name = r.get("name") or attrs.get("name") or r.get("title") or "?"
    artist = r.get("artist") or attrs.get("artistName") or ""
    return {
        "kind": kind,
        "id": r.get("id") or r.get("catalog_id") or "",
        "library_id": r.get("library_id") or r.get("id") or "",
        "catalog_id": r.get("catalog_id") or "",
        "name": name,
        "artist": artist,
        "artwork": url,
        "duration_ms": r.get("duration_ms") or attrs.get("durationInMillis"),
    }


def _label(row: dict) -> str:
    label = f"{EMOJI.get(row['kind'], '•')} {row['name']}"
    if row.get("artist"):
        label += f" — {row['artist']}"
    return label[:100]


def _choices(rows: list[dict], limit: int = 25) -> list[app_commands.Choice[str]]:
    out = []
    for row in rows[:limit]:
        label = _label(row) + (" ·mine" if is_owned(row["id"]) else "")
        out.append(app_commands.Choice(
            name=label[:100], value=f"{row['kind']}:{row['id']}"[:100]))
    return out


def _decode(value: str):
    """Autocomplete values are '<kind>:<id>'; free text has no known kind."""
    kind, sep, rid = (value or "").partition(":")
    if sep and rid and kind in PLAYABLE:
        return kind, rid
    return None, None


# ---------------------------------------------------------------- lookups

async def _catalog_search(term: str, types: str, limit: int = 5) -> list[dict]:
    res = await _get("/catalog/search", term=term, types=types, limit=limit)
    rows = []
    for kind, items in (res or {}).items():
        for item in items or []:
            rows.append(_row(kind, item))
    return rows


_library_cache = {"at": 0.0, "rows": []}


async def _library_playlists(force: bool = False) -> list[dict]:
    now = time.monotonic()
    if not force and _library_cache["rows"] and now - _library_cache["at"] < LIB_CACHE_TTL:
        return _library_cache["rows"]
    # Apple pages this 100 at a time and the account already has more than
    # that — stopping at page one made brand-new playlists invisible (the
    # library list is alphabetical, so a fresh "Inferolli mix" sorted off the
    # end). Walk the pages; 1000 is a sanity ceiling, not a limit anyone hits.
    rows, offset = [], 0
    for _ in range(10):
        res = await _get("/library/playlists", limit=100, offset=offset)
        batch = res.get("items") or []
        rows.extend(_row("playlists", r) for r in batch)
        if len(batch) < 100:
            break
        offset += 100
    # Apple keeps nameless tombstones around right after a delete and the
    # index carries a few managed shells — they are not playlists anyone can
    # open, so don't offer them.
    rows = [r for r in rows if (r.get("name") or "").strip()]
    _library_cache.update(at=now, rows=rows)
    return rows


def _forget_library_cache() -> None:
    _library_cache.update(at=0.0, rows=[])


_station_cache = {"at": 0.0, "rows": []}


async def _station_list(force: bool = False) -> list[dict]:
    """Stations Apple builds for THIS account.

    They exist only in the personal surfaces — "CJ's Station" is not in the
    catalog and no search returns it. Recommendations + recently-played both
    carry `ra.` items; union them, keep the first name we see.
    """
    now = time.monotonic()
    if not force and _station_cache["rows"] and now - _station_cache["at"] < STATION_CACHE_TTL:
        return _station_cache["rows"]
    raw = []
    try:
        rec = await _get("/personal/recommendations")
        for group in rec.get("groups") or []:
            raw.extend(group.get("items") or [])
    except Exception:
        log.debug("station list: recommendations unavailable", exc_info=True)
    try:
        played = await _get("/personal/recently-played", limit=30)
        raw.extend(played.get("items") or [])
    except Exception:
        log.debug("station list: recently-played unavailable", exc_info=True)
    rows, seen = [], set()
    for item in raw:
        rid = str((item or {}).get("id") or "")
        if not rid.startswith("ra.") or rid in seen:
            continue
        seen.add(rid)
        rows.append(_row("stations", item))
    _station_cache.update(at=now, rows=rows)
    return rows


async def _playlist_tracks(playlist_id: str) -> list[dict]:
    res = await _get(f"/library/playlists/{playlist_id}/tracks")
    return res.get("tracks") or []


async def _song_ids(kind: str, rid: str) -> list[str]:
    """Catalog song ids for a song, or every track of an album / playlist."""
    if kind in ("songs", "song", "library-songs"):
        return [rid]
    if kind in ("albums", "album", "library-albums"):
        path = (f"/library/albums/{rid}/tracks" if rid.startswith("l.")
                else f"/catalog/albums/{rid}/tracks")
    elif kind in ("playlists", "playlist", "library-playlists"):
        path = (f"/library/playlists/{rid}/tracks" if rid.startswith("p.")
                else f"/catalog/playlists/{rid}/tracks")
    else:
        raise StudioError(f"can't pull tracks out of a {kind}")
    res = await _get(path)
    rows = res.get("tracks") or res.get("items") or []
    ids = [(r.get("catalog_id") or r.get("id")) for r in rows]
    return [str(i) for i in ids if i]


# ---------------------------------------------------------------- playback

async def _queue_playlist_tracks(playlist_id: str, limit: int = 40) -> dict:
    """Play a library playlist by building the queue one track at a time.

    MusicKit refuses a whole-library-playlist setQueue on some builds
    (`mk-140 CONTENT_UNSUPPORTED`) even when every track plays fine alone, and
    one unavailable track poisons the whole list. Playing the first song and
    appending the rest skips the duds instead of failing the playlist.
    """
    tracks = await _playlist_tracks(playlist_id)
    if not tracks:
        raise StudioError("that playlist is empty")
    first = tracks[0]
    await _post(f"/playback/play-song/{first.get('catalog_id') or first.get('id')}")
    queued = skipped = 0
    for track in tracks[1:limit]:
        sid = track.get("catalog_id") or track.get("id")
        if not sid:
            skipped += 1
            continue
        try:
            await _post("/playback/play-later", catalog_id=sid)
            queued += 1
        except StudioError:
            skipped += 1  # unavailable in this storefront — keep going
    return {"mode": "playlist", "queued": queued, "skipped": skipped,
            "total": len(tracks)}


async def _play_artist(name: str, limit: int = 12) -> dict:
    rows = await _catalog_search(name, "songs", limit=limit)
    if not rows:
        raise StudioError(f"nothing by {name}")
    await _post(f"/playback/play-song/{rows[0]['id']}")
    queued = 0
    for row in rows[1:]:
        try:
            await _post("/playback/play-later", catalog_id=row["id"])
            queued += 1
        except StudioError:
            continue
    return {"mode": "artist", "queued": queued, "total": len(rows)}


def _tokens(text: str) -> list[str]:
    import re
    return [t for t in re.split(r"[^0-9a-z]+", (text or "").lower()) if len(t) > 1]


def _soft_filter(rows: list[dict], query: str) -> list[dict]:
    """Every query word appears somewhere in the row's name/artist.

    People say "CJ's Station" for "CJ Hauser's Station" and "phonk playlist"
    for "ACTUALLY GOOD PHONK" — a raw substring test misses both, and the
    apostrophe alone would sink it.
    """
    wanted = _tokens(query)
    if not wanted:
        return list(rows)
    out = []
    for row in rows:
        have = set(_tokens(f"{row.get('name') or ''} {row.get('artist') or ''}"))
        if all(tok in have for tok in wanted):
            out.append(row)
    return out


async def _resolve_text(query: str):
    """Free text → the most likely thing to play.

    Song first (that's what /play always meant) unless the text says
    "station"/"radio" — then stations win, and the account's OWN stations come
    before Apple's, since `/play cj's station` means CJ Hauser's Station and
    catalog search has never heard of it.
    """
    wants_station = bool(set(_tokens(query)) & {"station", "radio"})
    order = (["own", "stations", "songs", "playlists", "albums"] if wants_station
             else ["songs", "own", "stations", "playlists", "albums"])
    for step in order:
        if step == "own":
            try:
                own = _soft_filter(await _station_list(), query)
            except Exception:
                own = []
            if own:
                return "stations", own[0]["id"], own[0]["name"]
            continue
        try:
            rows = await _catalog_search(query, step, limit=3)
        except Exception:
            rows = []
        if rows:
            return step, rows[0]["id"], rows[0]["name"]
    return None


async def _start(kind: str, rid: str, label: str = "") -> dict:
    """Start playback for one normalized row."""
    if kind in ("songs", "song", "library-songs"):
        await _post(f"/playback/play-song/{rid}")
        return {"mode": "song"}
    if kind in ("artists",):
        return await _play_artist(label or rid)
    collection = {"albums": "album", "album": "album", "library-albums": "album",
                  "playlists": "playlist", "playlist": "playlist",
                  "library-playlists": "playlist",
                  "stations": "station", "station": "station"}.get(kind)
    if not collection:
        raise StudioError(f"i don't know how to play a {kind}")
    try:
        await _post("/playback/play-collection", kind=collection, id=rid)
        return {"mode": collection}
    except StudioError:
        if collection != "playlist":
            raise
        # library playlists: whole-list setQueue is the fragile path → rebuild
        return await _queue_playlist_tracks(rid)


# ---------------------------------------------------------------- embeds

def _np_embed(np: dict) -> discord.Embed:
    e = discord.Embed(
        title=np.get("title") or "nothing playing",
        description=f"**{np.get('artist') or '—'}** — {np.get('album') or '—'}",
        colour=COLOUR)
    if np.get("artworkUrl"):
        e.set_thumbnail(url=np["artworkUrl"])
    dur, el = np.get("durationMs"), np.get("elapsedMs")
    if dur and el is not None:
        pos = f"{int(el)//60000}:{int(el)//1000 % 60:02d}"
        full = f"{int(dur)//60000}:{int(dur)//1000 % 60:02d}"
        bar = "▬" * max(1, int(20 * el / dur))
        e.add_field(name="playhead", value=f"`{bar} {'◉' if np.get('isPlaying') else '❙❙'} ▬` `{pos} / {full}`")
    return e


def _results_embed(title: str, rows: list[dict], note: str = "") -> discord.Embed:
    e = discord.Embed(title=title[:250], colour=COLOUR)
    lines = []
    for i, row in enumerate(rows[:25], 1):
        artist = f" — {row['artist']}" if row.get("artist") else ""
        mine = " ·mine" if is_owned(row["id"]) else ""
        lines.append(f"`{i:>2}.` {EMOJI.get(row['kind'], '•')} **{row['name']}**{artist}{mine}")
    e.description = "\n".join(lines) or "*nothing found*"
    if note:
        e.set_footer(text=note)
    return e


def _tracks_embed(playlist: dict, tracks: list[dict], page: int,
                  per_page: int = 12) -> discord.Embed:
    e = discord.Embed(title=f"📃 {playlist.get('name') or 'playlist'}"[:250],
                      colour=COLOUR)
    if playlist.get("artwork"):
        e.set_thumbnail(url=playlist["artwork"])
    start = page * per_page
    lines = [f"`{i}.` **{t.get('name')}** — {t.get('artist') or ''}"
             for i, t in enumerate(tracks[start:start + per_page], start + 1)]
    e.description = "\n".join(lines) or "*empty — add something with `/playlist add`*"
    pages = max(1, (len(tracks) + per_page - 1) // per_page)
    owner = "mine to edit" if is_owned(playlist["id"]) else "yours — read-only for me"
    e.set_footer(text=f"{len(tracks)} tracks · page {page + 1}/{pages} · {owner}")
    return e


# ---------------------------------------------------------------- views

class Picker(discord.ui.View):
    """One select menu — pick a row, hand it to `on_pick`."""

    def __init__(self, rows: list[dict], on_pick, *, placeholder: str = "pick one"):
        super().__init__(timeout=300)
        self.rows = rows
        self._on_pick = on_pick
        options = []
        for i, row in enumerate(rows[:25]):
            desc = row.get("artist") or ("mine — editable" if is_owned(row["id"]) else "")
            options.append(discord.SelectOption(
                label=_label(row), value=str(i),
                description=desc[:100] if desc else None,
                emoji=EMOJI.get(row["kind"])))
        select = discord.ui.Select(placeholder=placeholder[:150], options=options)
        select.callback = self._picked
        self.select = select
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction):
        row = self.rows[int(self.select.values[0])]
        await self._on_pick(interaction, row)


class TrackView(discord.ui.View):
    """A playlist's tracks, paged, with a play-all button."""

    PER_PAGE = 12

    def __init__(self, cog, playlist: dict, tracks: list[dict], page: int = 0):
        super().__init__(timeout=300)
        self.cog = cog
        self.playlist = playlist
        self.tracks = tracks
        self.page = page
        self._sync()

    def _sync(self):
        self.clear_items()
        play = discord.ui.Button(style=discord.ButtonStyle.success, label="play all")
        play.callback = self._play_all
        self.add_item(play)
        if self.page > 0:
            prev = discord.ui.Button(style=discord.ButtonStyle.secondary, label="◀ back")
            prev.callback = self._prev
            self.add_item(prev)
        if (self.page + 1) * self.PER_PAGE < len(self.tracks):
            nxt = discord.ui.Button(style=discord.ButtonStyle.secondary, label="more ▶")
            nxt.callback = self._next
            self.add_item(nxt)

    async def _play_all(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            embed = await self.cog._run_play(
                interaction, "playlists", self.playlist["id"], self.playlist.get("name", ""))
        except Exception as exc:
            return await interaction.followup.send(
                f"couldn't play it — {str(exc)[:150]}", ephemeral=True)
        await interaction.edit_original_response(embed=embed, view=None)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._sync()
        await interaction.response.edit_message(
            embed=_tracks_embed(self.playlist, self.tracks, self.page), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(
            embed=_tracks_embed(self.playlist, self.tracks, self.page), view=self)


class Confirm(discord.ui.View):
    def __init__(self, on_answer):
        super().__init__(timeout=60)
        self._on_answer = on_answer
        yes = discord.ui.Button(style=discord.ButtonStyle.danger, label="delete it")
        no = discord.ui.Button(style=discord.ButtonStyle.secondary, label="keep it")
        yes.callback = self._yes
        no.callback = self._no
        self.add_item(yes)
        self.add_item(no)

    async def _yes(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await self._on_answer(interaction, True)

    async def _no(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="kept it 👍", view=self)


# ---------------------------------------------------------------- autocomplete

def _filter(rows: list[dict], current: str) -> list[dict]:
    term = (current or "").strip().lower()
    if not term:
        return rows
    return [r for r in rows if term in (r.get("name") or "").lower()
            or term in (r.get("artist") or "").lower()]


AC_TIMEOUT = 2.4  # Discord gives autocomplete 3s — leave headroom


async def _quiet(coro):
    try:
        return await coro
    except Exception:
        return []


async def _within(coros, timeout: float | None = None):
    """Run lookups concurrently and keep whatever finished in time.

    Autocomplete is killed at ~3s; a slow studio must degrade the menu, not
    erase it — so a slow option never costs the fast ones their results.
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    done, pending = await asyncio.wait(tasks, timeout=timeout or AC_TIMEOUT)
    for task in pending:
        task.cancel()
    rows = []
    for task in done:
        if task.cancelled():
            continue
        try:
            rows.extend(task.result() or [])
        except Exception:
            continue
    return rows


async def _ac_play(interaction: discord.Interaction, current: str):
    if not _configured():
        return []
    term = (current or "").strip()
    if not term:
        return []

    async def catalog_rows():
        return await _catalog_search(
            term, "songs,albums,playlists,stations", limit=4)

    async def station_rows():
        # the account's own stations never appear in catalog search
        return _soft_filter(await _station_list(), term)[:3]

    async def library_rows():
        return _filter(await _library_playlists(), term)[:4]

    rows = await _within([_quiet(catalog_rows()), _quiet(station_rows()),
                          _quiet(library_rows())])
    return _choices(rows)


async def _ac_library_playlist(interaction: discord.Interaction, current: str):
    if not _configured():
        return []
    try:
        return _choices(_filter(await _library_playlists(), current))
    except Exception:
        return []


async def _ac_owned_playlist(interaction: discord.Interaction, current: str):
    """Only playlists the bot created can be edited, so only those autocomplete.

    Read straight from the ownership ledger: no studio round-trip, so this
    answers inside Discord's deadline even when Apple is slow.
    """
    if not _configured():
        return []
    term = (current or "").strip().lower()
    rows = []
    for pid, meta in owned().items():
        name = meta.get("name") or pid
        if term and term not in name.lower():
            continue
        rows.append({"kind": "playlists", "id": pid, "name": name, "artist": ""})
    # newest first — the one you just made is the one you want
    rows.sort(key=lambda r: owned().get(r["id"], {}).get("at", 0), reverse=True)
    return _choices(rows)


async def _ac_add_source(interaction: discord.Interaction, current: str):
    """Songs / albums / playlists you could add to one of the bot's playlists."""
    if not _configured():
        return []
    term = (current or "").strip()
    if not term:
        return []

    async def catalog_rows():
        return await _catalog_search(term, "songs,albums,playlists", limit=4)

    async def library_rows():
        return _filter(await _library_playlists(), term)[:3]

    rows = await _within([_quiet(catalog_rows()), _quiet(library_rows())])
    return _choices(rows)


# ---------------------------------------------------------------- cog

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._np_task = None
        self._last_track = None

    # ---------- voice ----------

    async def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        """Join (or move to) the requester's voice channel and start the live stream."""
        user = getattr(interaction, "user", None)
        if not user or not getattr(user, "voice", None) or not user.voice.channel:
            return None
        ch = user.voice.channel
        vc = interaction.guild.voice_client
        perms = ch.permissions_for(ch.guild.me)
        if vc is None or not vc.is_connected():
            if not (perms.connect and perms.speak):
                return None
            vc = await ch.connect(self_deaf=True)
        elif vc.channel != ch:
            await vc.move_to(ch)
        if not vc.is_playing():
            # preflight the studio's audio pump. NB: the stream endpoint has
            # no HEAD support (405), so the old HEAD probe always failed and
            # /play never joined a voice channel. /api/audio/status is the
            # honest check — it reports whether the pump is running at all.
            try:
                st = await _get("/audio/status")
            except Exception:
                return None  # studio isn't reachable/streaming
            if not st.get("running"):
                return None  # no live session yet (browser not playing)
            src = discord.FFmpegPCMAudio(
                f"{URL}/api/audio/stream.mp3?api_key={KEY}",
                before_options=["-loglevel", "error"],
                options="-vn")
            vc.play(discord.PCMVolumeTransformer(src, volume=0.8))
        return vc

    async def _np_watcher(self, guild: discord.Guild):
        """While a voice party is live, post track changes to the listening post."""
        await self.bot.wait_until_ready()
        post = discord.utils.get(guild.text_channels, name=LISTENING_POST)
        while guild.voice_client and guild.voice_client.is_connected():
            try:
                np = await _get("/playback/now-playing")
                track = f"{np.get('title')}|{np.get('artist')}"
                if np.get("title") and track != self._last_track:
                    self._last_track = track
                    if post and post.permissions_for(guild.me).send_messages:
                        await post.send(embed=_np_embed(np))
            except Exception:
                log.debug("np watcher hiccup", exc_info=True)
            await discord.utils.sleep(20)

    # ---------- shared play plumbing ----------

    async def _run_play(self, interaction: discord.Interaction, kind: str,
                        rid: str, label: str = "") -> discord.Embed:
        """Start playback, join voice if the asker is in a channel, describe it."""
        summary = await _start(kind, rid, label)
        await asyncio.sleep(1.2)  # let the player commit the new queue
        np = {}
        try:
            np = await _get("/playback/now-playing")
        except Exception:
            log.debug("now-playing unavailable right after play", exc_info=True)
        if not np.get("title"):
            np = {"title": label or "queued", "artist": "", "isPlaying": True}
        vc = None
        try:
            vc = await self._ensure_voice(interaction)
        except Exception:
            log.warning("voice join failed", exc_info=True)
        bits = [KIND_WORD.get(kind, kind)]
        if summary.get("skipped"):
            bits.append(f"{summary['skipped']} unavailable track(s) skipped")
        if summary.get("queued"):
            bits.append(f"{summary['queued']} queued")
        if vc:
            bits.append(f"streaming into {vc.channel.mention}")
        elif summary.get("mode") == "song":
            bits.append("on the studio player")
        embed = _np_embed(np)
        embed.set_footer(text=" · ".join(bits))
        if vc and interaction.guild and (self._np_task is None or self._np_task.done()):
            self._np_task = self.bot.loop.create_task(self._np_watcher(interaction.guild))
        return embed

    async def _pick_and_play(self, interaction: discord.Interaction, row: dict):
        """Select-menu callback: play the chosen row, collapse the picker."""
        await interaction.response.defer()
        try:
            embed = await self._run_play(interaction, row["kind"], row["id"], row["name"])
        except Exception as exc:
            return await interaction.followup.send(
                f"couldn't play that — {str(exc)[:150]}", ephemeral=True)
        await interaction.edit_original_response(embed=embed, view=None)

    async def _open_playlist(self, interaction: discord.Interaction, row: dict):
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            tracks = await _playlist_tracks(row["id"])
        except Exception as exc:
            return await interaction.followup.send(
                f"couldn't read that playlist — {str(exc)[:150]}", ephemeral=True)
        await interaction.edit_original_response(
            embed=_tracks_embed(row, tracks, 0),
            view=TrackView(self, row, tracks))

    # ---------- playback commands ----------

    @app_commands.command(
        name="play",
        description="Play a song, album, playlist or station (autocompletes as you type)")
    @app_commands.describe(query="pick from the list, or just type whatever you want to hear")
    @app_commands.autocomplete(query=_ac_play)
    async def play(self, interaction: discord.Interaction, query: str):
        if not _configured():
            return await interaction.response.send_message(
                "the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        kind, rid = _decode(query)
        if kind is None:
            # free text — best song match, then a station/playlist by name
            try:
                found = await _resolve_text(query)
            except Exception as exc:
                return await interaction.followup.send(
                    f"the studio didn't answer — {str(exc)[:120]}")
            if not found:
                return await interaction.followup.send(
                    f"couldn't find anything for “{query}”")
            kind, rid, query = found
        try:
            embed = await self._run_play(interaction, kind, rid, query)
        except Exception as exc:
            return await interaction.followup.send(f"playback refused — {str(exc)[:150]}")
        await interaction.followup.send(
            embed=embed.set_footer(text=f"{embed.footer.text} · for {interaction.user.display_name}"))

    @app_commands.command(name="search",
                          description="Search Apple Music — songs, albums, playlists, stations, artists")
    @app_commands.describe(query="what to look for", kind="narrow it down (default: everything)")
    @app_commands.choices(kind=[
        app_commands.Choice(name="everything", value="everything"),
        app_commands.Choice(name="songs", value="songs"),
        app_commands.Choice(name="albums", value="albums"),
        app_commands.Choice(name="playlists", value="playlists"),
        app_commands.Choice(name="stations", value="stations"),
        app_commands.Choice(name="artists", value="artists"),
    ])
    async def search(self, interaction: discord.Interaction, query: str,
                     kind: app_commands.Choice[str] = None):
        if not _configured():
            return await interaction.response.send_message(
                "the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        want = kind.value if kind else "everything"
        types = ("songs,albums,playlists,stations,artists" if want == "everything" else want)
        try:
            rows = await _catalog_search(query, types, limit=6 if want == "everything" else 10)
        except Exception as exc:
            return await interaction.followup.send(
                f"the studio didn't answer — {str(exc)[:120]}")
        if want in ("everything", "playlists"):
            try:
                lib = (await _get("/library/search", term=query, limit=10)).get("playlists") or []
                known = {r["id"] for r in rows}
                rows += [_row("playlists", r) for r in lib if r.get("id") not in known]
            except Exception:
                log.debug("library playlist search failed", exc_info=True)
        if not rows:
            return await interaction.followup.send(f"nothing for “{query}” — try looser words")
        rows = rows[:25]
        embed = _results_embed(f"🔎 {query}", rows,
                               note="pick one below to play it"
                                    + (" · 📃 mine = a playlist I can edit"
                                       if any(is_owned(r["id"]) for r in rows) else ""))
        await interaction.followup.send(embed=embed, view=Picker(rows, self._pick_and_play))

    @app_commands.command(name="station",
                          description="Play a radio station — yours (CJ's Station, Discovery…) or Apple's")
    @app_commands.describe(query="station to search for; leave empty for your own stations")
    async def station(self, interaction: discord.Interaction, query: str = None):
        if not _configured():
            return await interaction.response.send_message(
                "the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        rows: list[dict] = []
        if query:
            try:
                rows += _soft_filter(await _station_list(), query)[:5]  # your own first
            except Exception:
                pass
            try:
                rows += await _catalog_search(query, "stations", limit=10)
            except Exception as exc:
                if not rows:
                    return await interaction.followup.send(
                        f"the studio didn't answer — {str(exc)[:120]}")
        else:
            try:
                rows = await _station_list(force=True)
            except Exception as exc:
                return await interaction.followup.send(
                    f"couldn't read your stations — {str(exc)[:120]}")
        seen, unique = set(), []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            unique.append(row)
        if not unique:
            return await interaction.followup.send(
                "no stations came back — play something in Apple Music once and try again")
        embed = _results_embed(f"📻 {query or 'your stations'}", unique,
                               note="pick a station to start it")
        await interaction.followup.send(embed=embed, view=Picker(unique, self._pick_and_play))

    @app_commands.command(name="np", description="What's playing right now")
    async def np(self, interaction: discord.Interaction):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        try:
            np = await _get("/playback/now-playing")
        except Exception:
            return await interaction.response.send_message("the studio didn't answer", ephemeral=True)
        await interaction.response.send_message(embed=_np_embed(np))

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        await self._simple_command(interaction, "next", "skipped ⏭")

    @app_commands.command(name="pause", description="Pause the music")
    async def pause(self, interaction: discord.Interaction):
        await self._simple_command(interaction, "pause", "paused ⏸")

    @app_commands.command(name="resume", description="Resume the music")
    async def resume(self, interaction: discord.Interaction):
        await self._simple_command(interaction, "resume", "resumed ▶")

    @app_commands.command(name="queue", description="See what's queued")
    async def queue(self, interaction: discord.Interaction):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        try:
            q = (await _get("/playback/queue")).get("items", [])
        except Exception:
            return await interaction.response.send_message("the studio didn't answer", ephemeral=True)
        if not q:
            return await interaction.response.send_message("queue's empty — play something", ephemeral=True)
        lines = [f"**{i+1}.** {it.get('title', '?')} — {it.get('artistName', '?')}"
                 for i, it in enumerate(q[:10])]
        await interaction.response.send_message("**up next:**\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="volume", description="Set the studio volume (0-100)")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        try:
            await _post(f"/playback/volume/{level / 100:.2f}")
        except Exception as e:
            return await interaction.response.send_message(f"volume refused: {str(e)[:80]}", ephemeral=True)
        await interaction.response.send_message(f"volume → {level}%", ephemeral=True)

    @app_commands.command(name="party", description="Start a listening party in your voice channel")
    async def party(self, interaction: discord.Interaction):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc is None:
            has_voice_ch = bool(interaction.user.voice and interaction.user.voice.channel)
            return await interaction.followup.send(
                "🎧 I couldn't start the stream — either the studio has no live session yet "
                "(the Apple player needs to be logged in and playing), or I can't reach that "
                "voice channel." if has_voice_ch else
                "join a voice channel first and I'll bring the music 🎧")
        self._np_task = self.bot.loop.create_task(self._np_watcher(interaction.guild))
        from cogs.pings import mention_for
        mt = await mention_for(interaction.guild, "music")
        await interaction.followup.send(
            (mt["mention"] + " " if mt["mention"] else "") +
            "🎧 **listening party started** — playing the studio's live "
            "session here. track changes land in the listening post.",
            allowed_mentions=mt["allowed"])

    async def _simple_command(self, interaction: discord.Interaction, cmd: str, ack: str):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        try:
            await _post(f"/playback/{cmd}")
        except Exception as e:
            return await interaction.response.send_message(f"refused: {str(e)[:80]}", ephemeral=True)
        await interaction.response.send_message(ack, ephemeral=True)

    # ---------- playlists ----------

    playlist = app_commands.Group(name="playlist", description="Browse, play and (only my own) edit playlists")

    @playlist.command(name="list", description="Every playlist in the library — searchable")
    @app_commands.describe(query="filter by name")
    async def pl_list(self, interaction: discord.Interaction, query: str = None):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        try:
            rows = await _library_playlists(force=bool(query))
        except Exception as exc:
            return await interaction.followup.send(f"couldn't read your library — {str(exc)[:120]}")
        if query:
            rows = _filter(rows, query)
        if not rows:
            return await interaction.followup.send(
                f"no playlists match “{query}”" if query else "the library has no playlists")
        rows = rows[:25]
        mine = sum(1 for r in rows if is_owned(r["id"]))
        embed = _results_embed(
            f"📃 {query or 'library playlists'}",
            rows, note=f"{len(rows)} shown · {mine} editable by me · pick one to open it")
        await interaction.followup.send(embed=embed, view=Picker(rows, self._open_playlist))

    @playlist.command(name="show", description="See a playlist's tracks")
    @app_commands.describe(name="which playlist")
    @app_commands.autocomplete(name=_ac_library_playlist)
    async def pl_show(self, interaction: discord.Interaction, name: str):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        kind, rid = _decode(name)
        row = None
        if kind:
            try:
                row = next((r for r in await _library_playlists() if r["id"] == rid), None)
            except Exception:
                row = None
            row = row or {"id": rid, "name": rid, "kind": "playlists"}
        if row is None:
            try:
                matches = _filter(await _library_playlists(), name)
            except Exception as exc:
                return await interaction.followup.send(f"couldn't read your library — {str(exc)[:120]}")
            if not matches:
                return await interaction.followup.send(f"no playlist called “{name}”")
            row = matches[0]
        await self._open_playlist(interaction, row)

    @playlist.command(name="play", description="Play a whole playlist")
    @app_commands.describe(name="which playlist")
    @app_commands.autocomplete(name=_ac_library_playlist)
    async def pl_play(self, interaction: discord.Interaction, name: str):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        kind, rid = _decode(name)
        if kind is None:
            try:
                matches = _filter(await _library_playlists(), name)
            except Exception as exc:
                return await interaction.followup.send(f"couldn't read your library — {str(exc)[:120]}")
            if not matches:
                return await interaction.followup.send(f"no playlist called “{name}”")
            kind, rid = "playlists", matches[0]["id"]
        try:
            embed = await self._run_play(interaction, kind, rid, name)
        except Exception as exc:
            return await interaction.followup.send(f"couldn't play it — {str(exc)[:150]}")
        await interaction.followup.send(embed=embed)

    @playlist.command(name="new", description="Create a playlist — I'll own it and can edit it")
    @app_commands.describe(name="playlist name", description="optional blurb")
    async def pl_new(self, interaction: discord.Interaction, name: str, description: str = ""):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer(thinking=True)
        try:
            res = await _request("POST", "/write/playlists",
                                 params={"name": name, "description": description or ""},
                                 timeout=CREATE_TIMEOUT)
        except Exception as exc:
            return await interaction.followup.send(
                f"couldn't create it — {str(exc)[:150]}")
        pid = (res or {}).get("playlist_id")
        if not pid:
            return await interaction.followup.send(
                "the studio said okay but handed back no playlist id — check Apple "
                "Music in a minute before re-running this")
        mark_owned(pid, name, interaction.user.id)
        _forget_library_cache()
        embed = discord.Embed(title=f"📃 {name}", colour=COLOUR,
                              description="created — it's mine to edit from here on")
        embed.add_field(name="next", value="`/playlist add` a song, album or another playlist")
        embed.set_footer(text=f"id {pid}")
        await interaction.followup.send(embed=embed)

    @playlist.command(name="add", description="Add a song, album or playlist to one of my playlists")
    @app_commands.describe(playlist="one of my playlists", query="what to add — song, album or playlist")
    @app_commands.autocomplete(playlist=_ac_owned_playlist, query=_ac_add_source)
    async def pl_add(self, interaction: discord.Interaction, playlist: str, query: str):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        kind, pid = _decode(playlist)
        if not pid or not is_owned(pid):
            return await interaction.followup.send(
                await _refusal(pid, playlist), ephemeral=True)
        pname = owned().get(pid, {}).get("name") or pid
        kind, rid = _decode(query)
        if kind is None:
            try:
                found = await _catalog_search(query, "songs,albums,playlists", limit=5)
            except Exception as exc:
                return await interaction.followup.send(f"couldn't search — {str(exc)[:120]}")
            if not found:
                return await interaction.followup.send(f"couldn't find “{query}” — pick from the autocomplete list")
            kind, rid = found[0]["kind"], found[0]["id"]
        try:
            ids = await _song_ids(kind, rid)
        except Exception as exc:
            return await interaction.followup.send(f"couldn't list its tracks — {str(exc)[:150]}")
        if not ids:
            return await interaction.followup.send("that came back with no tracks to add")
        note = ""
        if len(ids) > 100:
            note = f" (first 100 of {len(ids)})"
            ids = ids[:100]
        try:
            await _request("POST", f"/write/playlists/{pid}/tracks",
                           body=ids, timeout=WRITE_TIMEOUT)
        except Exception as exc:
            return await interaction.followup.send(f"couldn't add them — {str(exc)[:150]}")
        _forget_library_cache()
        await interaction.followup.send(
            f"added **{len(ids)}** {KIND_WORD.get(kind, 'track')} track(s) to **{pname or query}**{note} 📃")

    @playlist.command(name="remove", description="Drop a track from one of my playlists (by number)")
    @app_commands.describe(playlist="one of my playlists", index="track number from /playlist show")
    @app_commands.autocomplete(playlist=_ac_owned_playlist)
    async def pl_remove(self, interaction: discord.Interaction, playlist: str,
                        index: app_commands.Range[int, 1, 500]):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        kind, pid = _decode(playlist)
        if not pid or not is_owned(pid):
            return await interaction.followup.send(
                await _refusal(pid, playlist), ephemeral=True)
        pname = owned().get(pid, {}).get("name") or pid
        try:
            tracks = await _playlist_tracks(pid)
        except Exception as exc:
            return await interaction.followup.send(f"couldn't read that playlist — {str(exc)[:150]}")
        if not 1 <= index <= len(tracks):
            return await interaction.followup.send(
                f"**{pname}** has {len(tracks)} track(s) — pick 1–{len(tracks)}")
        gone = tracks[index - 1]
        keep = [t.get("library_id") or t.get("id")
                for i, t in enumerate(tracks, 1) if i != index]
        try:
            await _request("PUT", f"/write/playlists/{pid}/tracks",
                           body=[k for k in keep if k], timeout=WRITE_TIMEOUT)
        except Exception as exc:
            return await interaction.followup.send(f"couldn't remove it — {str(exc)[:150]}")
        _forget_library_cache()
        await interaction.followup.send(
            f"removed **{gone.get('name')} — {gone.get('artist') or ''}** from **{pname}** 📃")

    @playlist.command(name="rename", description="Rename one of my playlists")
    @app_commands.describe(playlist="one of my playlists", name="the new name")
    @app_commands.autocomplete(playlist=_ac_owned_playlist)
    async def pl_rename(self, interaction: discord.Interaction, playlist: str, name: str):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        kind, pid = _decode(playlist)
        if not pid or not is_owned(pid):
            return await interaction.followup.send(
                await _refusal(pid, playlist), ephemeral=True)
        pname = owned().get(pid, {}).get("name") or pid
        try:
            await _request("PATCH", f"/write/playlists/{pid}",
                           params={"name": name}, timeout=WRITE_TIMEOUT)
        except Exception as exc:
            return await interaction.followup.send(f"couldn't rename it — {str(exc)[:150]}")
        mark_owned(pid, name, interaction.user.id)
        _forget_library_cache()
        await interaction.followup.send(f"**{pname}** → **{name}** 📃")

    @playlist.command(name="delete", description="Delete one of my playlists (asks first)")
    @app_commands.describe(playlist="one of my playlists")
    @app_commands.autocomplete(playlist=_ac_owned_playlist)
    async def pl_delete(self, interaction: discord.Interaction, playlist: str):
        if not _configured():
            return await interaction.response.send_message("the studio isn't wired up yet 🎧", ephemeral=True)
        kind, pid = _decode(playlist)
        if not pid or not is_owned(pid):
            return await interaction.response.send_message(
                await _refusal(pid, playlist), ephemeral=True)
        pname = owned().get(pid, {}).get("name") or pid

        async def answer(it: discord.Interaction, yes: bool):
            if not yes:
                return
            await it.response.defer()
            try:
                await _request("DELETE", f"/write/playlists/{pid}", timeout=WRITE_TIMEOUT)
            except Exception as exc:
                return await it.followup.send(f"couldn't delete it — {str(exc)[:150]}")
            unmark_owned(pid)
            # Apple applies the delete async: it answers 204 immediately and
            # the playlist drops out of the index a moment later. Give it a
            # beat, then say which of the two actually happened.
            gone = False
            for _ in range(5):
                await asyncio.sleep(2)
                try:
                    gone = all(r["id"] != pid for r in await _library_playlists(force=True))
                except Exception:
                    continue
                if gone:
                    break
            if gone:
                await it.edit_original_response(
                    content=f"deleted **{pname}** — gone from the library 📃", view=None)
            else:
                await it.edit_original_response(
                    content=(f"sent the delete for **{pname}** — Apple is still "
                             f"syncing it, it'll drop off the library in a minute 📃"),
                    view=None)

        await interaction.response.send_message(
            f"delete **{pname}**? it disappears from the Apple Music library too.",
            view=Confirm(answer), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
