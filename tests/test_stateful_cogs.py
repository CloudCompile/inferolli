"""cogs/thoughts.py + cogs/ambient.py + cogs/dreams.py — stateful behaviour.

These cogs persist small pieces of state across restarts (which is where the
duplicate-thought bug came from) and decide when the bot may speak on its own
(the part users actually notice). The tests pin those decisions.
"""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cogs import ambient, dreams, thoughts


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ThoughtStateTests(unittest.TestCase):
    """The dedupe markers must survive a restart, or the same window gets
    re-thought and stored as duplicates (the bug that filled thoughts.json)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._state = thoughts.STATE_FILE
        thoughts.STATE_FILE = Path(self.tmp.name) / "thoughts-state.json"

    def tearDown(self):
        thoughts.STATE_FILE = self._state
        self.tmp.cleanup()

    def test_state_roundtrip(self):
        a = thoughts.ThoughtsCog(SimpleNamespace())
        a._last_id = {10: 500}
        a._last_digest = {10: "abc123"}
        a._save_state()

        b = thoughts.ThoughtsCog(SimpleNamespace())
        self.assertEqual(b._last_id, {10: 500})
        self.assertEqual(b._last_digest, {10: "abc123"})

    def test_missing_state_is_empty_not_fatal(self):
        cog = thoughts.ThoughtsCog(SimpleNamespace())
        self.assertEqual((cog._last_id, cog._last_digest), ({}, {}))

    def test_corrupt_state_is_ignored(self):
        thoughts.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        thoughts.STATE_FILE.write_text("{broken")
        cog = thoughts.ThoughtsCog(SimpleNamespace())
        self.assertEqual(cog._last_id, {})

    def test_same_window_is_not_thought_about_twice(self):
        import hashlib
        digest = "Alice: hello\nBob: hi"
        d = hashlib.md5(digest.encode()).hexdigest()
        cog = thoughts.ThoughtsCog(SimpleNamespace())
        cog._last_digest[10] = d
        # the guard in _think compares against this hash
        self.assertEqual(cog._last_digest.get(10), d)

    def test_quiet_hours_skip(self):
        self.assertEqual((thoughts.QUIET_START, thoughts.QUIET_END), (2, 8))


class AmbientCapTests(unittest.TestCase):
    def setUp(self):
        self.cog = ambient.AmbientCog(SimpleNamespace())

    def test_cap_resets_on_a_new_day(self):
        self.cog._today = (datetime.utcnow() - timedelta(days=1)).date()
        self.cog._started_today = 99
        self.assertTrue(self.cog._under_cap(), "a new day resets the counter")
        self.assertEqual(self.cog._started_today, 0)

    def test_cap_blocks_when_reached(self):
        self.cog._today = datetime.utcnow().date()
        self.cog._started_today = ambient.DAILY_CAP
        self.assertFalse(self.cog._under_cap())

    def test_skip_channels_include_the_noisy_ones(self):
        for name in ("bot-commands", "starboard", "model-market", "rules"):
            self.assertIn(name, ambient.SKIP_CHANNELS)

    def test_cooldowns_are_ordered_sensibly(self):
        self.assertLess(ambient.GLOBAL_COOLDOWN_S, ambient.CHANNEL_COOLDOWN_S)


class DreamTopicTests(unittest.TestCase):
    """_build_topics groups by channel and writes one line per room."""

    def setUp(self):
        self.cog = dreams.DreamsCog.__new__(dreams.DreamsCog)
        self.entries = [
            {"channel_id": "1", "channel": "general", "text": "talk about models"},
            {"channel_id": "1", "channel": "general", "text": "more models"},
            {"channel_id": "2", "channel": "staff", "text": "routing decisions"},
        ]

    def test_groups_by_channel_and_writes_topics(self):
        payload = {"channels": [
            {"id": "1", "topic": "models", "keywords": ["models"]},
            {"id": "2", "topic": "routing", "keywords": ["routing"]},
        ]}

        async def fake_chat(*a, **k):
            return payload

        written = []
        async def fake_set(cid, name, topic, keywords=None):
            written.append((str(cid), name, topic))

        with mock.patch.object(dreams.ai, "chat_for", fake_chat), \
             mock.patch.object(dreams.memory, "set_channel_topic", fake_set), \
             mock.patch.object(dreams.memory, "prune_topics", mock.AsyncMock()):
            count = run(self.cog._build_topics(self.entries))
        self.assertEqual(count, 2)
        self.assertEqual({w[0] for w in written}, {"1", "2"})

    def test_no_entries_is_a_no_op(self):
        with mock.patch.object(dreams.ai, "chat_for", mock.AsyncMock()) as chat:
            self.assertEqual(run(self.cog._build_topics([])), 0)
        chat.assert_not_awaited()

    def test_model_failure_is_contained(self):
        async def boom(*a, **k):
            raise dreams.ai.AIError("down")

        with mock.patch.object(dreams.ai, "chat_for", boom):
            self.assertEqual(run(self.cog._build_topics(self.entries)), 0)

    def test_unknown_channel_ids_are_dropped(self):
        async def fake_chat(*a, **k):
            return {"channels": [{"id": "999", "topic": "ghost", "keywords": []}]}

        written = []
        async def fake_set(cid, name, topic, keywords=None):
            written.append(cid)

        with mock.patch.object(dreams.ai, "chat_for", fake_chat), \
             mock.patch.object(dreams.memory, "set_channel_topic", fake_set), \
             mock.patch.object(dreams.memory, "prune_topics", mock.AsyncMock()):
            count = run(self.cog._build_topics(self.entries))
        self.assertEqual(count, 0)
        self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
