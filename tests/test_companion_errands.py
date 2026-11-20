"""companion.py — deterministic screen errands ("open X", "search Y")."""

import unittest

from cogs.companion import _desktop_errand


class ErrandTests(unittest.TestCase):
    def test_demo_message(self):
        url = _desktop_errand("go use Google to search about inferenceport and send us a screenshot back")
        self.assertEqual(url, "https://www.google.com/search?q=inferenceport")

    def test_research_is_not_a_search_verb(self):
        # "research" contains "search" — this hijacked a real message into
        # q=and+tell+us+your+opinios instead of answering it
        msg = ("nono i mean on the 6.2 astra and 6.2 sol release. "
               "do some research and tell us  your opinios")
        self.assertIsNone(_desktop_errand(msg))

    def test_research_alone_is_not_an_errand(self):
        self.assertIsNone(_desktop_errand("research the 6.2 astra release"))

    def test_version_numbers_survive(self):
        self.assertEqual(
            _desktop_errand("search for inferenceport 6.2 astra and 6.2 sol release"),
            "https://www.google.com/search?q=inferenceport+6.2+astra+and+6.2+sol+release")

    def test_trailing_instruction_clause_dropped(self):
        self.assertEqual(_desktop_errand("search 6.2 astra and 6.2 sol. and tell us your opinions"),
                         "https://www.google.com/search?q=6.2+astra+and+6.2+sol")

    def test_search_phrase(self):
        self.assertEqual(_desktop_errand("search for tonearm discord bot"),
                         "https://www.google.com/search?q=tonearm+discord+bot")

    def test_google_verb(self):
        self.assertEqual(_desktop_errand("google inferenceport"),
                         "https://www.google.com/search?q=inferenceport")

    def test_open_bare_domain(self):
        self.assertEqual(_desktop_errand("open inferenceport.ai"), "https://inferenceport.ai")

    def test_open_full_url(self):
        self.assertEqual(_desktop_errand("go to https://inferenceport.ai/models please"),
                         "https://inferenceport.ai/models")

    def test_chat_is_not_an_errand(self):
        for msg in ("what's up", "open the door", "that's cool", ""):
            self.assertIsNone(_desktop_errand(msg), msg)

    def test_search_on_a_domain_is_a_search(self):
        # no open/go-to verb → a web search for the domain string
        self.assertEqual(_desktop_errand("search on inferenceport.ai"),
                         "https://www.google.com/search?q=inferenceport.ai")

    def test_a_search_bar_is_a_noun_not_a_verb(self):
        """The gd-webport incident: 'find peam palms using the search bar'
        became a Google search for 'bar' — sent twice, and the note told the
        agent the job was already done, so it stopped actually doing it."""
        self.assertIsNone(_desktop_errand(
            "okay, i fixed the bug, use the open page and go to the community "
            "levels and find peam palms using the search bar"))
        self.assertIsNone(_desktop_errand(
            "I MEANT THE SEARCH BAR AFTER YOU OPEN GEOMETRY DASH AND THEN OPEN "
            "THE COMMUNITY LEVELS TAB AND SEARCH IN THE IN GAME SEARCH BAR"))
        # but a real web search still works
        self.assertEqual(_desktop_errand("search for peam palms"),
                         "https://www.google.com/search?q=peam+palms")

    def test_compound_desktop_instructions_are_left_to_the_agent(self):
        """Imperatives before the search verb mean a multi-step desktop job —
        flattening it into a Google query hijacks the message."""
        self.assertIsNone(_desktop_errand(
            "open the game and search for peam palms"))
        self.assertIsNone(_desktop_errand(
            "click the search icon then type peam palms in the search bar"))

    def test_render_failures_are_detected_in_the_browser_log(self):
        from cogs.companion import _render_fail_in
        dead = ("[51613:51613:ERROR:gpu/command_buffer/service/context_group.cc:148] "
                "ContextResult::kFatalFailure: WebGL2 blocklisted")
        self.assertIn("error card", _render_fail_in(dead))
        self.assertIn("error card", _render_fail_in(
            "net::ERR_NAME_NOT_RESOLVED at https://nope.invalid"))
        self.assertEqual(_render_fail_in("loaded fine, no errors"), "")
        self.assertEqual(_render_fail_in(""), "")


if __name__ == "__main__":
    unittest.main()
