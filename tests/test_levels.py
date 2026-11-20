"""levels.py — XP ladder math, spark-only nightly, standings board persistence."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory
from cogs import levels
from tests.fakes import FakeChannel, FakeGuild, FakeMember, FakeMessage


class TierTests(unittest.TestCase):
    def test_xp_thresholds(self):
        cases = {0: "🪵 Landlubber", 99: "🪵 Landlubber", 100: "🪝 Deckhand",
                 250: "🚢 Crew", 500: "⚓ Sailor", 1000: "🧭 Navigator",
                 1750: "🔬 Research Diver", 2750: "⛵ Bosun",
                 4000: "🎖️ First Mate", 6000: "🚩 Captain",
                 9000: "👑 Admiral", 25000: "👑 Admiral"}
        for xp, tier in cases.items():
            self.assertEqual(memory.tier_for_xp(xp), tier, xp)

    def test_the_ladder_absorbed_the_strays(self):
        """The old stray rank roles are now ladder rungs — one ladder, no
        duplicates pretending to be ranks."""
        names = [t[0] for t in memory.TIERS]
        self.assertIn("🚢 Crew", names)
        self.assertIn("🔬 Research Diver", names)
        self.assertIn("🎖️ First Mate", names)
        self.assertIn("🚩 Captain", names)
        # the old names they carried are renamed away, not left behind
        self.assertNotIn("🧭 First Mate", names)
        self.assertNotIn("⚓ Captain", names)
        self.assertEqual(levels.LADDER_RENAMES["🧭 First Mate"], "🎖️ First Mate")
        self.assertEqual(levels.LADDER_RENAMES["⚓ Captain"], "🚩 Captain")

    def test_progress_bar(self):
        self.assertEqual(levels._progress_bar(0), "░" * 8)
        self.assertEqual(levels._progress_bar(50), "▓" * 4 + "░" * 4)
        self.assertEqual(levels._progress_bar(100), "░" * 8)  # just reached Deckhand
        self.assertEqual(levels._progress_bar(175), "▓" * 4 + "░" * 4)
        self.assertEqual(levels._progress_bar(9000), "▓" * 8)  # top of the ladder
        self.assertNotIn("░", levels._progress_bar(20000))

    def test_xp_to_next(self):
        self.assertEqual(levels.xp_to_next(90), "10 XP to 🪝 Deckhand")
        self.assertIn("top of the ladder", levels.xp_to_next(9000))


class AwardTests(unittest.TestCase):
    """The XP engine: deterministic awards, tier pinned on change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._mem_dir = memory.MEM_DIR
        self._ranks_file = memory.RANKS_FILE
        memory.MEM_DIR = Path(self.tmp.name)
        # RANKS_FILE is bound at import time — repoint it too
        memory.RANKS_FILE = memory.MEM_DIR / "ranks.json"

    def tearDown(self):
        memory.MEM_DIR = self._mem_dir
        memory.RANKS_FILE = self._ranks_file
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def _award(self, uid, amount):
        async def no_roles(guild):
            return {}
        with mock.patch.object(levels, "ensure_level_roles", no_roles):
            return self.run_async(levels.LevelsCog(bot=None)._award(
                FakeGuild(), uid, amount))

    def test_awards_stack_and_promote(self):
        # award #1: Landlubber
        self.assertIsNone(self._award(1, levels.XP_PER_MESSAGE))
        # awards #2..#19: still Landlubber at 95 XP
        for _ in range(18):
            self.assertIsNone(self._award(1, levels.XP_PER_MESSAGE))
        # award #20: 100 XP → Deckhand
        self.assertEqual(self._award(1, levels.XP_PER_MESSAGE), "🪝 Deckhand")
        r = self.run_async(memory.get_rank(1))
        self.assertEqual(r["tier"], "🪝 Deckhand")
        self.assertAlmostEqual(r["xp"], 100)

    def test_award_never_demotes(self):
        # seed someone at Captain
        self.run_async(memory.save_rank(9, dict(
            self.run_async(memory.get_rank(9)), xp=6000, tier="🚩 Captain")))
        # a negative award is refused outright — demotion is structurally
        # impossible, not just untested
        self.assertIsNone(self._award(9, -5))
        r = self.run_async(memory.get_rank(9))
        self.assertEqual(r["tier"], "🚩 Captain")
        self.assertAlmostEqual(r["xp"], 6000)

    def test_message_cooldown(self):
        cog = levels.LevelsCog(bot=None)
        guild = FakeGuild()
        member = FakeMember(id=5)
        msg = FakeMessage(id=1, content="hello")
        msg.author, msg.guild = member, guild

        async def award(*args):
            awards.append(args[-1])
        awards = []
        with mock.patch.object(levels.LevelsCog, "_award", award):
            self.run_async(cog.on_message(msg))
            self.run_async(cog.on_message(msg))
            self.run_async(cog.on_message(msg))
        self.assertEqual(awards, [levels.XP_PER_MESSAGE])   # once per minute

    def test_reaction_pays_the_author(self):
        cog = levels.LevelsCog(bot=None)
        guild = FakeGuild()
        author = FakeMember(id=7)
        msg = FakeMessage(id=1, content="great post")
        msg.author, msg.guild = author, guild

        async def award(*args):
            awards.append((args[-2], args[-1]))
        awards = []
        reactor = FakeMember(id=8)
        with mock.patch.object(levels.LevelsCog, "_award", award):
            self.run_async(cog.on_reaction_add(
                mock.Mock(message=msg), reactor))
            # self-reacting doesn't pay
            self.run_async(cog.on_reaction_add(
                mock.Mock(message=msg), author))
            # bots don't pay
            self.run_async(cog.on_reaction_add(
                mock.Mock(message=msg), FakeMember(id=9, bot=True)))
        self.assertEqual(awards, [(7, levels.XP_PER_REACTION)])

    def test_staff_adjust_can_add_and_subtract(self):
        """The /xp hand wheel: staff CAN move XP down (unlike the engine,
        which never demotes) — and every hand move is logged in history."""
        cog = levels.LevelsCog(bot=None)

        async def no_roles(guild):
            return {}
        with mock.patch.object(levels, "ensure_level_roles", no_roles), \
             mock.patch.object(levels, "sync_member_tier", mock.AsyncMock()):
            # add across a threshold: 0 → 120 = Deckhand
            r = self.run_async(cog.adjust_xp(FakeGuild(), 3, 120, by_name="cj"))
            self.assertEqual(r["tier"], "🪝 Deckhand")
            self.assertAlmostEqual(r["xp"], 120)
            # subtract back below it: Deckhand 120 → 70 = Landlubber
            r = self.run_async(cog.adjust_xp(FakeGuild(), 3, -50, by_name="cj"))
            self.assertEqual(r["tier"], "🪵 Landlubber")
            self.assertAlmostEqual(r["xp"], 70)
            # the ledger records both hand moves
            notes = [h["note"] for h in r["history"]]
            self.assertEqual(notes.count("hand"), 2)
            self.assertIn("cj", r["history"][-1]["reason"])

    def test_staff_adjust_clamps_at_zero(self):
        cog = levels.LevelsCog(bot=None)

        async def no_roles(guild):
            return {}
        with mock.patch.object(levels, "ensure_level_roles", no_roles), \
             mock.patch.object(levels, "sync_member_tier", mock.AsyncMock()):
            r = self.run_async(cog.adjust_xp(FakeGuild(), 4, -5000, by_name="cj"))
            self.assertEqual(r["xp"], 0.0)
            self.assertEqual(r["tier"], memory.TIERS[0][0])

    def test_staff_adjust_syncs_roles_on_tier_change(self):
        cog = levels.LevelsCog(bot=None)
        synced = []

        async def fake_roles(guild):
            return {"🪝 Deckhand": object()}
        with mock.patch.object(levels, "ensure_level_roles", fake_roles), \
             mock.patch.object(levels, "sync_member_tier",
                               mock.AsyncMock(side_effect=lambda g, u, t, a: synced.append(t))):
            self.run_async(cog.adjust_xp(FakeGuild(), 5, 200, by_name="cj"))
            self.assertEqual(synced, ["🪝 Deckhand"])
            # a move that doesn't cross a rung doesn't re-sync
            self.run_async(cog.adjust_xp(FakeGuild(), 5, 10, by_name="cj"))
            self.assertEqual(synced, ["🪝 Deckhand"])


