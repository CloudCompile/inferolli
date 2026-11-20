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
import os
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

    def read1(self, n):
        if self._chunks:
            return self._chunks.pop(0)[:n]
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

    def test_a_suspended_monitor_gives_silence_not_a_dead_stream(self):
        """PulseAudio sends a suspended sink's monitor NOTHING — not silence.

        Blocking there is how the bot ended up in the channel sending no
        packets at all while every flag it checked said "playing".
        """
        source, _ = self._source()
        with mock.patch.object(music.StudioAudio, "_ready", return_value=False):
            frame = source.read()
        self.assertEqual(len(frame), music.FRAME)
        self.assertEqual(frame, b"\x00" * music.FRAME)
        self.assertEqual(source.frames, 1)
        self.assertEqual(source.stalled_frames, 1)
        self.assertEqual(source.silent_frames, 1)
        self.assertFalse(source.dead)
        self.assertFalse(source.sounding())

    def test_dead_air_accumulates_so_the_watchdog_can_see_it(self):
        source, _ = self._source()
        with mock.patch.object(music.StudioAudio, "_ready", return_value=False):
            for _ in range(50):
                source.read()
        self.assertAlmostEqual(source.silent_seconds(), 1.0)
        self.assertEqual(source.stalled_frames, 50)

    def test_sound_after_dead_air_clears_the_silence_counter(self):
        loud = b"\x01\x00" * (music.FRAME // 2)
        source, _ = self._source(loud)
        with mock.patch.object(music.StudioAudio, "_ready", return_value=False):
            source.read()
        source.read()
        self.assertTrue(source.sounding())
        self.assertEqual(source.silent_frames, 0)
        self.assertEqual(source.stalled_frames, 1)

    def test_partial_chunks_are_assembled_into_a_whole_frame(self):
        half = b"\x01\x00" * (music.FRAME // 4)
        source, _ = self._source(half, half)
        frame = source.read()
        self.assertEqual(len(frame), music.FRAME)
        self.assertTrue(source.sounding())

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
    def test_reads_the_studio_sink(self):
        done = mock.Mock(stdout="1\ttonearm_audio\tmodule-null-sink.c\ts16le\n")
        with mock.patch.object(music.subprocess, "run", mock.Mock(return_value=done)):
            self.assertEqual(music._sink_name(), "tonearm_audio")

    def test_no_pulse_returns_none(self):
        with mock.patch.object(music.subprocess, "run", mock.Mock(side_effect=FileNotFoundError)):
            self.assertIsNone(music._sink_name())

    def test_a_desktop_sink_is_not_mistaken_for_the_studio(self):
        """First-sink-wins captured the machine's own output on a real desktop."""
        listed = ("1\talsa_output.pci-0000_00_1f.3.analog-stereo\tmodule-alsa-card.c\ts16le\n"
                  "2\tremote_out\tmodule-null-sink.c\ts16le\n"
                  "3\ttonearm_audio\tmodule-null-sink.c\ts16le\n")
        with mock.patch.object(music.subprocess, "run",
                               mock.Mock(return_value=mock.Mock(stdout=listed))):
            self.assertEqual(music._sink_name(), "tonearm_audio")

    def test_the_configured_sink_wins(self):
        listed = "1\ttonearm_audio\tx\ty\n2\tinferolli_studio\tx\ty\n"
        with mock.patch.dict(music.os.environ, {"TONEARM_AUDIO_SINK": "inferolli_studio"}), \
             mock.patch.object(music.subprocess, "run",
                               mock.Mock(return_value=mock.Mock(stdout=listed))):
            self.assertEqual(music._sink_name(), "inferolli_studio")

    def test_no_studio_sink_is_reported_rather_than_guessed(self):
        listed = "1\talsa_output.usb\tx\ty\n2\tremote_out\tx\ty\n"
        with mock.patch.dict(music.os.environ, {}, clear=False), \
             mock.patch.object(music.subprocess, "run",
                               mock.Mock(return_value=mock.Mock(stdout=listed))):
            self.assertIsNone(music._pick_sink(["alsa_output.usb", "remote_out"], "tonearm_audio"))

    def test_a_single_sink_is_still_used(self):
        self.assertEqual(music._pick_sink(["whatever_studio"], "tonearm_audio"), "whatever_studio")


class PulseRuntimeTests(unittest.TestCase):
    """The bot must find the daemon it can actually talk to."""

    def test_prefers_a_runtime_dir_with_a_live_socket(self):
        def exists(path):
            return path == "/run/user/1000/pulse/native"

        with mock.patch.dict(music.os.environ, {"XDG_RUNTIME_DIR": "/tmp/pulse-runtime"}), \
             mock.patch.object(music.os.path, "exists", mock.Mock(side_effect=exists)):
            self.assertEqual(music._pulse_runtime(), f"/run/user/{music.os.getuid()}")

    def test_env_runtime_dir_is_used_when_it_is_live(self):
        with mock.patch.dict(music.os.environ, {"XDG_RUNTIME_DIR": "/custom"}), \
             mock.patch.object(music.os.path, "exists", mock.Mock(return_value=True)):
            self.assertEqual(music._pulse_runtime(), "/custom")

    def test_no_daemon_anywhere_is_none(self):
        with mock.patch.object(music.os.path, "exists", mock.Mock(return_value=False)):
            self.assertIsNone(music._pulse_env().get("XDG_RUNTIME_DIR") and music._pulse_runtime())


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
        source.stalled_frames = 0
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


    def test_an_empty_player_starts_what_was_queued(self):
        """Nothing loaded, nothing playing: 'resume' resumes silence."""
        posted = []

        async def get(path, **kw):
            if path.endswith("/queue"):
                return {"items": [{"id": "617154366", "title": "Get Lucky"}]}
            return {"isPlaying": False, "id": None}

        async def post(path, **kw):
            posted.append(path)
            return {}

        with mock.patch.object(music, "_get", mock.Mock(side_effect=get)), \
             mock.patch.object(music, "_post", mock.Mock(side_effect=post)):
            self.assertIn("queued", self.run_async(music._nudge_player()))
        self.assertEqual(posted, ["/playback/play-song/617154366"])

    def test_an_empty_queue_gives_up_quietly(self):
        async def get(path, **kw):
            return {"items": []} if path.endswith("/queue") else {"isPlaying": False}

        with mock.patch.object(music, "_get", mock.Mock(side_effect=get)), \
             mock.patch.object(music, "_post", mock.Mock()) as post:
            self.assertEqual(self.run_async(music._nudge_player()), "")
        post.assert_not_called()


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


class DeadAirTests(unittest.TestCase):
    """is_playing() is true while a stalled source sends nothing at all."""

    class StalledStudio(music.StudioAudio):
        def __init__(self, silent_seconds=12.0):
            self.frames = 500
            self.silent_frames = int(silent_seconds / 0.02)
            self.stalled_frames = 10
            self.dead = False
            self.mode = "pulse"

        def cleanup(self):
            pass

    def test_the_watcher_restarts_dead_air(self):
        guild = FakeGuild()
        vc = mock.Mock()
        vc.is_playing.return_value = True  # discord.py thinks all is well
        vc.channel = mock.Mock(name="vc-channel")
        vc.source = self.StalledStudio()
        guild.voice_client = vc

        restarts = []

        async def start_stream(_self, client):  # patched on the class -> bound
            restarts.append(client)
            return None

        async def fake_sleep(_seconds):
            guild.voice_client = None  # one pass, then leave the loop

        async def now_playing(path, **params):
            return {}

        cog = music.MusicCog(bot=mock.Mock())
        cog.bot.wait_until_ready = mock.AsyncMock()
        with mock.patch.object(music, "_get", mock.Mock(side_effect=now_playing)), \
             mock.patch("cogs.music.asyncio.sleep", fake_sleep), \
             mock.patch("discord.utils.get", mock.Mock(return_value=None)), \
             mock.patch.object(music.MusicCog, "_start_stream", start_stream):
            asyncio.run(cog._np_watcher(guild))

        self.assertEqual(restarts, [vc])

    def test_short_dips_in_sound_are_left_alone(self):
        guild = FakeGuild()
        vc = mock.Mock()
        vc.is_playing.return_value = True
        vc.channel = mock.Mock(name="vc-channel")
        vc.source = self.StalledStudio(silent_seconds=2.0)
        guild.voice_client = vc

        restarts = []

        async def start_stream(_self, client):
            restarts.append(client)
            return None

        async def fake_sleep(_seconds):
            guild.voice_client = None

        async def now_playing(path, **params):
            return {}

        cog = music.MusicCog(bot=mock.Mock())
        cog.bot.wait_until_ready = mock.AsyncMock()
        with mock.patch.object(music, "_get", mock.Mock(side_effect=now_playing)), \
             mock.patch("cogs.music.asyncio.sleep", fake_sleep), \
             mock.patch("discord.utils.get", mock.Mock(return_value=None)), \
             mock.patch.object(music.MusicCog, "_start_stream", start_stream):
            asyncio.run(cog._np_watcher(guild))

        self.assertEqual(restarts, [])


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
    a *different* server — the sink looks fine and the audio is dead air.

    The dir itself differs per host (sandbox: /tmp/pulse-runtime, desktop:
    /run/user/1000), so the rule is "the first one with a live socket", not a
    hard-coded path. These tests fake the filesystem so they hold anywhere.
    """

    def _live(self, *dirs):
        def exists(path):
            return any(path == os.path.join(d, "pulse", "native") for d in dirs)
        return mock.Mock(side_effect=exists)

    def test_a_socketless_env_dir_falls_through_to_the_studio_dir(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/stale"}, clear=True), \
             mock.patch.object(music.os.path, "exists", self._live("/tmp/pulse-runtime")):
            self.assertEqual(music._pulse_env()["XDG_RUNTIME_DIR"], "/tmp/pulse-runtime")

    def test_the_desktop_daemon_is_found_when_the_env_has_nothing(self):
        user_dir = f"/run/user/{os.getuid()}"
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(music.os.path, "exists", self._live(user_dir)):
            self.assertEqual(music._pulse_env()["XDG_RUNTIME_DIR"], user_dir)

    def test_a_live_env_dir_is_respected(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/tmp/mine"}, clear=True), \
             mock.patch.object(music.os.path, "exists", self._live("/tmp/mine")):
            self.assertEqual(music._pulse_env()["XDG_RUNTIME_DIR"], "/tmp/mine")

    def test_missing_daemon_is_not_invented(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(music.os.path, "exists", self._live()):
            self.assertIsNone(music._pulse_runtime())
            self.assertNotIn("XDG_RUNTIME_DIR", music._pulse_env())


class ToneTests(unittest.TestCase):
    """/voicecheck's tone: it exists so the bot can be tested without the studio."""

    def test_a_frame_is_20ms_of_stereo_pcm(self):
        tone = music.ToneSource(seconds=0.1)
        self.assertEqual(len(tone.read()), music.FRAME)

    def test_the_tone_is_actually_audible(self):
        import array
        tone = music.ToneSource(seconds=0.5)
        samples = array.array("h", tone.read())
        self.assertGreater(max(abs(s) for s in samples), 1000)

    def test_it_stops_after_its_duration(self):
        tone = music.ToneSource(seconds=0.04)  # exactly two frames
        self.assertEqual(len(tone.read()), music.FRAME)
        self.assertEqual(len(tone.read()), music.FRAME)
        self.assertEqual(tone.read(), b"")
        self.assertTrue(tone.dead)

    def test_full_level_does_not_clip(self):
        import array
        tone = music.ToneSource(seconds=0.5, level=1.0)
        samples = array.array("h", tone.read())
        self.assertLessEqual(max(abs(s) for s in samples), 32767)

    def test_level_controls_amplitude(self):
        import array
        quiet = array.array("h", music.ToneSource(seconds=0.5, level=0.1).read())
        loud = array.array("h", music.ToneSource(seconds=0.5, level=0.5).read())
        self.assertLess(max(abs(s) for s in quiet), max(abs(s) for s in loud))

    def test_the_phase_carries_over_between_frames(self):
        """A restart at zero every frame is a 50 Hz buzz, not a tone."""
        import array
        tone = music.ToneSource(seconds=0.1)
        first = array.array("h", tone.read())
        second = array.array("h", tone.read())
        self.assertLess(abs(first[-1] - second[0]), 32767 // 4)


class SinkWakeTests(unittest.TestCase):
    """A suspended sink is indistinguishable from a dead studio, so wake it."""

    def test_wakes_the_named_sink(self):
        with mock.patch.object(music.subprocess, "run", mock.Mock()) as run:
            music._wake_sink("tonearm_audio")
        self.assertEqual(run.call_args[0][0],
                         ["pactl", "suspend-sink", "tonearm_audio", "0"])

    def test_no_sink_is_a_no_op(self):
        with mock.patch.object(music.subprocess, "run", mock.Mock()) as run:
            music._wake_sink(None)
        run.assert_not_called()

    def test_a_missing_pactl_does_not_raise(self):
        with mock.patch.object(music.subprocess, "run", mock.Mock(side_effect=FileNotFoundError)):
            music._wake_sink("tonearm_audio")  # must not raise


class VoiceEncryptionTests(unittest.TestCase):
    """DAVE (E2EE) is mandatory, and it has to actually encrypt.

    Two separate bugs lived here:

    1. Discord enforces A/V E2EE for non-stage calls and closes the voice
       websocket with code 4017 ("E2EE/DAVE protocol required") when a client
       advertises max_dave_protocol_version=0. Opting out is a refusal, not a
       downgrade.

    2. discord.py 2.7.1 gates encryption on `dave_session.ready`, which davey
       0.2.0 never sets (the field is now `can_encrypt`). Measured live: MLS
       status=active, 2 members, key present — and can_encrypt False for the
       whole session, so every frame went out legacy and every DAVE client
       threw it away.
    """

    def test_dave_stays_advertised_by_default(self):
        import discord.voice_state as voice_state
        before = voice_state.VoiceConnectionState.max_dave_protocol_version
        music._prefer_plain_voice()
        self.assertIs(voice_state.VoiceConnectionState.max_dave_protocol_version, before)

    def test_the_encryption_gate_uses_the_field_davey_actually_sets(self):
        music._prefer_plain_voice()
        import discord.voice_state as voice_state
        conn = voice_state.VoiceConnectionState.__new__(voice_state.VoiceConnectionState)
        conn.dave_protocol_version = 1
        conn.dave_session = mock.Mock(ready=False, can_encrypt=True)
        self.assertTrue(conn.can_encrypt, "a ready key must be used")

    def test_no_key_means_no_encryption(self):
        music._prefer_plain_voice()
        import discord.voice_state as voice_state
        conn = voice_state.VoiceConnectionState.__new__(voice_state.VoiceConnectionState)
        conn.dave_protocol_version = 1
        conn.dave_session = mock.Mock(ready=False, can_encrypt=False)
        self.assertFalse(conn.can_encrypt)

    def test_older_davey_falls_back_to_ready(self):
        music._prefer_plain_voice()
        import discord.voice_state as voice_state
        conn = voice_state.VoiceConnectionState.__new__(voice_state.VoiceConnectionState)
        conn.dave_protocol_version = 1
        session = mock.Mock(ready=True)
        del session.can_encrypt
        conn.dave_session = session
        self.assertTrue(conn.can_encrypt)

    def test_advertising_zero_requires_an_explicit_flag(self):
        import os
        with mock.patch.dict(os.environ, {"TONEARM_VOICE_NO_E2EE": "1"}):
            music._prefer_plain_voice()
        import discord.voice_state as voice_state
        conn = voice_state.VoiceConnectionState.__new__(voice_state.VoiceConnectionState)
        self.assertEqual(conn.max_dave_protocol_version, 0)


class E2EEGateTests(unittest.TestCase):
    """Frames sent before the DAVE session is ready are discarded silently.

    Measured earlier: a real tone (272-byte opus packets) at 50 packets/s, and
    total silence at the listener, because the session was not ready and the
    packets went out with the legacy scheme.
    """

    def run_async(self, coro):
        return asyncio.run(coro)

    def _cog(self):
        return music.MusicCog(bot=mock.Mock())

    def _vc(self, can_encrypt, connected=True):
        vc = mock.Mock()
        vc.is_connected.return_value = connected
        conn = mock.Mock()
        conn.can_encrypt = can_encrypt
        conn.dave_protocol_version = 1
        conn.dave_session = mock.Mock(ready=can_encrypt)
        vc._connection = conn
        return vc

    def test_an_encryptable_link_passes_immediately(self):
        with mock.patch("asyncio.sleep", mock.AsyncMock()):
            self.assertEqual(self.run_async(self._cog()._wait_e2ee(self._vc(True))), "")

    def test_a_link_that_cannot_encrypt_is_refused_with_a_reason(self):
        ticks = [0.0]

        def ticking():
            ticks[0] += 1.0
            return ticks[0]

        with mock.patch("asyncio.sleep", mock.AsyncMock()), \
             mock.patch.object(music.time, "monotonic", ticking):
            why = self.run_async(self._cog()._wait_e2ee(self._vc(False), timeout=5))
        self.assertIn("encryption handshake", why)
        self.assertIn("dave=1", why)

    def test_a_link_that_drops_is_not_played_into(self):
        vc = self._vc(False, connected=False)
        with mock.patch("asyncio.sleep", mock.AsyncMock()):
            self.assertIn("dropped", self.run_async(self._cog()._wait_e2ee(vc)))

    def test_the_stream_does_not_attach_before_encryption_is_ready(self):
        cog = self._cog()
        vc = self._vc(False)
        with mock.patch.object(music, "_configured", mock.Mock(return_value=True)), \
             mock.patch.object(music, "_new_source") as new_source, \
             mock.patch.object(cog, "_wait_e2ee", mock.AsyncMock(return_value="not ready")), \
             mock.patch("asyncio.sleep", mock.AsyncMock()):
            why = self.run_async(cog._start_stream(vc))
        self.assertEqual(why, "not ready")
        new_source.assert_not_called()
        vc.play.assert_not_called()


class VoiceReadinessTests(unittest.TestCase):
    """/voicecheck joined, left, and errored in the same second.

    The traceback was `vc.play()` raising ClientException('Not connected to
    voice') at the exact instant the log said "Voice connection complete":
    connect() can return a client that cannot accept a source yet. Two copies
    of the bot logged into the same session produce the same symptom, so the
    user hit both at once — hence a real wait, and a message that names the
    duplicate-session possibility instead of a stack trace.
    """

    def run_async(self, coro):
        return asyncio.run(coro)

    def _cog(self):
        return music.MusicCog(bot=mock.Mock())

    def test_waits_for_a_client_that_is_not_ready_yet(self):
        vc = mock.Mock()
        vc.is_connected.side_effect = [False, False, True]
        with mock.patch("asyncio.sleep", mock.AsyncMock()):
            self.assertTrue(self.run_async(self._cog()._wait_voice_ready(vc, timeout=5)))

    def test_gives_up_when_it_never_becomes_ready(self):
        vc = mock.Mock()
        vc.is_connected.return_value = False
        with mock.patch("asyncio.sleep", mock.AsyncMock()):
            self.assertFalse(self.run_async(self._cog()._wait_voice_ready(vc, timeout=0.1)))

    def test_a_link_that_never_settles_is_explained_not_raised(self):
        ch = mock.Mock(mention="#voice")
        ch.guild = mock.Mock()
        ch.permissions_for.return_value = mock.Mock(connect=True, speak=True)
        vc = mock.Mock()
        vc.is_connected.return_value = False
        ch.connect = mock.AsyncMock(return_value=vc)
        guild = mock.Mock(voice_client=None)
        inter = mock.Mock(guild=guild, user=mock.Mock())
        inter.user.voice = mock.Mock(channel=ch)
        ticks = [0.0]

        def ticking():
            ticks[0] += 1.0
            return ticks[0]

        with mock.patch("asyncio.sleep", mock.AsyncMock()), \
             mock.patch.object(music.time, "monotonic", ticking):
            got, why = self.run_async(self._cog()._join(inter))
        self.assertIsNone(got)
        self.assertIn("another copy", why)

    def test_the_tone_survives_a_client_that_was_not_ready(self):
        """vc.play() is retried, so a slow handshake is not a failed command."""
        vc = mock.Mock()
        vc.is_connected.return_value = True
        vc.play.side_effect = [__import__("discord").ClientException("Not connected to voice"), None]
        inter = mock.Mock()
        inter.response.defer = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        cog = self._cog()
        cog._join = mock.AsyncMock(return_value=(vc, None))
        cog._stop_voice = mock.Mock()
        cog._audio_report = mock.AsyncMock(return_value="report")
        cog._start_stream = mock.AsyncMock(return_value=None)
        with mock.patch.object(music, "ToneSource", mock.Mock()), \
             mock.patch("asyncio.sleep", mock.AsyncMock()), \
             mock.patch("asyncio.run_coroutine_threadsafe", mock.Mock()), \
             mock.patch("asyncio.get_running_loop", mock.Mock(return_value=mock.Mock())):
            self.run_async(cog.voicecheck.callback(cog, inter))
        self.assertEqual(vc.play.call_count, 2, "the second attempt must happen")
        self.assertTrue(inter.followup.send.called)
