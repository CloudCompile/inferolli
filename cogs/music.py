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

import array
import asyncio
import json
import logging
import math
import os
import select
import subprocess
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

# 20 ms of 48 kHz stereo s16 — one Discord voice frame.
FRAME = 3840
# How long one frame may take to arrive before we call the capture stalled.
# PulseAudio gives a *suspended* sink's monitor nothing at all — not silence —
# so without this the read blocks forever and the bot stops sending audio.
# Short enough that dead air still goes out at roughly Discord's frame rate
# (a stalled frame is emitted as silence rather than collapsing the stream).
READ_TIMEOUT = 0.06


def _pulse_runtime() -> str | None:
    """The PulseAudio runtime dir that actually has a live socket.

    This used to assume the sandbox's /tmp/pulse-runtime. On the VPS the
    daemon lives in /run/user/1000, so the assumption meant the bot talked to
    a daemon that was not there and captured nothing. Ask the filesystem.
    """
    seen = []
    for candidate in (os.environ.get("XDG_RUNTIME_DIR"),
                      f"/run/user/{os.getuid()}",
                      "/tmp/pulse-runtime"):
        if not candidate or candidate in seen:
            continue
        seen.append(candidate)
        if os.path.exists(os.path.join(candidate, "pulse", "native")):
            return candidate
    return None


def _pulse_env() -> dict:
    """Environment that can reach the studio's PulseAudio daemon."""
    env = dict(os.environ)
    runtime = _pulse_runtime()
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
    return env


def _wake_sink(sink: str | None) -> None:
    """Stop PulseAudio suspending the studio's sink.

    A suspended sink's monitor delivers NO data — not a stream of silence —
    so every client reading it blocks forever. That is how the bot ended up
    sitting in a voice channel sending nothing at all while every flag it
    checked said "playing": the read never returned, so no frame was ever
    encoded, and nothing could notice.
    """
    if not sink:
        return
    try:
        subprocess.run(["pactl", "suspend-sink", sink, "0"],
                       capture_output=True, timeout=5, env=_pulse_env())
    except Exception:
        log.debug("could not wake the studio sink", exc_info=True)


# The application name Discord gives its *voice* playback stream. UI sounds
# (the deafen blip) come from a different stream, `discord-canary`, which is
# why one can be audible while the other is not.
VOICE_STREAM_APP = "webrtc voiceengine"


def _sink_inputs_listing() -> str:
    try:
        out = subprocess.run(["pactl", "list", "sink-inputs"],
                             capture_output=True, text=True, timeout=5,
                             env=_pulse_env())
        return out.stdout or ""
    except Exception:
        log.debug("could not list sink inputs", exc_info=True)
        return ""


def _muted_voice_streams(listing: str) -> list[str]:
    """Indexes of *muted* voice playback streams in a `pactl` listing.

    Deafen mutes Discord's voice stream inside PulseAudio, and un-deafening
    does not reliably lift it. Everything the bot can read then looks perfect:
    dave ready, `can_encrypt` true, packets arriving, remote-user volume 1.0,
    `deaf: false` — and the sink input still says `Mute: yes`, so the monitor
    records silence. The UI's own blips are a different stream, so they stay
    audible and make "the client can't reach the server" look true when it is
    not. This is a mute no flag in the bot can see.
    """
    found: list[str] = []
    idx: str | None = None
    app = ""
    muted = False

    def close():
        if idx is not None and muted and app.strip().lower() == VOICE_STREAM_APP:
            found.append(idx)

    for line in listing.splitlines():
        s = line.strip()
        if s.startswith("Sink Input #"):
            close()
            idx = s.split("#", 1)[1].strip()
            app, muted = "", False
        elif s.startswith("application.name = "):
            app = s.split("=", 1)[1].strip().strip('"')
        elif s.startswith("Mute:"):
            muted = s.split(":", 1)[1].strip().lower() == "yes"
    close()
    return found


def _unmute_voice_streams() -> list[str]:
    """Lift that mute. Returns the indexes that were muted."""
    indexes = _muted_voice_streams(_sink_inputs_listing())
    for idx in indexes:
        try:
            subprocess.run(["pactl", "set-sink-input-mute", idx, "0"],
                           capture_output=True, timeout=5, env=_pulse_env())
        except Exception:
            log.warning("could not unmute voice stream %s", idx, exc_info=True)
    return indexes


