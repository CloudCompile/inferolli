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


if __name__ == "__main__":
    unittest.main()
