"""models_feed.py — diffs, price formatting, silent boot sync (repost fix)."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cogs import models_feed


def cat(**prices):
    return {mid: {"name": mid.upper(), "prompt": p, "completion": p,
                  "context": 1000, "type": "chat"}
            for mid, p in prices.items()}


class DiffTests(unittest.TestCase):
    def test_added_removed_reprice(self):
        old = cat(a="0.1", b="0.2")
        new = cat(a="0.5", b="0.2", c="0.3")
        d = models_feed.diff(old, new)
        self.assertEqual(d["added"], ["c"])
        self.assertEqual(d["removed"], [])
        self.assertEqual(d["price_changes"], [("a", "prompt", "0.1", "0.5"),
                                              ("a", "completion", "0.1", "0.5")])

    def test_removed(self):
        d = models_feed.diff(cat(a="1", b="2"), cat(a="1"))
        self.assertEqual(d["removed"], ["b"])

    def test_no_change_is_empty(self):
        d = models_feed.diff(cat(a="1"), cat(a="1"))
        self.assertFalse(d["added"] or d["removed"] or d["price_changes"])

    def test_per_m_formatting(self):
        self.assertIsNone(models_feed._per_m(None))
        self.assertIsNone(models_feed._per_m("0"))
        self.assertEqual(models_feed._per_m("0.002"), "$2000.00/M")
        self.assertEqual(models_feed._per_m("0.0000005"), "$0.50/M")
        self.assertIsNone(models_feed._per_m("not-a-number"))


class BootSyncTests(unittest.TestCase):
    """A fresh sandbox restores a stale snapshot — boot must NOT re-announce."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._snap = models_feed.SNAPSHOT
        models_feed.SNAPSHOT = Path(self.tmp.name) / "models-snapshot.json"
        self.catalog = cat(a="1")
        self.posted = []

    def tearDown(self):
        models_feed.SNAPSHOT = self._snap
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def _cog(self):
        cog = models_feed.ModelsFeedCog(bot=None)

        async def post_diff(d):
            self.posted.append(d)

        cog.post_diff = post_diff
        return cog

    def test_silent_boot_sync_then_real_announce(self):
        async def fake_catalog():
            return self.catalog

        with mock.patch.object(models_feed, "fetch_catalog", fake_catalog):
            cog = self._cog()

            # boot cycle: silent baseline refresh, nothing posted
            self.run_async(cog.check_once(announce=False))
            self.assertTrue(models_feed.SNAPSHOT.exists())
            self.assertEqual(self.posted, [])

            # a real change on the 6h cycle: announced exactly once
            self.catalog = cat(a="1", b="2")
            self.run_async(cog.check_once(announce=True))
            self.run_async(cog.check_once(announce=True))

        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.posted[0]["added"], ["b"])

    def test_no_baseline_announces_nothing(self):
        async def fake_catalog():
            return self.catalog

        with mock.patch.object(models_feed, "fetch_catalog", fake_catalog):
            cog = self._cog()
            self.run_async(cog.check_once(announce=True))
        self.assertEqual(self.posted, [])
        self.assertTrue(models_feed.SNAPSHOT.exists())


if __name__ == "__main__":
    unittest.main()
