"""Music cog — Inferolli as the server's DJ, powered by tonearm.

tonearm (a separate E2B sandbox) drives the user's own logged-in Apple Music
web player and exposes it as a REST API + live audio streams. This cog gives
the server:

  /play <query>   search Apple Music, start playback, join your voice channel
                  if you're in one — instant listening party
  /np             what's playing right now (artwork, lyrics snippet)
  /skip /pause /resume /queue /volume
  /party          join your voice channel and stream the session live

While a voice party is active, track changes are posted to the
🎧│listening-post channel (auto-created by admins via abilities).
"""

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import httpx

log = logging.getLogger("cogs.music")

URL = os.environ.get("TONEARM_URL", "").rstrip("/")
KEY = os.environ.get("TONEARM_API_KEY", "")
LISTENING_POST = "listening-post"

COLOUR = 0xFA243C  # apple music red


def _configured() -> bool:
    return bool(URL and KEY)


def _headers() -> dict:
    return {"Authorization": f"Bearer {KEY}"}


async def _get(path: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(f"{URL}/api{path}", headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(f"{URL}/api{path}", headers=_headers())
        r.raise_for_status()
        return r.json()


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


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._np_task = None
        self._last_track = None

    # ---------- voice ----------

    async def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        """Join (or move to) the requester's voice channel and start the live stream."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            return None
        ch = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        perms = ch.permissions_for(ch.guild.me)
        if vc is None or not vc.is_connected():
            if not (perms.connect and perms.speak):
                return None
            vc = await ch.connect(self_deaf=True)
        elif vc.channel != ch:
            await vc.move_to(ch)
        if not vc.is_playing():
            # preflight the stream — mock mode serves no audio by design
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.head(f"{URL}/api/audio/stream.mp3?api_key={KEY}")
            if r.status_code != 200:
                return None  # studio isn't streaming (no live session yet)
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

    # ---------- commands ----------

    @app_commands.command(name="play", description="Play something on the server's Apple Music")
    async def play(self, interaction: discord.Interaction, query: str):
        if not _configured():
            return await interaction.response.send_message(
                "the studio isn't wired up yet 🎧", ephemeral=True)
        await interaction.response.defer()
        try:
            res = await _get("/catalog/search", term=query, limit=5)
        except Exception:
            return await interaction.followup.send("the studio didn't answer — tonearm might be down")
        songs = (res or {}).get("songs", [])
        if not songs:
            return await interaction.followup.send(f"couldn't find anything for “{query}”")
        song = songs[0]
        # rows come flat from tonearm ({name, artist, album, artwork,
        # duration_ms, ...}) but tolerate raw nested catalog shapes too
        attrs = song.get("attributes") or song
        name = attrs.get("name") or attrs.get("title")
        artist = attrs.get("artist") or attrs.get("artistName")
        album = attrs.get("album") or attrs.get("albumName")
        art = attrs.get("artwork")
        artwork = art.get("url") if isinstance(art, dict) else art
        dur = attrs.get("duration_ms") or attrs.get("durationMs")
        try:
            await _post(f"/playback/play-song/{song.get('catalog_id') or song.get('id')}")
        except Exception as e:
            return await interaction.followup.send(f"playback refused: {str(e)[:100]}")
        vc = await self._ensure_voice(interaction)
        where = f" — streaming into {vc.channel.mention} 🎧" if vc else " (on the studio player)"
        await interaction.followup.send(
            embed=_np_embed({"title": name, "artist": artist, "album": album or "",
                             "artworkUrl": artwork, "isPlaying": True,
                             "durationMs": dur, "elapsedMs": 0})
            .set_footer(text=f"requested by {interaction.user.display_name}{where}"))
        if vc and (self._np_task is None or self._np_task.done()):
            self._np_task = self.bot.loop.create_task(self._np_watcher(interaction.guild))

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


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
