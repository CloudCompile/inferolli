"""config.py — per-guild defaults, atomic updates, corruption tolerance."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_data, self._orig_file = config.DATA_DIR, config.CONFIG_FILE
        config.DATA_DIR = Path(self.tmp.name)
        config.CONFIG_FILE = config.DATA_DIR / "config.json"

    def tearDown(self):
        config.DATA_DIR, config.CONFIG_FILE = self._orig_data, self._orig_file
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_defaults_when_missing(self):
        cfg = self.run_async(config.get(1))
        self.assertFalse(cfg["automod"]["enabled"])
        self.assertEqual(cfg["starboard_threshold"], 3)
        self.assertTrue(cfg["companion"]["enabled"])

    def test_update_persists_and_isolates_guilds(self):
        async def go():
            await config.update(1, lambda c: c.update(mod_log_channel_id=42))
            await config.update(2, lambda c: c.update(mod_log_channel_id=99))
            return await config.get(1), await config.get(2)

        one, two = self.run_async(go())
        self.assertEqual(one["mod_log_channel_id"], 42)
        self.assertEqual(two["mod_log_channel_id"], 99)

    def test_get_returns_a_copy(self):
        async def go():
            a = await config.get(1)
            a["automod"]["enabled"] = True          # mutate the returned dict
            b = await config.get(1)
            return b["automod"]["enabled"]

        self.assertFalse(self.run_async(go()))

    def test_deep_merge_backfills_new_keys(self):
        # an old config from before a section existed gains every new default
        config.CONFIG_FILE.write_text(json.dumps({"1": {"automod": {"enabled": True}}}))
        cfg = self.run_async(config.get(1))
        self.assertTrue(cfg["automod"]["enabled"])
        self.assertEqual(cfg["automod"]["timeout_seconds"], 600)

    def test_corrupt_file_falls_back_to_defaults(self):
        config.CONFIG_FILE.write_text("{not json!!")
        cfg = self.run_async(config.get(1))
        self.assertFalse(cfg["automod"]["enabled"])

    def test_mutate_returning_replacement(self):
        async def go():
            await config.update(1, lambda c: {**c, "starboard_threshold": 7})
            return await config.get(1)

        self.assertEqual(self.run_async(go())["starboard_threshold"], 7)


if __name__ == "__main__":
    unittest.main()
