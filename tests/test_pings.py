"""cogs/pings.py — mention construction and per-user opt-outs."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import memory
from cogs import pings
from tests.fakes import FakeGuild


class MentionTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_role_mention_when_role_exists(self):
        async def fake_role(guild, category):
            return SimpleNamespace(mention="<@&123>")

        guild = FakeGuild()
        with mock.patch.object(pings, "ensure_role", fake_role):
            out = self.run_async(pings.mention_for(guild, "announcements"))
        self.assertEqual(out["mention"], "<@&123>")
        self.assertTrue(out["allowed"].roles)
        self.assertFalse(out["allowed"].everyone)

    def test_empty_when_role_unavailable(self):
        async def fake_role(guild, category):
            return None

        with mock.patch.object(pings, "ensure_role", fake_role):
            out = self.run_async(pings.mention_for(FakeGuild(), "music"))
        self.assertEqual(out["mention"], "")
        self.assertFalse(out["allowed"].everyone)


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = {k: getattr(memory, k) for k in
                       ("MEM_DIR", "USER_DIR", "PERSONA_FILE", "SERVER_FILE",
                        "STM_FILE", "THOUGHTS_FILE", "RANKS_FILE", "STM_EMB_FILE")}
        root = Path(self.tmp.name)
        memory.MEM_DIR = root
        memory.USER_DIR = root / "users"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(memory, k, v)
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_ambient_optout(self):
        async def go():
            self.assertTrue(await pings.can_ambient_ping(1))
            mem = await memory.get_user(1)
            mem["pings"]["ambient"] = False
            await memory.save_user(1, mem)
            return await pings.can_ambient_ping(1)

        self.assertFalse(self.run_async(go()))


if __name__ == "__main__":
    unittest.main()
