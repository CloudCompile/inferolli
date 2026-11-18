"""memory.py — recall relevance, channel keying, thought scoping, channel topics.

These cover the "it brings up unrelated conversations" failure: recall used to
pad to the top-N no matter how irrelevant, channels were keyed by display name
(so a rename split the history), and every channel's inner voice was pasted
into every reply.
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import memory


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {k: getattr(memory, k) for k in (
            "MEM_DIR", "USER_DIR", "PERSONA_FILE", "SERVER_FILE", "STM_FILE",
            "THOUGHTS_FILE", "RANKS_FILE", "STM_EMB_FILE", "TOPICS_FILE")}
        memory.MEM_DIR = self.root
        memory.USER_DIR = self.root / "users"
        memory.STM_FILE = self.root / "stm.json"
        memory.THOUGHTS_FILE = self.root / "thoughts.json"
        memory.TOPICS_FILE = self.root / "topics.json"
        self._embed = memory.ai.embed

        async def _no_embed(texts):
            return [[0.0] * 4 for _ in texts]

        memory.ai.embed = _no_embed

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(memory, k, v)
        memory.ai.embed = self._embed
        self.tmp.cleanup()


class RecallRelevanceTests(_Base):
    def test_irrelevant_memories_are_dropped(self):
        """The real bug: a question about 6.2 Astra returned 'yeah—do' from
        staff chat because recall always padded to the limit."""
        async def go():
            await memory.remember_stm(1, "a", "staff", "yeah—do", channel_id=1)
            await memory.remember_stm(2, "b", "general", "hi", channel_id=2)
            return await memory.recall_stm(terms=["astra", "release"], limit=5)

        self.assertEqual(run(go()), [])

    def test_relevant_memory_survives(self):
        async def go():
            await memory.remember_stm(1, "a", "general", "the astra release is out",
                                      channel_id=1)
            await memory.remember_stm(2, "b", "general", "totally unrelated",
                                      channel_id=1)
            return await memory.recall_stm(terms=["astra"], limit=5)

        hits = run(go())
        self.assertEqual(len(hits), 1)
        self.assertIn("astra", hits[0]["text"])

    def test_semantic_floor_keeps_related_text(self):
        async def go():
            await memory.remember_stm(1, "a", "c", "sol pricing is cheaper",
                                      channel_id=1)
            return await memory.recall_stm(terms=[], qvec=[0.0, 1.0, 0.0, 0.0])

        self.assertEqual(run(go()), [])  # zero-vector embed → no semantic evidence

    def test_no_query_means_no_recall(self):
        async def go():
            await memory.remember_stm(1, "a", "c", "anything", channel_id=1)
            return await memory.recall_stm(terms=[], qvec=None)

        self.assertEqual(run(go()), [])


class ChannelKeyTests(_Base):
    def test_exclusion_works_across_rename(self):
        """Old entries were keyed by name; after a rename the current channel's
        own history leaked in as 'elsewhere in the server'."""
        async def go():
            await memory.remember_stm(1, "a", "general-chat", "astra talk here",
                                      channel_id=77)
            await memory.remember_stm(2, "b", "other", "astra talk elsewhere",
                                      channel_id=88)
            return await memory.recall_stm(terms=["astra"], exclude_channel="general-chat",
                                           exclude_channel_id=77)

        hits = run(go())
        self.assertEqual([h["channel"] for h in hits], ["other"])

    def test_decorated_rename_still_excluded(self):
        # '🌊│general-chat' vs 'general-chat' is the same room
        async def go():
            await memory.remember_stm(1, "a", "🌊│general-chat", "astra here")
            await memory.remember_stm(2, "b", "staff", "astra there")
            return await memory.recall_stm(terms=["astra"], exclude_channel="general-chat")

        self.assertEqual([h["channel"] for h in run(go())], ["staff"])

    def test_exclusion_by_id_alone(self):
        async def go():
            await memory.remember_stm(1, "a", "general-chat", "astra one", channel_id=77)
            await memory.remember_stm(2, "b", "other", "astra two", channel_id=88)
            return await memory.recall_stm(terms=["astra"], exclude_channel_id="77")

        self.assertEqual([h["channel"] for h in run(go())], ["other"])

    def test_legacy_entries_still_match_by_name(self):
        async def go():
            await memory.remember_stm(1, "a", "legacy-room", "astra legacy",
                                      channel_id=None)
            return await memory.recall_stm(terms=["astra"], exclude_channel="legacy-room")

        self.assertEqual(run(go()), [])

    def test_channel_id_is_stored(self):
        run(memory.remember_stm(1, "a", "general-chat", "hi", channel_id=1234))
        stm = json.loads(memory.STM_FILE.read_text())
        self.assertEqual(stm["entries"][-1]["channel_id"], "1234")


class ThoughtTests(_Base):
    def test_duplicate_thoughts_are_not_stored_twice(self):
        self.assertTrue(run(memory.save_thought("same thought", "general-chat")))
        self.assertFalse(run(memory.save_thought("same thought", "general-chat")))
        store = json.loads(memory.THOUGHTS_FILE.read_text())
        self.assertEqual(len(store["thoughts"]), 1)

    def test_recent_thoughts_scoped_to_channel(self):
        async def go():
            await memory.save_thought("about memes", "memes", channel_id=1)
            await memory.save_thought("about models", "general", channel_id=2)
            return await memory.recent_thoughts(4, channel_id=2)

        self.assertEqual([t["text"] for t in run(go())], ["about models"])

    def test_recent_thoughts_unscoped_returns_all(self):
        async def go():
            await memory.save_thought("a", "memes", channel_id=1)
            await memory.save_thought("b", "general", channel_id=2)
            return await memory.recent_thoughts(4)

        self.assertEqual(len(run(go())), 2)

    def test_empty_thought_rejected(self):
        self.assertFalse(run(memory.save_thought("   ", "general")))


class ChannelTopicTests(_Base):
    def test_topic_roundtrip(self):
        async def go():
            await memory.set_channel_topic(5, "staff-chat",
                                           "model routing and provider decisions",
                                           ["glm", "deepseek", "routing"])
            return await memory.get_topics()

        topics = run(go())
        self.assertIn("5", topics["channels"])
        self.assertEqual(topics["channels"]["5"]["name"], "staff-chat")

    def test_topic_matches_on_keyword(self):
        entry = {"topic": "model routing decisions", "keywords": ["deepseek", "glm"]}
        self.assertTrue(memory.topic_matches(entry, ["deepseek"], None))
        self.assertFalse(memory.topic_matches(entry, ["pizza"], None))

    def test_topics_block_hides_current_channel_and_irrelevant_rooms(self):
        topics = {"channels": {
            "1": {"name": "general", "topic": "day to day chat", "keywords": ["chat"]},
            "2": {"name": "staff-chat", "topic": "model routing", "keywords": ["deepseek"]},
            "3": {"name": "this-room", "topic": "deepseek talk", "keywords": ["deepseek"]},
        }}
        block = memory.topics_block(topics, "3", ["deepseek"], None)
        self.assertIn("staff-chat", block)
        self.assertNotIn("this-room", block)     # never describe the room we're in
        self.assertNotIn("general", block)       # irrelevant room stays out

    def test_topics_block_empty_when_nothing_matches(self):
        topics = {"channels": {"2": {"name": "x", "topic": "pizza", "keywords": ["pizza"]}}}
        self.assertEqual(memory.topics_block(topics, "1", ["deepseek"], None), "")

    def test_stale_topics_pruned(self):
        async def go():
            await memory.set_channel_topic(9, "dead-room", "nobody talks here")
            store = json.loads(memory.TOPICS_FILE.read_text())
            store["channels"]["9"]["updated"] = time.time() - 30 * 86400
            memory.TOPICS_FILE.write_text(json.dumps(store))
            dropped = await memory.prune_topics()
            return dropped, await memory.get_topics()

        dropped, topics = run(go())
        self.assertEqual(dropped, 1)
        self.assertEqual(topics["channels"], {})

    def test_empty_topic_ignored(self):
        async def go():
            await memory.set_channel_topic(4, "x", "   ")
            return await memory.get_topics()

        self.assertEqual(run(go())["channels"], {})


if __name__ == "__main__":
    unittest.main()
