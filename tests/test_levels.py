"""levels.py — movement math, promotion gates, standings board persistence."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory
from cogs import levels
from tests.fakes import FakeChannel, FakeGuild


class MovementTests(unittest.TestCase):
    def test_pulls_toward_tier_center(self):
        # score 5, judged Sailor (center 49.5): delta = 44.5 * 0.25 = 11.1 → clamped to 8
        self.assertEqual(levels._movement(5, 2, 0.9), 8.0)
        # score 42, judged Sailor (center 49.5): 7.5 * 0.25 = 1.875
        self.assertAlmostEqual(levels._movement(42, 2, 0.9), 1.875)
        # score 49, judged Sailor: (49.5-49)*0.25 = 0.125
        self.assertAlmostEqual(levels._movement(49, 2, 0.9), 0.125)

    def test_negative_movement_is_bounded(self):
        # score 100 judged Landlubber (center 9.5): -22.6 → clamped to -6
        self.assertEqual(levels._movement(100, 0, 0.9), -6.0)

    def test_tier_index_unknown_defaults_to_bottom(self):
        self.assertEqual(levels._tier_index("👑 Admiral"), 5)
        self.assertEqual(levels._tier_index("not a tier"), 0)


class NightlyTests(unittest.TestCase):
    """run_nightly with the AI and the role layer mocked out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._mem_dir = memory.MEM_DIR
        memory.MEM_DIR = Path(self.tmp.name)
        self.ranks = {
            1: {"score": 5, "tier": "🪵 Landlubber", "history": [],
                "last_eval": None, "last_reason": ""},
            2: {"score": 5, "tier": "🪵 Landlubber", "history": [],
                "last_eval": None, "last_reason": ""},
            3: {"score": 95, "tier": "👑 Admiral", "history": [],
                "last_eval": None, "last_reason": ""},
        }
        self.decisions = {
            "alice": {"choice": "👑 Admiral", "confidence": 0.9},
            "bob": {"choice": "👑 Admiral", "confidence": 0.3},
            "carol": {"choice": "🪵 Landlubber", "confidence": 0.6},
        }
        self.saved = {}

    def tearDown(self):
        memory.MEM_DIR = self._mem_dir
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def _patches(self):
        entries = [
            {"user_id": 1, "name": "alice", "channel": "general", "text": "great post"},
            {"user_id": 2, "name": "bob", "channel": "general", "text": "meh post"},
            {"user_id": 3, "name": "carol", "channel": "general", "text": "fine post"},
            {"user_id": 555, "name": "Inferolli", "channel": "general", "text": "bot line"},
        ]

        async def stm_window(hours):
            return entries

        async def get_rank(uid):
            return copy.deepcopy(self.ranks[uid])

        async def save_rank(uid, r):
            self.saved[uid] = copy.deepcopy(r)

        def _who(state):
            for name in self.decisions:
                if f"member: {name}" in state:
                    return name
            return None

        async def decide(state, questions, timeout=30.0):
            return {"tier": self.decisions[_who(state)], "spark": {"choice": "no"}}

        async def chat_for(job, msgs, **kw):
            return "because reasons"

        async def no_likes(guild, chans, per_channel=60):
            return {}

        async def no_roles(guild):
            return {}

        return [
            mock.patch.object(memory, "stm_window", stm_window),
            mock.patch.object(memory, "get_rank", get_rank),
            mock.patch.object(memory, "save_rank", save_rank),
            mock.patch.object(levels.ai, "decide", decide),
            mock.patch.object(levels.ai, "chat_for", chat_for),
            mock.patch.object(levels, "tally_likes", no_likes),
            mock.patch.object(levels, "ensure_level_roles", no_roles),
        ]

    def test_gates(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            cog = levels.LevelsCog(bot=None)
            guild = FakeGuild()
            result = self.run_async(cog.run_nightly(guild, post=False))
        finally:
            for p in patches:
                p.stop()

        by_name = {s["name"]: s for s in result["standings"]}
        # alice: confident promotion
        self.assertEqual(by_name["alice"]["note"], "promoted")
        self.assertEqual(by_name["alice"]["tier"], "👑 Admiral")
        # bob: same judgment, low confidence → no promotion, score still moves
        self.assertEqual(by_name["bob"]["note"], "")
        self.assertEqual(by_name["bob"]["tier"], "🪵 Landlubber")
        self.assertAlmostEqual(by_name["bob"]["score"], 13.0)
        # carol: confident demotion
        self.assertEqual(by_name["carol"]["note"], "stepped down")
        self.assertEqual(by_name["carol"]["tier"], "🪵 Landlubber")
        self.assertAlmostEqual(by_name["carol"]["score"], 89.0)
        # the bot is never judged
        self.assertNotIn("Inferolli", by_name)
        self.assertEqual(result["judged"], 3)
        # history recorded per member
        self.assertEqual(len(self.saved[1]["history"]), 1)

    def test_scores_never_leave_0_100(self):
        self.ranks[2] = {"score": 0, "tier": "🪵 Landlubber", "history": [],
                         "last_eval": None, "last_reason": ""}
        self.decisions["bob"] = {"choice": "🪵 Landlubber", "confidence": 0.0}
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            cog = levels.LevelsCog(bot=None)
            result = self.run_async(cog.run_nightly(FakeGuild(), post=False))
        finally:
            for p in patches:
                p.stop()
        scores = [s["score"] for s in result["standings"]]
        self.assertTrue(all(0.0 <= s <= 100.0 for s in scores))


async def _fake_cfg(guild_id):
    return {"levels_channel_id": 10}


class BoardTests(unittest.TestCase):
    """The pinned board must live in the BACKED-UP data dir (regression)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._mem_dir = memory.MEM_DIR
        memory.MEM_DIR = Path(self.tmp.name)
        self.chan = FakeChannel(id=10, name="standings")
        self.guild = FakeGuild(channels=[self.chan])

    def tearDown(self):
        memory.MEM_DIR = self._mem_dir
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def _result(self):
        return {"judged": 1, "standings": [
            {"uid": 1, "name": "alice", "tier": "⚓ Sailor", "score": 55.0,
             "prev_tier": "🪝 Deckhand", "note": "", "reason": "", "spark": False,
             "likes": 0, "msgs": 5},
            {"uid": 2, "name": "bob", "tier": "🪵 Landlubber", "score": 0.0,
             "prev_tier": "🪵 Landlubber", "note": "", "reason": "", "spark": True,
             "likes": 0, "msgs": 1},
        ]}

    def test_board_created_then_edited(self):
        with mock.patch.object(levels, "get_cfg", _fake_cfg):
            cog = levels.LevelsCog(bot=None)
            self.run_async(cog.post_standings(self.guild, self._result()))
            state_file = memory.MEM_DIR / "standings.json"
            self.assertTrue(state_file.exists(), "board id must be stored in data/")
            board_id = json.loads(state_file.read_text())[str(self.guild.id)]["board_id"]
            self.assertEqual(len(self.chan.sent), 1)

            # second night: edits the same message instead of pinning a new board
            self.run_async(cog.post_standings(self.guild, self._result()))

        self.assertEqual(len(self.chan.sent), 1)
        self.assertIn("CREW STANDINGS", self.chan._existing[board_id].content)
        self.assertIn("✨", self.chan._existing[board_id].content)   # spark marker

    def test_zero_and_full_scores_render(self):
        res = self._result()
        res["standings"][0]["score"] = 100.0
        res["standings"][1]["score"] = 0.0
        with mock.patch.object(levels, "get_cfg", _fake_cfg):
            self.run_async(levels.LevelsCog(bot=None).post_standings(self.guild, res))
        body = self.chan.sent[0][0]
        self.assertIn("▓" * 8, body)     # full bar
        self.assertIn("░" * 8, body)     # empty bar


if __name__ == "__main__":
    unittest.main()