class NightlyTests(unittest.TestCase):
    """run_nightly: the model only hands spark bonuses — no demotions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._mem_dir = memory.MEM_DIR
        memory.MEM_DIR = Path(self.tmp.name)
        self.ranks = {
            1: {"xp": 980, "tier": "⚓ Sailor", "history": [],
                "last_eval": None, "last_reason": ""},   # one spark from Navigator
            2: {"xp": 90, "tier": "🪵 Landlubber", "history": [],
                "last_eval": None, "last_reason": ""},
        }
        self.decisions = {
            "alice": {"choice": "yes", "confidence": 0.9},
            "bob": {"choice": "yes", "confidence": 0.4},   # low confidence → no spark
        }

    def tearDown(self):
        memory.MEM_DIR = self._mem_dir
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def _patches(self):
        entries = [
            {"user_id": 1, "name": "alice", "channel": "general", "text": "great post"},
            {"user_id": 2, "name": "bob", "channel": "general", "text": "meh post"},
            {"user_id": 555, "name": "Inferolli", "channel": "general", "text": "bot line"},
        ]

        async def stm_window(hours):
            return entries

        async def get_rank(uid):
            return copy.deepcopy(self.ranks[uid])

        async def save_rank(uid, r):
            self.ranks[uid] = copy.deepcopy(r)

        def _who(state):
            for name in self.decisions:
                if f"member: {name}" in state:
                    return name
            return None

        async def decide(state, questions, timeout=30.0):
            return {"spark": self.decisions[_who(state)]}

        async def chat_for(job, msgs, **kw):
            return "she taught the bot to sail"

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
            mock.patch.object(levels, "sync_member_tier", mock.AsyncMock()),
        ]

    def test_sparks_only(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            cog = levels.LevelsCog(bot=None)
            result = self.run_async(cog.run_nightly(FakeGuild(), post=False))
        finally:
            for p in patches:
                p.stop()

        by_name = {s["name"]: s for s in result["standings"]}
        # alice: confident spark → +25 XP, citation, and a promotion crossing 1000
        self.assertEqual(by_name["alice"]["note"], "spark")
        self.assertAlmostEqual(self.ranks[1]["xp"], 980 + levels.SPARK_XP)
        self.assertEqual(by_name["alice"]["tier"], "🧭 Navigator")
        # bob: spark judgment but low confidence → nothing added
        self.assertEqual(by_name["bob"]["note"], "")
        self.assertAlmostEqual(self.ranks[2]["xp"], 90)
        # the bot is never judged
        self.assertNotIn("Inferolli", by_name)
        self.assertEqual(result["judged"], 2)
        # spark history recorded
        self.assertEqual(self.ranks[1]["history"][-1]["note"], "spark")

    def test_no_demotion_is_possible(self):
        """No code path in the nightly pass lowers XP or tier; a negative
        award is refused outright."""
        src = Path("cogs/levels.py").read_text()
        run_nightly = src[src.index("async def run_nightly"):src.index("async def post_standings")]
        self.assertNotIn("- SPARK_XP", run_nightly)
        self.assertNotIn("stepped down", run_nightly)


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
        return {"judged": 2, "standings": [
            {"uid": 1, "name": "alice", "tier": "⚓ Sailor", "xp": 520.0,
             "prev_tier": "🚢 Crew", "note": "", "reason": "", "spark": False,
             "likes": 0, "msgs": 5},
            {"uid": 2, "name": "bob", "tier": "🪵 Landlubber", "xp": 10.0,
             "prev_tier": "🪵 Landlubber", "note": "spark", "reason": "taught the bot to sail",
             "spark": True, "likes": 0, "msgs": 1},
        ]}

    def test_board_created_then_edited(self):
        # mention_for is imported inside the moves branch — patch at source
        import cogs.pings as pings

        async def mention_for(guild, topic):
            return {"mention": "", "allowed": None}

        with mock.patch.object(levels, "get_cfg", _fake_cfg), \
             mock.patch.object(pings, "mention_for", mention_for):
            cog = levels.LevelsCog(bot=None)
            self.run_async(cog.post_standings(self.guild, self._result()))
            state_file = memory.MEM_DIR / "standings.json"
            self.assertTrue(state_file.exists(), "board id must be stored in data/")
            board_id = json.loads(state_file.read_text())[str(self.guild.id)]["board_id"]

            # second night: edits the same message instead of pinning a new board
            self.run_async(cog.post_standings(self.guild, self._result()))

        board = self.chan._existing[board_id].content
        self.assertIn("CREW STANDINGS", board)
        self.assertIn("✨", board)            # spark marker
        self.assertIn("XP", board)           # the new currency is on the board
        # board + moves per night; the second board is an EDIT, not a new send
        self.assertEqual(len(self.chan.sent), 3)

    def test_moves_message_names_the_spark(self):
        sent = []

        async def mention_for(guild, topic):
            return {"mention": "", "allowed": None}

        async def send(content=None, **kw):
            sent.append(content)
            return FakeMessage()

        real_send = self.chan.send
        # mention_for is imported inside the method — patch at its source
        import cogs.pings as pings
        with mock.patch.object(levels, "get_cfg", _fake_cfg), \
             mock.patch.object(pings, "mention_for", mention_for):
            self.chan.send = send
            try:
                cog = levels.LevelsCog(bot=None)
                self.run_async(cog.post_standings(self.guild, self._result()))
            finally:
                self.chan.send = real_send
        self.assertTrue(sent, "messages went out")
        self.assertEqual(len(sent), 2)          # board + moves
        self.assertIn("✨ **bob**", sent[1])
        self.assertIn("taught the bot to sail", sent[1])


if __name__ == "__main__":
    unittest.main()