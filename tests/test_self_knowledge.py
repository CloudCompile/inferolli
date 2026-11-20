"""Does the bot know what it IS and what it can DO?

The failure this pins, verbatim from #casual:

    Christopher: Clone the code and look through it
    Inferolli:   i'm on the same side of the screen as you, mate — i can't clone
                 stuff or read my own source.

    Christopher: use your computer use to open it and send me a screenshot
    Inferolli:   no chrome, no screenshots — i'm a guy in the channel, not a
                 remote desktop.

Every word of that is false: it runs in a sandbox with bash, git, chromium and a
live :99 desktop, and /bot is its own source tree. It had already been told all
of that in DESKTOP_BLOCK — which the reply prompt cut off, because oc_ask
truncated at 6000 chars and the instructions were appended AFTER the memory and
server-pulse blocks.
"""

import unittest
from unittest import mock

import cogs.companion as companion
from cogs import computer


class SelfKnowledgeTests(unittest.TestCase):
    def test_it_is_told_where_its_own_source_is(self):
        block = companion.SELF_BLOCK
        self.assertIn("/bot", block)
        for tool in ("bash", "git", "chromium", "grep"):
            self.assertIn(tool, block)

    def test_it_is_forbidden_to_claim_helplessness_without_trying(self):
        block = companion.SELF_BLOCK.lower()
        self.assertIn("without trying at least one command", block)
        self.assertIn("never lie about what you can do", block)

    def test_the_persona_does_not_forbid_being_software(self):
        """'Never announce being an AI or a bot' is what made it invent a fake
        alibi ('i can't clone stuff') instead of just answering."""
        prompt = companion._persona_prompt({"name": "Inferolli", "opinions": []})
        self.assertNotIn("Never announce being an AI or a bot", prompt)
        self.assertIn("never lie about being software", prompt)

    def test_the_desktop_block_still_promises_it_can_act(self):
        self.assertIn("Never say you can't", companion.DESKTOP_BLOCK)


class PromptTruncationTests(unittest.TestCase):
    def test_prompt_cap_fits_the_instructions(self):
        """6000 was smaller than one busy server's context block."""
        self.assertGreaterEqual(computer.OC_PROMPT_MAX, 30000)

    def test_a_huge_prompt_is_announced(self):
        sent = {}

        class FakeResp:
            status_code = 200
            json = staticmethod(lambda: {"data": {"id": "m1"}})
            text = ""

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                sent["text"] = json["text"]
                return FakeResp()

            async def get(self, url, auth=None):
                class R:
                    status_code = 200
                    json = staticmethod(lambda: {"data": []})
                return R()

        import asyncio
        with mock.patch.object(computer, "_httpx") as hx, \
             mock.patch.object(computer, "_auth", lambda: None), \
             mock.patch.object(computer.asyncio, "sleep", mock.AsyncMock()), \
             mock.patch.object(computer, "OC_PROMPT_MAX", 100), \
             self.assertLogs("cogs.computer", level="WARNING") as logs:
            hx.AsyncClient = lambda **kw: FakeClient()
            asyncio.run(computer.oc_ask("ses1", "x" * 500, timeout=0.01))

        self.assertEqual(len(sent["text"]), 100)
        self.assertIn("truncating", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