def _pick_sink(sinks: list[str], want: str) -> str | None:
    """Choose the studio's sink out of whatever the daemon lists.

    Taking the first sink was fine in the sandbox, where the studio's was the
    only one. On a desktop it is not: picking the first sink there captures the
    machine's own output (or a remote-desktop sink) instead of the music.
    """
    if want in sinks:
        return want
    studio = [s for s in sinks if "tonearm" in s.lower()]
    if len(studio) == 1:
        return studio[0]
    if studio:
        return studio[0]
    if len(sinks) == 1:
        return sinks[0]
    return None


def _sink_name() -> str | None:
    """The PulseAudio sink the studio plays into."""
    want = os.environ.get("TONEARM_AUDIO_SINK") or "tonearm_audio"
    try:
        out = subprocess.run(["pactl", "list", "short", "sinks"],
                             capture_output=True, text=True, timeout=5,
                             env=_pulse_env())
        sinks = []
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                sinks.append(parts[1])
        return _pick_sink(sinks, want)
    except Exception:
        log.debug("no pactl — falling back to the HTTP stream", exc_info=True)
    return None


class StudioAudio(discord.AudioSource):
    """The studio's live audio, read straight from its PulseAudio monitor.

    Two reasons this is not the HTTP stream route:

    1. tonearm's unsubscribe() stops the capture pipeline even when other
       listeners remain, so ANY second client disconnecting (including a
       preflight probe) kills the audio underneath the bot.
    2. It lets the bot MEASURE what it sends. Silence is the failure people
       actually hit, and a flag like isPlaying:true cannot see it — a stalled
       Chromium reports playing while its sink goes idle.

    The HTTP stream stays as a fallback for a studio on another machine.
    """

    def __init__(self, url: str = "", device: str | None = None):
        self.frames = 0
        self.silent_frames = 0
        self.stalled_frames = 0
        self._buf = b""
        self.dead = False
        self.mode = "pulse" if device else "http"
        if device:
            args = ["ffmpeg", "-nostdin", "-loglevel", "error",
                    "-f", "pulse", "-i", f"{device}.monitor"]
        else:
            args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", url]
        args += ["-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"]
        self._proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,
                                      stdin=subprocess.DEVNULL,
                                      env=_pulse_env() if device else None)

    def _ready(self, timeout: float) -> bool:
        try:
            return bool(select.select([self._proc.stdout], [], [], timeout)[0])
        except Exception:
            return True  # let the (blocking) read decide

    def read(self) -> bytes:
        """One 20 ms frame — always. Silence counts as a frame.

        Returning b"" when the capture goes quiet would stop the player, and
        the player stopping looks identical to the track ending. A silent
        frame instead keeps Discord's timeline running and makes the dead air
        measurable, so the watchdog can see it and restart the studio.
        """
        if self.dead:
            return b""
        while len(self._buf) < FRAME:
            if not self._ready(READ_TIMEOUT):
                break
            chunk = self._proc.stdout.read1(FRAME - len(self._buf))
            if not chunk:
                self.dead = True
                return b""
            self._buf += chunk
        if len(self._buf) < FRAME:
            missing = FRAME - len(self._buf)
            self.frames += 1
            self.silent_frames += 1
            self.stalled_frames += 1
            data = self._buf + b"\x00" * missing
            self._buf = b""
            return data
        data, self._buf = self._buf[:FRAME], self._buf[FRAME:]
        self.frames += 1
        if data.count(b"\x00") == len(data):
            self.silent_frames += 1
        else:
            self.silent_frames = 0
        return data

    def is_opus(self) -> bool:
        return False

    def sounding(self) -> bool:
        """Bytes flowing, and not all of them digital silence."""
        return self.frames > 0 and self.silent_frames < self.frames

    def silent_seconds(self) -> float:
        return self.silent_frames * 0.02

    def cleanup(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass


class ToneSource(discord.AudioSource):
    """A plain 440 Hz tone, generated in-process — no studio involved.

    The studio's audio goes through a browser, PulseAudio, a capture process
    and a sink. This goes through none of them. So it separates two failures
    that look identical from the outside ("i hear nothing"):

    - tone audible, studio silent -> the bot's path into Discord is fine, the
      fault is the studio's audio;
    - tone NOT audible -> nothing the studio does matters, the packets are
      leaving but not arriving (or not being decoded), and that's the bug.
    """

    def __init__(self, seconds: float = 6.0, hz: float = 440.0,
                 level: float = 0.3):
        self.total_frames = max(1, int(seconds / 0.02))
        self.frames = 0
        self.dead = False
        self.mode = "tone"
        self.silent_frames = 0
        self._amp = int(32767 * max(0.0, min(1.0, level)))
        self._phase = 0.0
        self._step = 2.0 * math.pi * hz / 48000.0

    def read(self) -> bytes:
        if self.frames >= self.total_frames:
            self.dead = True
            return b""
        self.frames += 1
        buf = array.array("h")
        phase, step, amp = self._phase, self._step, self._amp
        for _ in range(FRAME // 4):  # stereo pairs in one 20 ms frame
            sample = int(amp * math.sin(phase))
            phase += step
            buf.append(sample)
            buf.append(sample)
        self._phase = phase % (2.0 * math.pi)
        return buf.tobytes()

    def is_opus(self) -> bool:
        return False

    def sounding(self) -> bool:
        return self.frames > 0

    def silent_seconds(self) -> float:
        return 0.0

    def cleanup(self) -> None:
        pass


async def _nudge_player() -> str:
    """Kick a stalled player. Returns a short description of what was tried."""
    try:
        np = await _get("/playback/now-playing")
    except Exception:
        return ""
    try:
        if np.get("isPlaying") is False and np.get("id"):
            await _post("/playback/play")
            return "resumed the player"
        song = np.get("id")
        if not song:
            # Nothing is loaded at all. The queue outlives a stop, so start
            # what was lined up instead of resuming into silence.
            queue = await _get("/playback/queue")
            items = queue.get("items") or []
            song = items[0].get("id") if items else None
        if song:
            await _post(f"/playback/play-song/{song}")
            return "restarted the track" if np.get("id") else "started the queued track"
    except StudioError:
        log.debug("nudge refused", exc_info=True)
    return ""


def _new_source() -> StudioAudio:
    device = _sink_name()
    _wake_sink(device)
    url = f"{URL}/api/audio/stream.mp3?api_key={KEY}"
    return StudioAudio(url, device)


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

def _voice_can_encrypt(self) -> bool:
    """Whether this connection may encrypt frames (discord.py's own gate).

    discord.py 2.7.1 asks `dave_session.ready`, but davey 0.2.0 replaced that
    with `can_encrypt`: in 0.2.0 `ready` stays False even once the MLS group is
    established, while `can_encrypt` becomes True. The migration guide is
    explicit — "we stop sending frames until we have a key to encrypt with".

    With the stale flag, `can_encrypt` here was False forever, so
    `_get_voice_packet` sent every frame with the LEGACY scheme. Discord now
    enforces DAVE, and a DAVE call DISCARDS legacy frames: real tone, 50
    packets a second, silence at every listener, nothing wrong in our logs.
    That is the bug that survived every other fix.
    """
    session = self.dave_session
    if self.dave_protocol_version == 0 or session is None:
        return False
    flag = getattr(session, "can_encrypt", None)
    if flag is None:
        flag = getattr(session, "ready", False)
    return bool(flag)


def _prefer_plain_voice() -> None:
    """Wire up Discord's mandatory DAVE (E2EE) so our audio is actually heard.

    Two things matter, and each one cost a debugging session:

    1. DAVE can NOT be opted out of any more. Discord enforces A/V E2EE for
       non-stage voice calls and closes the voice websocket with code 4017
       ("E2EE/DAVE protocol required") for a client that advertises
       max_dave_protocol_version=0. That was the retry loop that looked like
       "it joins for half a second and leaves".

    2. The connection must encrypt. davey 0.2.0 exposes `can_encrypt`, and
       discord.py 2.7.1 checks the older `ready` instead, which never becomes
       true — so the link silently falls back to legacy frames that DAVE
       clients drop.

    TONEARM_VOICE_NO_E2EE=1 still forces the old opt-out for diagnostics.
    """
    if os.environ.get("TONEARM_VOICE_NO_E2EE") == "1":
        try:
            import discord.voice_state as _voice_state
            _voice_state.VoiceConnectionState.max_dave_protocol_version = property(
                lambda self: 0)
            log.warning("voice: E2EE disabled by TONEARM_VOICE_NO_E2EE — modern "
                        "Discord closes this with 4017")
        except Exception:
            log.warning("could not disable DAVE", exc_info=True)
        return

    try:
        import discord.voice_state as _voice_state
        _voice_state.VoiceConnectionState.can_encrypt = property(_voice_can_encrypt)
        log.info("voice: DAVE wired up (can_encrypt <- davey can_encrypt)")
    except Exception:
        log.warning("could not wire up DAVE — voice may be inaudible", exc_info=True)


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._np_task = None
        self._last_track = None
        _prefer_plain_voice()

    # ---------- voice ----------

    async def _wait_voice_ready(self, vc, timeout: float = 20.0) -> bool:
        """Wait until the voice client will actually accept a source.

        `channel.connect()` can return a client that reports "not connected"
        for a moment after the handshake completes. Calling play() in that gap
        raises ClientException('Not connected to voice') — which is what made
        /voicecheck join, leave and error out in the same second.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if vc is not None and vc.is_connected():
                    return True
            except Exception:
                log.debug("voice client unusable while waiting", exc_info=True)
                return False
            await asyncio.sleep(0.25)
        return False

    async def _unmute_client_voice(self, user) -> str:
        """Lift a mute the client left on its own voice stream after deafen.

        Only when the person has *not* deafened themselves right now: being
        deafened is their choice, and a muted stream is then correct. Returns
        a sentence for the person when a mute was actually cleared.
        """
        state = getattr(user, "voice", None)
        if state is None or getattr(state, "self_deaf", False) or getattr(state, "deaf", False):
            return ""
        fixed = await asyncio.to_thread(_unmute_voice_streams)
        if not fixed:
            return ""
        log.info("voice: client playback stream(s) %s were muted — unmuted",
                 ", ".join(fixed))
        return ("🎚 your client had its own voice stream muted inside the audio "
                f"server (left over from deafen) — i unmuted it ({', '.join(fixed)})")

    async def _join(self, interaction: discord.Interaction):
        """Join (or move to) the asker's voice channel. Returns (vc, why_not)."""
        user = getattr(interaction, "user", None)
        if not user or not getattr(user, "voice", None) or not user.voice.channel:
            return None, "join a voice channel first and I'll bring the music 🎧"
        ch = user.voice.channel
        vc = interaction.guild.voice_client
        perms = ch.permissions_for(ch.guild.me)
        if vc is None or not vc.is_connected():
            if not (perms.connect and perms.speak):
                return None, f"i can't join {ch.mention} — check my Connect/Speak permissions"
            try:
                vc = await ch.connect(self_deaf=True, timeout=30)
            except Exception as exc:
                log.warning("voice connect failed: %s", exc)
                return None, "voice wouldn't connect just now — give it a few seconds and try again"
            if not await self._wait_voice_ready(vc):
                return None, ("i joined but the voice link never became usable — "
                              "if this keeps happening, another copy of me may be "
                              "logged in somewhere")
        elif vc.channel != ch:
            await vc.move_to(ch)
        # A mute left behind by deafen kills the audio without changing a
        # single flag the bot can read (see _muted_voice_streams), so clear it
        # here — every command that brings sound goes through this join.
        self._client_mute_note = await self._unmute_client_voice(user)
        return vc, None

    async def _ensure_voice(self, interaction: discord.Interaction):
        """Join the requester's voice channel and start the live stream.

        Returns (voice_client, why_not). `why_not` is a sentence a person can
        act on when we couldn't bring audio, and None when we did.
        """
        vc, why_not = await self._join(interaction)
        if vc is None:
            return None, why_not
        if vc.is_playing():
            return vc, None
        # Attach and then check the audio itself. Every earlier version of this
        # checked a flag instead — HEAD (no such route), then
        # /audio/status.running (only true while someone already listens) —
        # and each one left the bot sitting in the channel in silence.
        why = await self._start_stream(vc)
        if why:
            return None, why
        return vc, None

    async def _wait_e2ee(self, vc, timeout: float = 15.0) -> str:
        """Wait until the voice link can actually encrypt what we send.

        Frames sent before DAVE is ready go out with the LEGACY scheme, and
        every listener discards them — real tone, 50 packets a second, total
        silence, and nothing on our side complains. That is why "it says it is
        playing" and "nobody hears anything" lived together for so long.

        Returns "" when the link can encrypt, or a sentence explaining what it
        is stuck on.
        """
        conn = getattr(vc, "_connection", None)
        if conn is None:
            return "the voice client has no connection object"
        started = time.monotonic()
        seen = None
        while time.monotonic() - started < timeout:
            if getattr(conn, "can_encrypt", False):
                if seen is not None:
                    log.info("voice: E2EE ready after %.1fs (dave=%s)",
                             time.monotonic() - started,
                             getattr(conn, "dave_protocol_version", "?"))
                return ""
            if not vc.is_connected():
                return "the voice link dropped while setting up encryption"
            await asyncio.sleep(0.25)
        version = getattr(conn, "dave_protocol_version", None)
        ready = getattr(getattr(conn, "dave_session", None), "ready", None)
        log.warning("voice: E2EE never became ready (dave=%s ready=%s)", version, ready)
        return (f"i'm in the channel but Discord's encryption handshake didn't finish "
                f"(dave={version}, ready={ready}), so anything i played would be "
                f"dropped. try again in a moment")

    def _voice_state_report(self, vc) -> str:
        conn = getattr(vc, "_connection", None)
        if conn is None:
            return "dave=? · ready=?"
        return (f"dave={getattr(conn, 'dave_protocol_version', '?')} · "
                f"e2ee_ready={getattr(getattr(conn, 'dave_session', None), 'ready', '?')} · "
                f"can_encrypt={getattr(conn, 'can_encrypt', '?')}")

    async def _start_stream(self, vc) -> str | None:
        """Attach the studio's audio and confirm sound flows. None on success."""
        if not _configured():
            return "the studio isn't wired up yet 🎧"
        why = await self._wait_e2ee(vc)
        if why:
            return why
        # One player at a time. Leaving the old one attached leaked an ffmpeg
        # per restart, all of them reading the same monitor.
        self._stop_voice(vc)
        await asyncio.sleep(0.05)
        source = await asyncio.to_thread(_new_source)
        vc.play(source)
        await asyncio.sleep(1.5)
        if source.frames and source.sounding():
            log.info("audio attached: mode=%s frames=%d silent=%d stalled=%d",
                     source.mode, source.frames, source.silent_frames,
                     source.stalled_frames)
            return None
        if source.frames == 0:
            source.cleanup()
            self._stop_voice(vc)
            return ("the studio has no audio to give right now — it's either still "
                    "starting up or Apple has nothing loaded; try again in a few "
                    "seconds")
        # Bytes but digital silence: Chromium has stalled (a track that "plays"
        # forever with an idle sink). Nudge it and listen again.
        tried = await _nudge_player()
        source.cleanup()
        self._stop_voice(vc)
        source = await asyncio.to_thread(_new_source)
        vc.play(source)
        await asyncio.sleep(1.5)
        if source.frames and source.sounding():
            log.info("studio was silent — %s and the sound came back", tried or "retried")
            return None
        source.cleanup()
        self._stop_voice(vc)
        return ("the studio is silent: it says it's playing but no sound is coming "
                "out. i restarted its player and it's still silent, so this is a "
                "bug on my side, not yours")

    @staticmethod
    def _stop_voice(vc) -> None:
        try:
            if vc.is_playing():
                vc.stop()
        except Exception:
            pass

    async def _audio_report(self, vc) -> str:
        """The facts about each link in the chain, for /voicecheck.

        Written to be readable by a person and useful to me: every line is a
        link that can be the broken one.
        """
        lines = []
        runtime = await asyncio.to_thread(_pulse_runtime)
        lines.append(f"**pulse** `{runtime or 'none'}` "
                     f"({'ok' if runtime else 'no live socket'})")
        sink = await asyncio.to_thread(_sink_name)
        lines.append(f"**sink** `{sink}`" if sink
                     else "**sink** none — falling back to the HTTP stream")
        try:
            out = await asyncio.to_thread(lambda: subprocess.run(
                ["pactl", "list", "short", "sink-inputs"],
                capture_output=True, text=True, timeout=5, env=_pulse_env()))
            feeds = [l for l in (out.stdout or "").splitlines() if l.strip()]
            lines.append(f"**sink inputs** {len(feeds)} "
                         f"({'the studio is feeding it' if feeds else 'NOTHING is feeding it'})")
        except Exception as exc:
            lines.append(f"**sink inputs** unreadable ({type(exc).__name__})")
        try:
            np = await _get("/playback/now-playing")
            lines.append(f"**player** {np.get('title') or 'nothing'} · "
                         f"playing={np.get('isPlaying')} · state={np.get('state')}")
        except Exception:
            lines.append("**player** not answering")
        src = getattr(vc, "source", None)
        if src is None:
            lines.append("**source** attached but idle")
        else:
            lines.append(f"**source** {getattr(src, 'mode', type(src).__name__)} · "
                         f"{getattr(src, 'frames', 0)} frames · "
                         f"{getattr(src, 'silent_frames', 0)} silent · "
                         f"dead={getattr(src, 'dead', '?')}")
        lines.append(f"**voice** {getattr(getattr(vc, 'channel', None), 'name', '?')} · "
                     f"connected={vc.is_connected()} · {self._voice_state_report(vc)}")
        muted = await asyncio.to_thread(
            lambda: _muted_voice_streams(_sink_inputs_listing()))
        lines.append("**client out** "
                     + (f"voice stream MUTED ({', '.join(muted)}) — this is deafen's "
                        "leftover, and it silences everything i send"
                        if muted else "voice stream unmuted"))
        note = getattr(self, "_client_mute_note", "")
        if note:
            lines.append(note)
        return "\n".join(lines)

    async def _np_watcher(self, guild: discord.Guild):
        """While a voice party is live: post track changes, and keep the audio up.

        The stream can die without anyone touching Discord — tonearm tears its
        pipeline down whenever a listener disconnects, and a crashed pipeline
        answers status with `running: true` while encoding nothing. Silence that
        nobody notices is the worst outcome, so this refreshes the player when
        it has stopped but people are still in the channel (capped, with a log,
        so a genuinely dead studio can't spin forever).
        """
        await self.bot.wait_until_ready()
        post = discord.utils.get(guild.text_channels, name=LISTENING_POST)
        failures = 0
        while guild.voice_client and guild.voice_client.is_connected():
            vc = guild.voice_client
            try:
                np = await _get("/playback/now-playing")
                track = f"{np.get('title')}|{np.get('artist')}"
                if np.get("title") and track != self._last_track:
                    self._last_track = track
                    if post and post.permissions_for(guild.me).send_messages:
                        await post.send(embed=_np_embed(np))
            except Exception:
                log.debug("np watcher hiccup", exc_info=True)
            source = getattr(vc, "source", None)
            # A stalled source (frames still arriving, or the capture blocked
            # on a suspended sink) is dead air, and is_playing() cannot see it.
            stalled = isinstance(source, StudioAudio) and (
                source.dead or source.silent_seconds() >= 10)
            if (not vc.is_playing() or stalled) and failures < 3:
                why = await self._start_stream(vc)
                if why:
                    failures += 1
                    log.warning("stream restart %d/3 refused: %s", failures, why)
                else:
                    failures = 0
                    log.info("stream restarted in %s", getattr(vc.channel, "name", "?"))
            if isinstance(source, StudioAudio):
                log.info("stream telemetry: frames=%d silent_s=%.1f stalled=%d "
                         "dead=%s playing=%s", source.frames,
                         source.silent_seconds(), source.stalled_frames,
                         source.dead, vc.is_playing())
            await asyncio.sleep(8)

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
        vc, why_not, embed_note = None, None, None
        try:
            vc, why_not = await self._ensure_voice(interaction)
        except Exception as exc:
            log.warning("voice join failed", exc_info=True)
            why_not = f"i couldn't get into the voice channel — {str(exc)[:100]}"
        bits = [KIND_WORD.get(kind, kind)]
        if summary.get("skipped"):
            bits.append(f"{summary['skipped']} unavailable track(s) skipped")
        if summary.get("queued"):
            bits.append(f"{summary['queued']} queued")
        if vc:
            bits.append(f"streaming into {vc.channel.mention}")
        elif why_not:
            # never say "streaming" when nobody is hearing anything
            embed_note = why_not
            bits.append("no audio — see below")
        elif summary.get("mode") == "song":
            bits.append("on the studio player")
        embed = _np_embed(np)
        embed.set_footer(text=" · ".join(bits))
        if why_not and not vc:
            embed.description = f"{embed.description}\n\n⚠️ {embed_note}" if embed.description else f"⚠️ {embed_note}"
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
        await self._simple_command(interaction, "play", "resumed ▶")

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
        vc, why_not = await self._ensure_voice(interaction)
        if vc is None:
            return await interaction.followup.send(f"🎧 {why_not or 'couldn’t start the stream'}")
        self._np_task = self.bot.loop.create_task(self._np_watcher(interaction.guild))
        from cogs.pings import mention_for
        mt = await mention_for(interaction.guild, "music")
        await interaction.followup.send(
            (mt["mention"] + " " if mt["mention"] else "") +
            "🎧 **listening party started** — playing the studio's live "
            "session here. track changes land in the listening post.",
            allowed_mentions=mt["allowed"])

    @app_commands.command(name="voicecheck",
                          description="Test whether my audio reaches you: plays a 6s tone")
    async def voicecheck(self, interaction: discord.Interaction):
        """Play a tone that owes nothing to the studio, then print every link.

        "i can't hear it" has three different causes and I can only see the
        middle one from in here. A tone the sandbox generates itself separates
        them: hear the beep but not the music -> the studio's audio is at
        fault; hear neither -> my packets leave and don't arrive, and no amount
        of studio debugging will help.
        """
        await interaction.response.defer(ephemeral=True)
        vc, why_not = await self._join(interaction)
        if vc is None:
            return await interaction.followup.send(
                f"🎧 {why_not or 'i could not join that channel'}", ephemeral=True)
        loop = asyncio.get_running_loop()
        # Do not pretend to test audio over a link that cannot carry it yet:
        # frames sent before the DAVE session is ready are discarded by every
        # listener. Waiting here is the difference between "the tone is
        # inaudible" and "the tone was never going to be audible".
        e2ee = await self._wait_e2ee(vc)
        self._stop_voice(vc)
        tone = ToneSource(seconds=6)

        def after(exc):
            if exc:
                log.warning("test tone ended with an error: %s", exc)
            else:
                log.info("test tone finished: %d frames sent", tone.frames)
            asyncio.run_coroutine_threadsafe(self._start_stream(vc), loop)

        started = False
        for attempt in range(3):
            try:
                vc.play(tone, after=after)
                started = True
                break
            except discord.ClientException as exc:
                log.warning("tone did not start (attempt %d): %s", attempt + 1, exc)
                await self._wait_voice_ready(vc, timeout=5.0)
                await asyncio.sleep(0.5)
        if not started:
            return await interaction.followup.send(
                "🎧 i couldn't start the tone — my voice connection isn't usable. "
                "run /voicecheck again in a few seconds; if it keeps failing, a "
                "second copy of me is probably logged in.", ephemeral=True)
        await asyncio.sleep(1.0)
        report = await self._audio_report(vc)
        embed = discord.Embed(
            title="voice check — listen for a steady beep",
            description=(report + "\n\n**6 seconds of beep, then the studio's music "
                         "starts.** if you hear the beep but not the music, the fault "
                         "is the studio's audio. if you hear neither, my packets are "
                         "leaving this box and not arriving at your ears."
                         + (f"\n\n⚠️ **encryption:** {e2ee}" if e2ee else "")),
            colour=0xE67E22 if e2ee else 0x2ECC71)
        embed.set_footer(text="tell me which of the two you got — that's the whole diagnosis")
        await interaction.followup.send(embed=embed, ephemeral=True)

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
