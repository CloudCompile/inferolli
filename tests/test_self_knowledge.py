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

import pathlib
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

    def test_it_sees_both_copies_of_its_source(self):
        """The running copy (/bot) and the versioned repo clone
        (/workspace/inferolli) are different things with different powers."""
        block = companion.SELF_BLOCK
        self.assertIn("/workspace/inferolli", block)
        self.assertIn("log --oneline", block)
        self.assertIn("git blame", block)
        # inspect, don't rewrite the running engine
        self.assertIn("don't rewrite", block)

    def test_start_sh_clones_the_source_repo(self):
        text = pathlib.Path("start.sh").read_text()
        self.assertIn("git clone", text)
        self.assertIn("github.com/InferencePort-AI/inferolli", text)
        # a github outage must not stop the bot from booting
        self.assertIn("|| true", text)
        self.assertIn("clone unavailable", text)

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

    def test_chromium_recipes_enable_software_gl(self):
        """There is no GPU in the sandbox. Chromium 156 blocklists WebGL without
        SwiftShader, so GPU pages (gd-webport died on this) render an error
        card: 'no hardware acceleration'. Every launch recipe must carry the
        software-GL flags, and none may pass --disable-gpu."""
        import re
        src = pathlib.Path("cogs/companion.py").read_text()
        for needle in ("--use-angle=swiftshader", "--enable-unsafe-swiftshader"):
            # the agent's recipe in DESKTOP_BLOCK
            self.assertIn(needle, companion.DESKTOP_BLOCK)
            # the deterministic screen errand
            errand = src[src.index("def _run_screen_errand"):src.index("def _wants_to_join")]
            self.assertIn(needle, errand)
        # the command line itself must not pass it (the comment above it does)
        chrome_line = [ln for ln in errand.splitlines() if ln.strip().startswith("chrome = (")][0]
        chrome_cmd = "".join(errand.splitlines()[errand.splitlines().index(chrome_line):
                                              errand.splitlines().index(chrome_line) + 4])
        self.assertNotIn("--disable-gpu", chrome_cmd)
        computer_src = pathlib.Path("cogs/computer.py").read_text()
        self.assertIn("--enable-unsafe-swiftshader", computer_src)

    def test_a_long_desktop_turn_never_goes_silent(self):
        """The gd-webport incident, part 2: a multi-step desktop job outran
        the single 210s reply wait, so three turns in a row produced NO reply —
        the agent worked in the background while the channel got silence. The
        reply path now waits, posts an honest interim note, and keeps waiting."""
        src = pathlib.Path("cogs/companion.py").read_text()
        self.assertIn("asyncio.shield(oc_task)", src)
        self.assertIn("give me a couple", src)
        self.assertIn("interim note sent", src)

    def test_the_errand_note_forbids_claiming_the_unseen(self):
        """Part 3: the deterministic errand's note used to say 'comment on it
        in one dry line' — the agent never sees the screenshot it sent, and
        narrated 'the layout's there' off a WebGL error card."""
        src = pathlib.Path("cogs/companion.py").read_text()
        self.assertIn("You have NOT seen that screenshot", src)
        self.assertIn("_page_render_warnings", src)
        self.assertIn("error card", src)
        self.assertNotIn("comment on it in one dry line", src)


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
