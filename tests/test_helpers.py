"""helpers.py — admin check, anti-storm guard, mod-log posting."""

import asyncio
import time
import unittest

import helpers
from tests.fakes import FakeChannel, FakeGuild, FakeMember


class AdminTests(unittest.TestCase):
    def test_admin_flag(self):
        self.assertTrue(helpers.is_admin(FakeMember(admin=True)))
        self.assertFalse(helpers.is_admin(FakeMember(admin=False)))


class SendGuardTests(unittest.TestCase):
    def test_first_send_allowed_then_blocked_then_expires(self):
        cid = 777
        helpers._last_bot_send.pop(cid, None)
        self.assertTrue(helpers.may_send(cid))
        helpers.mark_sent(cid)
        self.assertFalse(helpers.may_send(cid))
        helpers._last_bot_send[cid] = time.monotonic() - helpers.SEND_GUARD_S - 1
        self.assertTrue(helpers.may_send(cid))


class ModLogTests(unittest.TestCase):
    def test_skips_without_channel(self):
        guild = FakeGuild()
        asyncio.run(helpers.send_mod_log(guild, {}, None))
        self.assertEqual(guild.text_channels, [])

    def test_posts_to_configured_channel(self):
        chan = FakeChannel(id=5)
        guild = FakeGuild(channels=[chan])
        asyncio.run(helpers.send_mod_log(guild, {"mod_log_channel_id": 5}, "embed"))
        self.assertEqual(len(chan.sent), 1)


if __name__ == "__main__":
    unittest.main()
