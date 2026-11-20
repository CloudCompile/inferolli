"""scripts/keeper.py — the out-of-VPS rescue.

The bot extends its own timeout, so the only thing that can still take it down
is E2B's 24h lifetime cap (pause+resume resets it). This script resumes a
paused sandbox and buys it an hour, from anywhere that has the API key: GitHub
Actions cron, the VPS shepherd, a laptop.

These tests pin the two discoveries that matter:
  * `/v2/sandboxes?state=paused` is the ONLY listing that can see a paused
    sandbox — v1 returns running ones only, so a paused bot looked like no bot.
  * resume is `POST /sandboxes/{id}/resume` (v2 resume does not exist) and it
    comes back with a 5-minute window, so an extend call has to follow it.
"""

import unittest
from unittest import mock

from scripts import keeper

LIVE = {"sandboxID": "live00000000000001", "alias": "inferolli", "templateID": keeper.TEMPLATE_ID, "startedAt": "2026-11-20T01:00:00Z"}
OTHER = {"sandboxID": "other0000000000001", "alias": "tonearm", "templateID": "xxx"}
PAUSED_OLD = {"sandboxID": "paused00000000001", "alias": "inferolli", "templateID": keeper.TEMPLATE_ID, "startedAt": "2026-11-19T01:00:00Z"}
PAUSED_NEW = {"sandboxID": "paused00000000002", "alias": "inferolli", "templateID": keeper.TEMPLATE_ID, "startedAt": "2026-11-20T02:00:00Z"}


class ListingTests(unittest.TestCase):
    def test_is_inferolli_by_alias_or_template(self):
        self.assertTrue(keeper.is_inferolli(LIVE))
        self.assertTrue(keeper.is_inferolli({"templateID": keeper.TEMPLATE_ID}))
        self.assertFalse(keeper.is_inferolli(OTHER))

    def test_uses_the_v2_listing_with_state(self):
        calls = []

        def fake_req(method, path, key, body=None, params=None, timeout=30):
            calls.append((method, path, params))
            return 200, []

        with mock.patch.object(keeper, "_req", fake_req):
            keeper.list_sandboxes("k", "paused")
        method, path, params = calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/v2/sandboxes")
        self.assertEqual(params, {"state": "paused"})

    def test_listing_failure_is_an_error_not_an_empty_list(self):
        """An empty list would read as 'nothing to do' and hide an outage."""
        with mock.patch.object(keeper, "_req", lambda *a, **k: (500, None)):
            with self.assertRaises(RuntimeError):
                keeper.list_sandboxes("k", "paused")


