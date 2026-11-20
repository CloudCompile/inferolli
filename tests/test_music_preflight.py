"""cogs/music.py — the audio path, which has been wrong three ways.

Real failures this file pins, in order of discovery:

1. The preflight issued HEAD on the mp3 stream. tonearm's stream route has no
   HEAD (405), so /play never joined a voice channel.
2. The preflight then asked /api/audio/status for `running`. That flag is only
   true while a listener is already attached — tonearm deletes its pipeline
   when the last one leaves — so on a first join it always read false and the
   bot sat in the channel, silent.
3. The preflight then pulled bytes from the stream, which DID pass — and then
   the probe's own disconnect called tonearm's unsubscribe(), which stops the
   capture pipeline even when other listeners remain. It killed the audio
   under the bot's freshly started ffmpeg. The bot played silence, and no
   error was logged because ffmpeg exited cleanly on EOF.

So: read the studio's sink directly, and MEASURE. `isPlaying: true` cannot see
a stalled Chromium whose sink has gone idle — that is exactly what happened.
"""

import asyncio
import unittest
from unittest import mock

from cogs import music
from tests.fakes import FakeGuild, FakeMember


class FakeProc:
    """Stand-in for the ffmpeg subprocess behind StudioAudio."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.killed = False
        self.stdout = self  # StudioAudio reads from proc.stdout

    def read(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def kill(self):
        self.killed = True


class StudioAudioTests(unittest.TestCase):
    def _source(self, *chunks, device="tonearm_audio"):
        proc = FakeProc(chunks)
        with mock.patch.object(music.subprocess, "Popen", mock.Mock(return_value=proc)):
            source = music.StudioAudio("http://studio/stream.mp3", device)
        return source, proc

    def test_real_audio_counts_as_sounding(self):
        loud = b"\x01\x00" * (music.FRAME // 2)
        source, proc = self._source(loud, loud)
        source.read()
        self.assertTrue(source.sounding())
        self.assertEqual(source.silent_frames, 0)
        self.assertEqual(source.mode, "pulse")

    def test_digital_silence_is_not_sounding(self):
        silence = b"\x00" * music.FRAME
        source, _ = self._source(silence, silence, silence)
        for _ in range(3):
            source.read()
        self.assertFalse(source.sounding())
        self.assertAlmostEqual(source.silent_seconds(), 0.06)

    def test_sound_after_silence_resets_the_counter(self):
        silence = b"\x00" * music.FRAME
        loud = b"\x01\x00" * (music.FRAME // 2)
        source, _ = self._source(silence, silence, loud)
        for _ in range(3):
            source.read()
        self.assertTrue(source.sounding())
        self.assertEqual(source.silent_frames, 0)

    def test_eof_marks_the_source_dead(self):
        source, _ = self._source()
        self.assertEqual(source.read(), b"")
        self.assertTrue(source.dead)
        self.assertFalse(source.sounding())

    def test_cleanup_kills_ffmpeg(self):
        source, proc = self._source()
        source.cleanup()
        self.assertTrue(proc.killed)

    def test_http_fallback_when_there_is_no_sink(self):
        with mock.patch.object(music.subprocess, "Popen", mock.Mock(return_value=FakeProc([]))) as popen:
            music.StudioAudio("http://studio/stream.mp3", None)
        args = popen.call_args[0][0]
        self.assertIn("http://studio/stream.mp3", args)
        self.assertNotIn("pulse", args)

    def test_pulse_capture_reads_the_monitor(self):
        with mock.patch.object(music.subprocess, "Popen", mock.Mock(return_value=FakeProc([]))) as popen:
            music.StudioAudio("http://x", "tonearm_audio")
        args = popen.call_args[0][0]
        self.assertIn("pulse", args)
        self.assertIn("tonearm_audio.monitor", args)
        self.assertIn("-ac", args)
        self.assertEqual(args[args.index("-ac") + 1], "2")
        self.assertEqual(args[-1], "pipe:1")


class SinkNameTests(unittest.TestCase):
    def test_reads_the_first_sink(self):
        done = mock.Mock(stdout="1\ttonearm_audio\tmodule-null-sink.c\ts16le\n")
        with mock.patch.object(music.subprocess, "run", mock.Mock(return_value=done)):
            self.assertEqual(music._sink_name(), "tonearm_audio")

    def test_no_pulse_returns_none(self):
        with mock.patch.object(music.subprocess, "run", mock.Mock(side_effect=FileNotFoundError)):
            self.assertIsNone(music._sink_name())


class AttachTests(unittest.TestCase):
    """The bot must not claim success unless sound is actually flowing."""

    def run_async(self, coro):
        return asyncio.run(coro)

    def _cog(self):
        return music.MusicCog(bot=mock.Mock())

    def _interaction(self):
        guild = FakeGuild()
        member = FakeMember(id=7)
        member.guild = guild
        ch = mock.Mock()
        ch.guild = guild
        ch.permissions_for = lambda m: mock.Mock(connect=True, speak=True)
        ch.mention = "#voice"
        member.voice = mock.Mock()
        member.voice.channel = ch
        inter = mock.Mock()
        inter.guild = guild
        inter.user = member
        return inter, guild, ch

    def _voice(self, guild, ch):
        vc = mock.Mock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.channel = ch
        vc.move_to = mock.AsyncMock()
        guild.voice_client = vc
        return vc

    def _source(self, sounding=True, frames=10):
        source = mock.Mock(spec=music.StudioAudio)
        source.frames = frames
        source.sounding.return_value = sounding
        source.dead = False
        source.mode = "pulse"
        source.silent_frames = 0
        source.cleanup = mock.Mock()
        return source

    def test_sound_flowing_means_joined(self):
        inter, guild, ch = self._interaction()
        vc = self._voice(guild, ch)
        cog = self._cog()
        source = self._source()
        with mock.patch.multiple(music, URL="http://studio", KEY="k"), \
             mock.patch.object(music, "_new_source", mock.Mock(return_value=source)), \
             mock.patch("asyncio.sleep", mock.AsyncMock()):
            got, why = self.run_async(cog._ensure_voice(inter))
        self.assertIs(got, vc)
        self.assertIsNone(why)
        vc.play.assert_called_once()

    def test_absolute_silence_is_refused_after_a_nudge(self):
        inter, guild, ch = self._interaction()
        vc = self._voice(guild, ch)
        cog = self._cog()
        silent = self._source(sounding=False)
        with mock.patch.multiple(music, URL="http://studio", KEY="k"), \
             mock.patch.object(music, "_new_source", mock.Mock(return_value=silent)), \
             mock.patch.object(music, "_nudge_player", mock.AsyncMock(return_value="restarted the track")), \
             mock.patch("asyncio.sleep", mock.AsyncMock()):
            got, why = self.run_async(cog._ensure_voice(inter))
        self.assertIsNone(got)
        self.assertIn("still silent", why)
        self.assertIn("bug on my side", why)
        self.assertEqual(silent.cleanup.call_count, 2)

    def test_nudge_that_works_is_accepted(self):
        inter, guild, ch = self._interaction()
        vc = self._voice(guild, ch)
        cog = self._cog()
        silent = self._source(sounding=False)
        recovered = self._source(sounding=True)
        with mock.patch.multiple(music, URL="http://studio", KEY="k"), \
             mock.patch.object(music, "_new_source", mock.Mock(side_effect=[silent, recovered])), \
             mock.patch.object(music, "_nudge_player", mock.AsyncMock(return_value="resumed the player")), \
             mock.patch("asyncio.sleep", mock.AsyncMock()):
            got, why = self.run_async(cog._ensure_voice(inter))
        self.assertIs(got, vc)
        self.assertIsNone(why)

    def test_no_bytes_at_all_is_refused(self):
        inter, guild, ch = self._interaction()
        vc = self._voice(guild, ch)
        cog = self._cog()
        empty = self._source(sounding=False, frames=0)
        with mock.patch.multiple(music, URL="http://studio", KEY="k"), \
             mock.patch.object(music, "_new_source", mock.Mock(return_value=empty)), \
             mock.patch("asyncio.sleep", mock.AsyncMock()):
            got, why = self.run_async(cog._ensure_voice(inter))
        self.assertIsNone(got)
        self.assertIn("no audio to give", why)

    def test_no_voice_channel_is_explained(self):
        inter, guild, ch = self._interaction()
        inter.user.voice = None
        got, why = self.run_async(self._cog()._ensure_voice(inter))
        self.assertIsNone(got)
        self.assertIn("voice channel", why)

    def test_already_playing_is_left_alone(self):
        inter, guild, ch = self._interaction()
        vc = self._voice(guild, ch)
        vc.is_playing.return_value = True
        fresh = mock.Mock()
        with mock.patch.multiple(music, URL="http://studio", KEY="k",
                                 _new_source=fresh):
            got, why = self.run_async(self._cog()._ensure_voice(inter))
        self.assertIs(got, vc)
        self.assertIsNone(why)
        fresh.assert_not_called()


class NudgeTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_paused_player_is_resumed(self):
        posted = []

        async def get(path, **kw):
            return {"isPlaying": False, "id": "123"}

        async def post(path, **kw):
            posted.append(path)
            return {}

        with mock.patch.object(music, "_get", mock.Mock(side_effect=get)), \
             mock.patch.object(music, "_post", mock.Mock(side_effect=post)):
            self.assertEqual(self.run_async(music._nudge_player()), "resumed the player")
        self.assertEqual(posted, ["/playback/play"])

    def test_stalled_playing_track_is_restarted(self):
        posted = []

        async def get(path, **kw):
            return {"isPlaying": True, "id": "617154366"}

        async def post(path, **kw):
            posted.append(path)
            return {}

        with mock.patch.object(music, "_get", mock.Mock(side_effect=get)), \
             mock.patch.object(music, "_post", mock.Mock(side_effect=post)):
            self.assertIn("restarted", self.run_async(music._nudge_player()))
        self.assertEqual(posted, ["/playback/play-song/617154366"])


class SelfHealTests(unittest.TestCase):
    def test_watcher_restarts_a_stopped_stream(self):
        guild = FakeGuild()
        vc = mock.Mock()
        vc.is_playing.return_value = False
        vc.channel = mock.Mock(name="vc-channel")
        guild.voice_client = vc

        cog = music.MusicCog(bot=mock.Mock())
        restarts = []

        async def start_stream(_cog_self, client):  # patched on the class → bound
            restarts.append(client)
            return None

        async def fake_sleep(_seconds):
            guild.voice_client = None  # one pass, then leave the loop

        async def now_playing(path, **params):
            return {}

        cog.bot.wait_until_ready = mock.AsyncMock()
        with mock.patch.object(music, "_get", mock.Mock(side_effect=now_playing)), \
             mock.patch("cogs.music.asyncio.sleep", fake_sleep), \
             mock.patch("discord.utils.get", mock.Mock(return_value=None)), \
             mock.patch.object(music.MusicCog, "_start_stream", start_stream):
            asyncio.run(cog._np_watcher(guild))

        self.assertEqual(restarts, [vc])


class CommandNameTests(unittest.TestCase):
    def test_resume_uses_the_real_tonearm_command(self):
        """tonearm accepts play|pause|toggle|next|previous — /resume used to
        POST /playback/resume and 404."""
        src = open("cogs/music.py").read()
        self.assertIn('_simple_command(interaction, "play", "resumed', src)
        self.assertNotIn('"resume", "resumed', src)


if __name__ == "__main__":
    unittest.main()


class PulseEnvTests(unittest.TestCase):
    """A bot that cannot reach the studio's pulse daemon captures silence from
    a *different* server — the sink looks fine and the audio is dead air."""

    def test_runtime_dir_defaults_to_the_studios(self):
        import os
        env = {"PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(music.os.path, "isdir", return_value=True):
            self.assertEqual(music._pulse_env()["XDG_RUNTIME_DIR"], "/tmp/pulse-runtime")

    def test_an_existing_runtime_dir_is_respected(self):
        import os
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/tmp/mine"}, clear=True), \
             mock.patch.object(music.os.path, "isdir", return_value=True):
            self.assertEqual(music._pulse_env()["XDG_RUNTIME_DIR"], "/tmp/mine")

    def test_missing_runtime_dir_is_not_invented(self):
        import os
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(music.os.path, "isdir", return_value=False):
            self.assertNotIn("XDG_RUNTIME_DIR", music._pulse_env())
