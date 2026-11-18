"""cogs/automod.py — a flagged message must actually be acted on.

The bug this covers: automod checked `message.deleted` before acting, but
discord.py's Message has no such attribute. Every flagged message raised
AttributeError and escaped deletion, so automod looked enabled while doing
nothing — the worst failure mode for a moderation feature.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import automod
from tests.fakes import FakeChannel, FakeGuild, FakeMember, FakeMessage


class FakeACog(automod.AutoModCog):
    """Cog instance without a bot, so we can drive on_message directly."""

    def __init__(self, cfg):
        self.bot = SimpleNamespace()
        self._pending = set()
        self.strikes = {}
        self._cfg = cfg

    async def _cfg_for(self, guild_id):
        return self._cfg

    async def _classify(self, message):
        return ("high", 0.99, "scam link")


class AutomodActTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"automod": {"enabled": True, "delete_on_low": True,
                                "timeout_on_high": False, "timeout_seconds": 600}}
        self.guild = FakeGuild()
        self.channel = FakeChannel(name="general-chat")
        self.author = FakeMember(id=42, name="someone", guild=self.guild)
        self.msg = FakeMessage(id=901, content="free nitro scam", channel=self.channel)
        self.msg.author = self.author
        self.msg.guild = self.guild

    def _run(self, cog):
        with mock.patch("config.get", mock.AsyncMock(return_value=self.cfg)), \
             mock.patch.object(automod, "send_mod_log", mock.AsyncMock()), \
             mock.patch.object(cog, "_record_strike",
                               lambda *a, **k: len(self._strikes()) + 1), \
             mock.patch.object(cog, "_warn_in_character", mock.AsyncMock()):
            asyncio.run(cog.on_message(self.msg))

    def _strikes(self):
        return []

    def test_flagged_message_is_deleted(self):
        cog = FakeACog(self.cfg)
        self._run(cog)
        self.assertTrue(self.msg.deleted_by_bot,
                        "a high-severity message must be deleted, not skipped")

    def test_no_attribute_error_on_message_object(self):
        # regression: `not message.deleted` raised AttributeError on every
        # flagged message, so automod silently failed open
        self.assertFalse(hasattr(FakeMessage(), "deleted"))

    def test_clean_message_is_left_alone(self):
        cog = FakeACog(self.cfg)

        async def clean(message):
            return ("clean", 0.97, "")

        cog._classify = clean
        self._run(cog)
        self.assertFalse(self.msg.deleted_by_bot)


if __name__ == "__main__":
    unittest.main()
