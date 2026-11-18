"""settings.py — config.json is the home for non-secret configuration.

The point of the split: changing a model is a config edit, not a secrets edit.
Secrets stay in .env and are never read from config.json.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self._orig = settings.CONFIG_PATH
        settings.CONFIG_PATH = self.path
        settings.reset()

    def tearDown(self):
        settings.CONFIG_PATH = self._orig
        settings.reset()
        self.tmp.cleanup()

    def write(self, data):
        self.path.write_text(json.dumps(data))
        settings.reset()

    def test_reads_dotted_path(self):
        self.write({"ai": {"model": "deepseek-v4.1-flash-1017"}})
        self.assertEqual(settings.get("ai.model"), "deepseek-v4.1-flash-1017")

    def test_config_wins_over_stale_env(self):
        # a supervisor can hand down an env var set hours ago; config.json must
        # be the source of truth or edits look like they did nothing
        self.write({"ai": {"model": "from-config"}})
        with mock.patch.dict(os.environ, {"AI_MODEL": "from-env"}):
            self.assertEqual(settings.ai_model(), "from-config")

    def test_env_used_when_config_missing_key(self):
        self.write({"ai": {}})
        with mock.patch.dict(os.environ, {"AI_MODEL": "from-env"}):
            self.assertEqual(settings.ai_model(), "from-env")

    def test_missing_file_falls_back_to_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.ai_model(), "gpt-6-luna")
            self.assertEqual(settings.ai_fallbacks(), ["lightning"])

    def test_corrupt_file_does_not_crash(self):
        self.path.write_text("{not json")
        settings.reset()
        self.assertEqual(settings.get("ai.model", "fallback"), "fallback")

    def test_lists_accept_json_or_csv(self):
        self.write({"ai": {"fallback_models": ["a", "b"]}})
        self.assertEqual(settings.ai_fallbacks(), ["a", "b"])
        # with nothing configured, env is the fallback and accepts csv
        self.write({"ai": {}})
        with mock.patch.dict(os.environ, {"AI_FALLBACK_MODELS": "x, y,z"}):
            self.assertEqual(settings.ai_fallbacks(), ["x", "y", "z"])

    def test_job_models(self):
        self.write({"ai": {"jobs": {"dream": "gemini-4-hydrogen"}}})
        self.assertEqual(settings.job_model("dream"), "gemini-4-hydrogen")
        self.assertIsNone(settings.job_model("reply"))

    def test_job_config_wins_over_env(self):
        self.write({"ai": {"jobs": {"dream": "from-config"}}})
        with mock.patch.dict(os.environ, {"AI_JOB_DREAM": "from-env"}):
            self.assertEqual(settings.job_model("dream"), "from-config")

    def test_job_names_normalize_dashes(self):
        self.write({"ai": {"jobs": {}}})
        with mock.patch.dict(os.environ, {"AI_JOB_QUICK_THING": "m"}):
            self.assertEqual(settings.job_model("quick-thing"), "m")

    def test_ints_and_bools(self):
        self.write({"ambient": {"every_seconds": 1500, "on": True}})
        self.assertEqual(settings.get_int("ambient.every_seconds", 0), 1500)
        self.assertTrue(settings.get_bool("ambient.on", False))
        self.assertEqual(settings.get_int("ambient.nope", 7), 7)

    def test_base_url_strips_trailing_slash(self):
        self.write({"ai": {"base_url": "https://x.example/v1/"}})
        self.assertEqual(settings.ai_base_url(), "https://x.example/v1")

    def test_gateway_pair(self):
        self.write({"ai": {"gateway": {"base_url": "https://gw/", "model": "stealth/x"}}})
        self.assertEqual(settings.gateway(), ("https://gw", "stealth/x"))


class SecretsNotInConfigTests(unittest.TestCase):
    """config.json is committed — it must never carry credentials."""

    def test_repo_config_has_no_secret_values(self):
        import re
        text = (Path(__file__).resolve().parent.parent / "config.json").read_text()
        self.assertIsNone(re.search(r"(sk-|ipa_live|e2b_[a-f0-9]{20,}|gho_|AKIA)", text))

    def test_repo_config_parses(self):
        import json as _json
        data = _json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
        self.assertIn("ai", data)
        self.assertIn("permissions", data)


if __name__ == "__main__":
    unittest.main()
