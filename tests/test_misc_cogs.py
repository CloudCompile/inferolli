"""The remaining cogs — deterministic logic that had no tests at all.

Coverage here is deliberately focused on the parts that decide something
(parsing, formatting, gating), not on Discord plumbing that the fakes would
only pretend to exercise.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import e2b_keepalive, music, serverpulse, shiplog, utils


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class DurationParseTests(unittest.TestCase):
    """utils.parse_duration — /remind's only real logic."""

    def test_common_units(self):
        for text, seconds in [("10m", 600), ("2h", 7200), ("3d", 259200),
                              ("1w", 604800), ("45s", 45)]:
            self.assertEqual(utils.parse_duration(text), seconds, text)

    def test_word_units(self):
        for text, seconds in [("5 minutes", 300), ("2 hours", 7200),
                              ("1 day", 86400)]:
            self.assertEqual(utils.parse_duration(text), seconds, text)

    def test_embedded_in_sentence(self):
        self.assertEqual(utils.parse_duration("remind me in 15m please"), 900)

    def test_garbage_is_none(self):
        for text in ("", "soon", "later", "abc", "10", "m"):
            self.assertIsNone(utils.parse_duration(text), text)

    def test_zero_is_none_ish(self):
        # 0m parses to 0, which /remind rejects as falsy — assert the contract
        self.assertEqual(utils.parse_duration("0m"), 0)


class NowPlayingEmbedTests(unittest.TestCase):
    """music._np_embed — the only formatting logic that can crash a command."""

    def test_full_track(self):
        e = music._np_embed({"title": "Song", "artist": "Artist", "album": "Album",
                             "durationMs": 240000, "elapsedMs": 60000, "isPlaying": True,
                             "artworkUrl": "https://x.invalid/a.png"})
        self.assertEqual(e.title, "Song")
        self.assertIn("Artist", e.description)
        self.assertTrue(e.thumbnail.url.endswith("a.png"))

    def test_empty_payload_is_safe(self):
        e = music._np_embed({})
        self.assertEqual(e.title, "nothing playing")

    def test_missing_duration_skips_playhead(self):
        e = music._np_embed({"title": "x", "artist": "y"})
        self.assertEqual(e.fields, [])

    def test_paused_marker(self):
        e = music._np_embed({"title": "x", "durationMs": 100000, "elapsedMs": 1000,
                             "isPlaying": False})
        self.assertIn("❙❙", e.fields[0].value)

    def test_playhead_at_the_end_does_not_divide_oddly(self):
        e = music._np_embed({"title": "x", "durationMs": 1000, "elapsedMs": 1000,
                             "isPlaying": True})
        self.assertIn("◉", e.fields[0].value)

    def test_configured_needs_url_and_key(self):
        # these are read once at import, so patch the module attributes
        with mock.patch.object(music, "URL", ""), mock.patch.object(music, "KEY", "k"):
            self.assertFalse(music._configured())
        with mock.patch.object(music, "URL", "https://t.invalid"), mock.patch.object(music, "KEY", ""):
            self.assertFalse(music._configured())
        with mock.patch.object(music, "URL", "https://t.invalid"), mock.patch.object(music, "KEY", "k"):
            self.assertTrue(music._configured())


class ServerPulseBlockTests(unittest.TestCase):
    def test_block_includes_age_and_digest(self):
        cog = serverpulse.ServerPulseCog(SimpleNamespace())
        cog.digest = "#general: hello"
        cog.updated = serverpulse.time.time() - 120
        block = cog.block()
        self.assertIn("#general: hello", block)
        self.assertIn("2 min ago", block)

    def test_never_refreshed_reports_stale(self):
        cog = serverpulse.ServerPulseCog(SimpleNamespace())
        cog.digest = ""
        cog.updated = 0
        self.assertIn("999 min ago", cog.block())


class KeepaliveTests(unittest.TestCase):
    """e2b_keepalive runs a keepalive ping only inside a sandbox. The gate is
    inline in its loop, so the test pins the two conditions it requires."""

    def test_gate_requires_flag_and_sandbox_id(self):
        import inspect
        src = inspect.getsource(e2b_keepalive)
        self.assertIn('E2B_KEEPALIVE", "") == "1"', src)
        self.assertIn("sandbox_id", src)

    def test_module_imports_without_sandbox_env(self):
        # importing must never require E2B credentials (CI, local dev)
        self.assertTrue(hasattr(e2b_keepalive, "setup"))


class ShiplogTests(unittest.TestCase):
    def test_cog_exists_and_has_daily_post(self):
        self.assertTrue(hasattr(shiplog, "ShipLogCog"))
        self.assertTrue(hasattr(shiplog.ShipLogCog, "post_state_of_ship"))


if __name__ == "__main__":
    unittest.main()
