"""rag.py — chunking, html extraction, hybrid retrieval."""

import asyncio
import unittest
from unittest import mock

import rag


class ChunkTests(unittest.TestCase):
    def test_splits_long_blocks(self):
        para = "a" * 100
        chunks = rag._chunk(f"{para}\n\n{para}\n\n{para}", size=150)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 150 for c in chunks))

    def test_merges_small_blocks(self):
        chunks = rag._chunk("a" * 90 + "\n\n" + "b" * 90, size=1500)
        self.assertEqual(len(chunks), 1)

    def test_drops_trivial_fragments(self):
        self.assertEqual(rag._chunk("tiny", size=1500), [])


class HtmlTests(unittest.TestCase):
    def test_title_and_text(self):
        title, text = rag._html_to_text(
            "<html><head><title>Docs</title></head><body><p>Hello world</p></body></html>")
        self.assertEqual(title, "Docs")
        self.assertIn("Hello world", text)
        self.assertNotIn("<p>", text)

    def test_script_content_skipped(self):
        _, text = rag._html_to_text(
            "<html><script>alert(1)</script><p>visible</p></html>")
        self.assertIn("visible", text)
        self.assertNotIn("alert(1)", text)


class SearchTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_hybrid_ranking_and_per_page_cap(self):
        chunks = [
            {"url": "u1", "title": "API", "text": "embeddings api endpoint docs",
             "embedding": [1.0, 0.0]},
            {"url": "u1", "title": "API", "text": "embeddings api again",
             "embedding": [1.0, 0.0]},
            {"url": "u1", "title": "API", "text": "embeddings api third",
             "embedding": [1.0, 0.0]},
            {"url": "u2", "title": "Other", "text": "something unrelated entirely",
             "embedding": [0.0, 1.0]},
        ]

        async def fake_index():
            return chunks

        async def fake_embed(texts):
            return [[1.0, 0.0]]

        with mock.patch.object(rag, "_load_index", fake_index), \
             mock.patch.object(rag.ai, "embed", fake_embed):
            out = self.run_async(rag.search("embeddings api", k=6))

        self.assertLessEqual(sum(1 for r in out if r["url"] == "u1"), 2)
        self.assertIn("embeddings", out[0]["text"])
        self.assertEqual(len(out), 3)

    def test_k_is_respected(self):
        chunks = [{"url": f"u{i}", "title": "", "text": f"topic {i}",
                   "embedding": [1.0, 0.0]} for i in range(10)]

        async def fake_index():
            return chunks

        async def fake_embed(texts):
            return [[1.0, 0.0]]

        with mock.patch.object(rag, "_load_index", fake_index), \
             mock.patch.object(rag.ai, "embed", fake_embed):
            out = self.run_async(rag.search("topic", k=3))
        self.assertEqual(len(out), 3)


if __name__ == "__main__":
    unittest.main()
