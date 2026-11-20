"""cogs/music.py — the voice preflight must prove audio actually flows.

Two dead ends are recorded here, because both shipped and both left the bot
sitting in a voice channel in total silence:

1. The probe issued HEAD on the mp3 stream. tonearm's stream route has no HEAD
   (405), so it always failed and /play never joined.
2. The probe asked /api/audio/status for `running`. That flag is only true
   while a listener is already attached — tonearm drops the pipeline when the
   last one leaves — so on a first join it always read false.

The fix is to pull a little of the real stream, which is exactly what ffmpeg
is about to do. These tests pin that, including the silent-but-flowing case.
"""

import asyncio
import unittest
from unittest import mock

import httpx

from cogs import music
from tests.fakes import FakeGuild, FakeMember


class FakeStream:
    """Stands in for httpx's streaming response object."""

    def __init__(self, status=200, chunks=(b"x" * 8192,)):
        self.status_code = status
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class PreflightTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

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

    def _ctx(self, probe):
        return mock.patch.multiple(
            music, URL="http://studio", KEY="k",
            _stream_probe=mock.AsyncMock(return_value=probe))

    def test_live_stream_joins_and_plays(self):
        inter, guild, ch = self._interaction()
        vc = self._voice(guild, ch)
        cog = music.MusicCog(bot=mock.Mock())
        with self._ctx((True, None)), \
             mock.patch.object(music.discord, "FFmpegPCMAudio", mock.Mock()), \
             mock.patch.object(music.discord, "PCMVolumeTransformer", mock.Mock()):
            got, why = self.run_async(cog._ensure_voice(inter))
        self.assertIs(got, vc)
        self.assertIsNone(why)
        vc.play.assert_called_once()

    def test_dead_pump_means_no_voice_and_says_why(self):
        inter, guild, ch = self._interaction()
        vc = self._voice(guild, ch)
        cog = music.MusicCog(bot=mock.Mock())
        with self._ctx((False, "the studio's player is paused — run `/resume`")):
            got, why = self.run_async(cog._ensure_voice(inter))
        self.assertIsNone(got)
        self.assertIn("paused", why)
        vc.play.assert_not_called()

    def test_no_voice_channel_is_explained(self):
        inter, guild, ch = self._interaction()
        inter.user.voice = None
        cog = music.MusicCog(bot=mock.Mock())
        got, why = self.run_async(cog._ensure_voice(inter))
        self.assertIsNone(got)
        self.assertIn("voice channel", why)

    def test_already_playing_is_left_alone(self):
        inter, guild, ch = self._interaction()
        vc = self._voice(guild, ch)
        vc.is_playing.return_value = True
        cog = music.MusicCog(bot=mock.Mock())
        probe = mock.AsyncMock(return_value=(True, None))
        with mock.patch.multiple(music, URL="http://studio", KEY="k",
                                 _stream_probe=probe):
            got, why = self.run_async(cog._ensure_voice(inter))
        self.assertIs(got, vc)
        self.assertIsNone(why)
        vc.play.assert_not_called()
        probe.assert_not_awaited()


class StreamProbeTests(unittest.TestCase):
    """The probe must be honest about silence, not just about connectivity."""

    def run_async(self, coro):
        return asyncio.run(coro)

    def _client(self, stream):
        client = mock.Mock()
        client.stream = mock.Mock(return_value=_AsyncCtx(stream))
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)
        factory = mock.Mock()
        factory.__aenter__ = mock.AsyncMock(return_value=client)
        factory.__aexit__ = mock.AsyncMock(return_value=False)
        return factory

    def _probe(self, stream=None, np=None, exc=None):
        async def get(path, **params):
            if exc:
                raise exc
            return np or {}

        patches = [mock.patch.multiple(music, URL="http://studio", KEY="k"),
                   mock.patch.object(music, "_get", mock.Mock(side_effect=get))]
        if stream is not None:
            patches.append(mock.patch.object(music.httpx, "AsyncClient",
                                             mock.Mock(return_value=self._client(stream))))
        return patches

    def _run(self, stream=None, np=None, exc=None):
        patches = self._probe(stream, np, exc)
        for p in patches:
            p.start()
        try:
            return self.run_async(music._stream_probe())
        finally:
            for p in reversed(patches):
                p.stop()

    def test_bytes_and_a_playing_player_is_a_go(self):
        ok, why = self._run(FakeStream(), {"isPlaying": True})
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_flowing_but_paused_is_refused_with_instructions(self):
        ok, why = self._run(FakeStream(), {"isPlaying": False})
        self.assertFalse(ok)
        self.assertIn("/resume", why)

    def test_empty_stream_is_refused(self):
        ok, why = self._run(FakeStream(chunks=()), {"isPlaying": True})
        self.assertFalse(ok)
        self.assertIn("nothing", why)

    def test_http_error_is_refused_and_owned(self):
        ok, why = self._run(FakeStream(status=502), {"isPlaying": True})
        self.assertFalse(ok)
        self.assertIn("502", why)
        self.assertIn("my bug", why)

    def test_unreachable_studio_is_refused(self):
        client = mock.Mock()
        client.stream = mock.Mock(side_effect=httpx.ConnectError("refused"))
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)
        factory = mock.Mock()
        factory.__aenter__ = mock.AsyncMock(return_value=client)
        factory.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch.multiple(music, URL="http://studio", KEY="k"), \
             mock.patch.object(music, "_get", mock.AsyncMock(return_value={})), \
             mock.patch.object(music.httpx, "AsyncClient", mock.Mock(return_value=factory)):
            ok, why = self.run_async(music._stream_probe())
        self.assertFalse(ok)
        self.assertIn("isn't answering", why)

    def test_unconfigured_studio_is_refused(self):
        with mock.patch.multiple(music, URL="", KEY=""):
            ok, why = self.run_async(music._stream_probe())
        self.assertFalse(ok)
        self.assertIn("isn't wired up", why)


class _AsyncCtx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()


class SelfHealTests(unittest.TestCase):
    """A stopped player while people are in the channel must be restarted —
    silence that nobody notices is the worst failure mode."""

    def test_watcher_restarts_a_stopped_stream(self):
        import asyncio

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

        sleeps = []

        async def fake_sleep(_seconds):
            sleeps.append(_seconds)
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
