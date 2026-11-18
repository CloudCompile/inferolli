"""tools_server.py — the evidence rule.

Inferolli is allowed to have opinions. It is not allowed to claim it verified
something it never looked at. The rule is injected into every tool-loop prompt,
into the persona prompt, and violations are logged (without rewriting the
reply) so we can see how often the model slips.
"""

import unittest

import tools_server
from cogs import companion


class EvidencePromptTests(unittest.TestCase):
    def test_rule_is_in_the_tool_prompt(self):
        self.assertIn("EVIDENCE RULE", tools_server.EVIDENCE_RULE)
        self.assertIn("Never claim you checked", tools_server.EVIDENCE_RULE)

    def test_rule_is_appended_in_run_loop(self):
        # the loop composes base_system + TOOLS_PROMPT + EVIDENCE_RULE
        import inspect
        src = inspect.getsource(tools_server.run_loop)
        self.assertIn("EVIDENCE_RULE", src)

    def test_persona_forbids_fake_verification(self):
        prompt = companion._persona_prompt({
            "name": "Inferolli", "vibe": "v", "opinions": [], "running_jokes": [],
            "energy": 0.5, "self_model": "",
        })
        self.assertIn("NOT LYING", prompt)
        self.assertIn("Never say you checked", prompt)


class ClaimDetectionTests(unittest.TestCase):
    def test_flags_verification_claim_with_no_tool(self):
        for reply in ("i just checked the docs and it's fine",
                      "i looked it up — sol is cheaper",
                      "i've verified the release date",
                      "according to my search, it shipped"):
            self.assertTrue(tools_server.claimed_without_evidence(reply, 0), reply)

    def test_does_not_flag_when_a_tool_ran(self):
        self.assertFalse(tools_server.claimed_without_evidence("i just checked", 2))

    def test_does_not_flag_hedged_opinions(self):
        for reply in ("i think it's probably fine",
                      "my gut says sol is the better buy",
                      "last i knew it was 6.1"):
            self.assertFalse(tools_server.claimed_without_evidence(reply, 0), reply)

    def test_empty_reply_not_flagged(self):
        self.assertFalse(tools_server.claimed_without_evidence("", 0))


if __name__ == "__main__":
    unittest.main()