class RunOnceTests(unittest.TestCase):
    def test_no_key_is_an_error(self):
        with mock.patch.dict("os.environ", {"E2B_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                keeper.run_once()

    def test_running_sandbox_just_gets_its_window_extended(self):
        calls = []

        def fake_req(method, path, key, body=None, params=None, timeout=30):
            calls.append((method, path, body))
            if path == "/v2/sandboxes":
                return 200, [LIVE, OTHER]
            return 204, None

        with mock.patch.object(keeper, "_req", fake_req):
            report = keeper.run_once("k")
        self.assertEqual(report["running"], ["live00000000000001"])
        self.assertEqual(report["resumed"], [])
        self.assertIn(("POST", "/sandboxes/live00000000000001/timeout", {"timeout": 3600}), calls)

    def test_paused_sandbox_is_resumed_then_extended(self):
        calls = []

        def fake_req(method, path, key, body=None, params=None, timeout=30):
            calls.append((method, path, body))
            if path == "/v2/sandboxes":
                return 200, ([] if params["state"] == "running" else [PAUSED_OLD, PAUSED_NEW])
            return 201, None

        with mock.patch.object(keeper, "_req", fake_req):
            report = keeper.run_once("k")
        self.assertEqual(report["resumed"], ["paused00000000002"])
        self.assertIn(("POST", "/sandboxes/paused00000000002/resume", None), calls)
        # five minutes is not a boot: an extend must follow every resume
        self.assertIn(("POST", "/sandboxes/paused00000000002/timeout", {"timeout": 3600}), calls)

    def test_nothing_anywhere_asks_for_a_launcher(self):
        def fake_req(method, path, key, body=None, params=None, timeout=30):
            return 200, []

        # pin the environment: the suite runs with the real .env loaded, which
        # has a Discord token, so can_create() would be true here
        with mock.patch.object(keeper, "_req", fake_req), \
             mock.patch.object(keeper, "can_create", lambda: False):
            report = keeper.run_once("k")
        self.assertTrue(report["needs_launcher"])
        self.assertIsNone(report["created"])

    def test_a_failed_resume_is_reported(self):
        def fake_req(method, path, key, body=None, params=None, timeout=30):
            if path == "/v2/sandboxes":
                return 200, ([] if params["state"] == "running" else [PAUSED_NEW, PAUSED_OLD])
            if path.endswith("/resume"):
                raise RuntimeError("boom")
            return 201, None

        with mock.patch.object(keeper, "_req", fake_req):
            report = keeper.run_once("k")
        self.assertEqual(report["resumed"], [])
        self.assertEqual(len(report["failed"]), 1)


class OneBotOnlyTests(unittest.TestCase):
    """Resuming every paused sandbox would put two bots on one Discord token."""

    def test_only_the_newest_paused_one_is_resumed(self):
        calls = []

        def fake_req(method, path, key, body=None, params=None, timeout=30):
            calls.append(path)
            if path == "/v2/sandboxes":
                return 200, ([] if params["state"] == "running" else [PAUSED_OLD, PAUSED_NEW])
            return 201, None

        with mock.patch.object(keeper, "_req", fake_req):
            report = keeper.run_once("k")
        self.assertEqual(report["resumed"], ["paused00000000002"])
        self.assertEqual(report["left_paused"], ["paused00000000001"])
        self.assertNotIn("/sandboxes/paused00000000001/resume", calls)


class CreateTests(unittest.TestCase):
    """Nothing running and nothing paused: the keeper makes one, so a wiped or
    killed sandbox comes back without the VPS."""

    BUNDLE = {"DISCORD_BOT_TOKEN": "tok", "E2B_API_KEY": "k",
              "S3_BUCKET": "inferolli", "S3_ACCESS_KEY_ID": "id",
              "S3_SECRET_ACCESS_KEY": "sec"}

    def test_needs_a_discord_token_to_create(self):
        with mock.patch.dict("os.environ", {"DISCORD_BOT_TOKEN": ""}, clear=False):
            self.assertFalse(keeper.can_create())

    def test_creation_passes_every_secret_through_the_environment(self):
        sent = {}

        def fake_req(method, path, key, body=None, params=None, timeout=30):
            if path == "/v2/sandboxes":
                return 200, []
            sent["body"] = body
            return 201, {"sandboxID": "brandnew00000001"}

        with mock.patch.dict("os.environ", self.BUNDLE, clear=False), \
             mock.patch.object(keeper, "_req", fake_req):
            report = keeper.run_once("k")
        self.assertEqual(report["created"], "brandnew00000001")
        env = sent["body"]["envVars"]
        for k in ("DISCORD_BOT_TOKEN", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
            self.assertEqual(env[k], self.BUNDLE[k])
        self.assertEqual(sent["body"]["templateID"], keeper.TEMPLATE_ID)
        self.assertTrue(sent["body"]["autoPause"])

    def test_a_failed_create_is_reported_not_raised(self):
        def fake_req(method, path, key, body=None, params=None, timeout=30):
            if path == "/v2/sandboxes":
                return 200, []
            return 500, {"message": "nope"}

        with mock.patch.dict("os.environ", self.BUNDLE, clear=False), \
             mock.patch.object(keeper, "_req", fake_req):
            report = keeper.run_once("k")
        self.assertIsNone(report["created"])
        self.assertEqual(len(report["failed"]), 1)


if __name__ == "__main__":
    unittest.main()
