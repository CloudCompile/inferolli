"""tools_server — recovering tool calls the model wraps in XML-ish tags.

Production failure this covers: instead of the JSON envelope, the model replied

    <tool calls>
      {"tool": "channel_history", "args": {"channel": "#📣│announcements", "n": 10}}
    </tool calls>

The ol' code raised AIError, the reply path called it a provider outage, and the
user got "my head's not answering right now — the providers are wedged". The
provider was fine; we just refused to read the intent.
"""

import asyncio
import json
import unittest
from unittest import mock

import tools_server


def run(coro):
    return asyncio.run(coro)


class UnwrapTaggedToolCallTests(unittest.TestCase):
    def test_tag_wrapped_tool_call_is_recovered(self):
        raw = ('<tool calls>\n'
               '  {"tool": "channel_history", "args": {"channel": "#general", "n": 10}}\n'
               '</tool calls>')
        got = tools_server.parse_envelope(raw)
        self.assertEqual(got["tool"], "channel_history")
        self.assertEqual(got["args"]["channel"], "#general")

    def test_tag_wrapped_reply_is_recovered(self):
        raw = "<response>\n{\"reply\": \"hello\", \"actions\": []}\n</response>"
        got = tools_server.parse_envelope(raw)
        self.assertEqual(got["reply"], "hello")

    def test_plain_json_still_parses(self):
        got = tools_server.parse_envelope('{"reply": "hi", "actions": []}')
        self.assertEqual(got, {"reply": "hi", "actions": []})

    def test_fenced_json_still_parses(self):
        got = tools_server.parse_envelope('```json\n{"reply": "hi"}\n```')
        self.assertEqual(got["reply"], "hi")

    def test_nested_args_inside_tags(self):
        raw = ('<tool calls><tool name="discord_search">'
               '{"tool": "discord_search", "args": {"query": "astra", "n": 5}}'
               '</tool></tool calls>')
        got = tools_server.parse_envelope(raw)
        self.assertEqual(got["tool"], "discord_search")

    def test_prose_is_not_an_envelope(self):
        self.assertIsNone(tools_server.parse_envelope("just a normal sentence"))
        self.assertIsNone(tools_server.parse_envelope(""))
        self.assertIsNone(tools_server.parse_envelope('{"unrelated": true}'))

    def test_unclosed_tag_is_still_recovered(self):
        raw = '<tool calls> {"tool": "web_search", "args": {"query": "x"}}'
        self.assertEqual(tools_server.parse_envelope(raw)["tool"], "web_search")


class RunLoopResilienceTests(unittest.TestCase):
    def _patch(self, *responses):
        queue = [r if isinstance(r, str) else json.dumps(r) for r in responses]

        async def fake(job, messages, **kw):
            return queue.pop(0)

        return mock.patch.object(tools_server.ai, "chat_for", fake)

    def test_tagged_tool_call_runs_instead_of_failing(self):
        with self._patch('<tool calls>{"tool": "channel_history", "args": {"channel": "#x"}}</tool calls>',
                         {"reply": "done", "actions": []}):
            out = run(tools_server.run_loop(None, "SYS", "hi"))
        self.assertEqual(out["reply"], "done")

    def test_plain_text_becomes_the_reply_not_an_error(self):
        """The model answering in prose must not be reported as an outage."""
        with self._patch("hey christopher, the tonearm thing is still queued"):
            out = run(tools_server.run_loop(None, "SYS", "hi"))
        self.assertIn("tonearm", out["reply"])

    def test_prompt_forbids_xml_wrapping(self):
        self.assertIn("no XML", tools_server.TOOLS_PROMPT)
        self.assertIn("<tool calls>", tools_server.TOOLS_PROMPT)


if __name__ == "__main__":
    unittest.main()
