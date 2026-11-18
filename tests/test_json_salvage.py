"""ai.py — truncated-JSON salvage.

A model that hits its token limit mid-object returns JSON that won't parse. In
production this silently zeroed the channel-topic map: one truncated write and
the whole response was discarded. Complete entries are worth keeping.
"""

import asyncio
import unittest
from unittest import mock

import ai


class SalvageTests(unittest.TestCase):
    def test_keeps_complete_entries_from_a_truncated_array(self):
        text = '{"channels": [{"id": "a", "topic": "one"}, {"id": "b", "topic": "two"}, {"id": "c", "top'
        self.assertEqual(ai._salvage_truncated_json(text),
                         {"channels": [{"id": "a", "topic": "one"},
                                       {"id": "b", "topic": "two"}]})

    def test_single_complete_entry(self):
        self.assertEqual(ai._salvage_truncated_json('{"x": [{"a": 1}, {"b": '),
                         {"x": [{"a": 1}]})

    def test_clean_json_passes_through(self):
        self.assertEqual(ai._salvage_truncated_json('{"x": [{"a": 1}]}'), {"x": [{"a": 1}]})

    def test_no_array_returns_none(self):
        for text in ("not json at all", "", '{"a": "b"}', '["bare", "array"]'):
            self.assertIsNone(ai._salvage_truncated_json(text), text)

    def test_unparseable_fragment_returns_none(self):
        self.assertIsNone(ai._salvage_truncated_json('{"x": [{"a": '))


class ChatForJsonTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_truncated_json_is_salvaged_not_raised(self):
        async def fake_chat(*a, **k):
            return '{"channels": [{"id": "a", "topic": "one"}, {"id": "b", "to'

        with mock.patch.object(ai, "chat", fake_chat):
            out = self.run_async(ai.chat_for("dream", [], json_out=True))
        self.assertEqual(out, {"channels": [{"id": "a", "topic": "one"}]})

    def test_unrecoverable_json_still_raises(self):
        async def fake_chat(*a, **k):
            return "total nonsense"

        with mock.patch.object(ai, "chat", fake_chat):
            with self.assertRaises(ai.AIError):
                self.run_async(ai.chat_for("dream", [], json_out=True))

    def test_fenced_json_still_parses(self):
        async def fake_chat(*a, **k):
            return '```json\n{"reply": "hi"}\n```'

        with mock.patch.object(ai, "chat", fake_chat):
            out = self.run_async(ai.chat_for("reply", [], json_out=True))
        self.assertEqual(out, {"reply": "hi"})


if __name__ == "__main__":
    unittest.main()
