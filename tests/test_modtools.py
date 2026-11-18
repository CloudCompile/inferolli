"""cogs/modtools.py — case persistence, hierarchy guards, defer-safe acks."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cogs import modtools
from tests.fakes import (FakeGuild, FakeInteraction, FakeMember, FakeRole)


class CaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cases = modtools.CASES_FILE
        modtools.CASES_FILE = Path(self.tmp.name) / "cases.json"

    def tearDown(self):
        modtools.CASES_FILE = self._cases
        self.tmp.cleanup()

    def test_record_and_filter_by_window(self):
        modtools.record_case(1, 42, "warn", "spam", "admin")
        self.assertEqual(len(modtools.member_cases(1, 42)), 1)
        # age the case out of the 30-day window
        store = json.loads(modtools.CASES_FILE.read_text())
        store["1"]["42"][0]["ts"] = time.time() - 31 * 86400
        modtools.CASES_FILE.write_text(json.dumps(store))
        self.assertEqual(modtools.member_cases(1, 42), [])

    def test_case_cap(self):
        for i in range(105):
            modtools.record_case(1, 42, "warn", f"r{i}", "admin")
        self.assertEqual(len(modtools.member_cases(1, 42)), 100)

    def test_reason_truncated(self):
        case = modtools.record_case(1, 42, "ban", "x" * 500, "admin")
        self.assertEqual(len(case["reason"]), 300)

    def test_guild_isolation(self):
        modtools.record_case(1, 42, "warn", "a", "admin")
        self.assertEqual(modtools.member_cases(2, 42), [])


class CanTouchTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild()
        self.admin = FakeMember(id=5, name="boss", roles=[FakeRole(1, "👑 Admiral", 10)],
                                admin=True, guild=self.guild)
        self.sailor = FakeMember(id=6, name="sailor", roles=[FakeRole(2, "⚓ Sailor", 5)],
                                 guild=self.guild)
        self.deckhand = FakeMember(id=7, name="deck", roles=[FakeRole(3, "🪝 Deckhand", 3)],
                                   guild=self.guild)

    def _touch(self, actor, target):
        it = FakeInteraction(user=actor, guild=self.guild)
        return modtools._can_touch(it, target)

    def test_bot_protected(self):
        bot = FakeMember(id=9, name="otherbot", bot=True, guild=self.guild)
        self.assertEqual(self._touch(self.admin, bot), "that's a bot")

    def test_admins_protected(self):
        self.assertIn("admin", self._touch(self.sailor, self.admin))

    def test_hierarchy_respected(self):
        self.assertIn("ladder", self._touch(self.sailor, self.sailor))

    def test_lower_member_is_touchable(self):
        self.assertIsNone(self._touch(self.sailor, self.deckhand))

    def test_owner_bypasses_hierarchy(self):
        self.guild.owner_id = self.sailor.id
        peer = FakeMember(id=8, name="peer", roles=[FakeRole(4, "⚓ Sailor", 5)],
                          guild=self.guild)
        self.assertIsNone(self._touch(self.sailor, peer))


class AckTests(unittest.TestCase):
    def setUp(self):
        self.cog = modtools.ModToolsCog(bot=None)

    def test_ack_before_defer_uses_response(self):
        it = FakeInteraction()
        asyncio.run(self.cog._ack(it, "done"))
        self.assertEqual(it.response.sent[0][0], "done")
        self.assertEqual(it.followup.sent, [])

    def test_ack_after_defer_uses_followup(self):
        it = FakeInteraction()

        async def go():
            await it.response.defer(ephemeral=True)
            await self.cog._ack(it, "done")

        asyncio.run(go())
        self.assertEqual(it.followup.sent[0][0], "done")
        self.assertEqual(it.response.sent, [])


if __name__ == "__main__":
    unittest.main()
