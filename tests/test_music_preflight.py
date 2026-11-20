"""cogs/music.py — the voice preflight must probe /audio/status, not HEAD.

History: the preflight issued HEAD on the mp3 stream endpoint. tonearm's
stream route does not implement HEAD (405), so the probe ALWAYS failed and
/play quietly skipped joining a voice channel — the "no audio in VC" report.
"""

import unittest
from unittest import mock

import discord

from cogs import music
from tests.fakes import FakeGuild, FakeMember


class PreflightTests(unittest.TestCase):
    def run_async(self, coro):
        import asyncio
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

    def test_status_endpoint_is_used_not_head(self):
        inter, guild, ch = self._interaction()
        vc = mock.Mock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.channel = ch
        guild.voice_client = vc

        calls = []

        async def fake_get(path, **params):
            calls.append(path)
            return {"running": True}

        cog = music.MusicCog(bot=mock.Mock())
        with mock.patch.object(music, "_get", fake_get), \
             mock.patch.object(music.discord, "FFmpegPCMAudio",
                               mock.Mock()), \
             mock.patch.object(music.discord, "PCMVolumeTransformer",
                               mock.Mock()):
            got = self.run_async(cog._ensure_voice(inter))
        self.assertIs(got, vc)
        self.assertEqual(calls, ["/audio/status"])
        vc.play.assert_called_once()

    def test_dead_pump_means_no_voice(self):
        """A studio that isn't pumping audio must not join — otherwise the
        channel fills with digital silence."""
        inter, guild, ch = self._interaction()
        vc = mock.Mock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.channel = ch
        vc.move_to = mock.AsyncMock()
        guild.voice_client = vc

        async def fake_get(path, **params):
            return {"running": False}

        cog = music.MusicCog(bot=mock.Mock())
        with mock.patch.object(music, "_get", fake_get):
            got = self.run_async(cog._ensure_voice(inter))
        self.assertIsNone(got)
        vc.play.assert_not_called()

    def test_unreachable_studio_means_no_voice(self):
        inter, guild, ch = self._interaction()
        vc = mock.Mock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.channel = ch
        vc.move_to = mock.AsyncMock()
        guild.voice_client = vc

        async def boom(path, **params):
            raise RuntimeError("down")

        cog = music.MusicCog(bot=mock.Mock())
        with mock.patch.object(music, "_get", boom):
            self.assertIsNone(self.run_async(cog._ensure_voice(inter)))


if __name__ == "__main__":
    unittest.main()
