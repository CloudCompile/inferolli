"""cogs/computer.py — the newest-first reply parser, auth resolution."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cogs import computer


class ReplyParserTests(unittest.TestCase):
    """The LANTERN bug: one-turn-late replies from a newest-first message list."""

    def msgs(self):
        return [
            {"id": "m3", "type": "assistant",
             "content": [{"type": "text", "text": "turn2 answer"}]},
            {"id": "m2", "type": "user", "text": "turn2 question"},
            {"id": "m1", "type": "assistant",
             "content": [{"type": "text", "text": "turn1 answer"}]},
            {"id": "m0", "type": "user", "text": "turn1 question"},
        ]

    def test_returns_reply_for_this_turn_not_the_previous_one(self):
        self.assertEqual(computer._extract_reply(self.msgs(), "m2"), "turn2 answer")

    def test_first_turn(self):
        # the newest assistant text is the answer worth sending
        self.assertEqual(computer._extract_reply(self.msgs(), "m0"), "turn2 answer")

    def test_no_assistant_yet(self):
        msgs = [{"id": "m1", "type": "user", "text": "q"}]
        self.assertIsNone(computer._extract_reply(msgs, "m1"))

    def test_no_prompt_id_gives_nothing(self):
        self.assertIsNone(computer._extract_reply(self.msgs(), None))

    def test_multipart_content_joined(self):
        msgs = [
            {"id": "a2", "type": "assistant",
             "content": [{"type": "reasoning", "text": "thinking"},
                         {"type": "text", "text": "part one"},
                         {"type": "text", "text": "part two"}]},
            {"id": "u1", "type": "user", "text": "q"},
        ]
        self.assertEqual(computer._extract_reply(msgs, "u1"), "part one\npart two")

    def test_non_dict_entries_ignored(self):
        msgs = ["garbage", {"id": "u1", "type": "user", "text": "q"}]
        self.assertIsNone(computer._extract_reply(msgs, "u1"))

    def test_last_text_wins_over_mid_turn_narration(self):
        # real bug: the agent said "websearch is flaky, falling back to curl"
        # mid-turn and that shipped as the reply instead of the final answer
        msgs = [
            {"type": "idle", "outcome": "succeeded"},
            {"type": "assistant", "content": [
                {"type": "reasoning", "text": "hm"},
                {"type": "text", "text": "the actual answer"}]},
            {"type": "assistant", "content": [
                {"type": "text", "text": "websearch is flaky, falling back to curl"}]},
            {"type": "user", "id": "u9", "text": "q"},
        ]
        self.assertEqual(computer._extract_reply(msgs, "u9"), "the actual answer")


class TurnFinishedTests(unittest.TestCase):
    def test_idle_marker_means_finished(self):
        msgs = [{"type": "idle", "outcome": "succeeded"},
                {"type": "user", "id": "u1", "text": "q"}]
        self.assertTrue(computer._turn_finished(msgs, "u1"))

    def test_no_idle_marker_is_not_finished(self):
        msgs = [{"type": "assistant", "content": [{"type": "text", "text": "half"}]},
                {"type": "user", "id": "u1", "text": "q"}]
        self.assertFalse(computer._turn_finished(msgs, "u1"))

    def test_empty_list_is_not_finished(self):
        self.assertFalse(computer._turn_finished([], "u1"))
        self.assertFalse(computer._turn_finished([], None))



class AuthTests(unittest.TestCase):
    def test_server_password_parsed_from_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "serve.log"
            log.write_text("server listening on http://127.0.0.1:4096\n"
                           "server password AbC_123-xyz\n")
            self.assertEqual(computer._server_password(str(log)), "AbC_123-xyz")

    def test_missing_log_is_none(self):
        self.assertIsNone(computer._server_password("/nonexistent/nowhere.log"))

    def test_env_password_wins(self):
        with mock.patch.dict(os.environ, {"OPENCODE_SERVER_PASSWORD": "envpw"}):
            self.assertEqual(computer._auth(), ("opencode", "envpw"))

    def test_falls_back_to_log_then_default(self):
        with mock.patch.dict(os.environ, {"OPENCODE_SERVER_PASSWORD": ""}), \
             mock.patch.object(computer, "_server_password", lambda *a: "logpw"):
            self.assertEqual(computer._auth(), ("opencode", "logpw"))
        with mock.patch.dict(os.environ, {"OPENCODE_SERVER_PASSWORD": ""}), \
             mock.patch.object(computer, "_server_password", lambda *a: None):
            self.assertEqual(computer._auth(), ("opencode", "inferolli-local-4096"))


if __name__ == "__main__":
    unittest.main()
