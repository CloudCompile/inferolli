"""The keepalive that never ran, and the fixes that make it run.

Post-mortem 2026-11-19: every boot logged

    not in e2b keepalive mode (no E2B_KEEPALIVE/E2B_SANDBOX_ID) — skipping

because the gate read three values from the *start command's* environment, where
none of them exist: E2B_KEEPALIVE is absent, E2B_SANDBOX_ID is the empty string,
and E2B_API_KEY is only in /bot/.env. So the sandbox was never extended by the
bot itself and lived entirely off the VPS shepherd — which is off every night.
These tests pin the discovery rules and the suspension reset.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cogs import e2b_keepalive as ka


class SandboxIdTests(unittest.TestCase):
    def test_rejects_empty_and_hostname(self):
        self.assertFalse(ka.looks_like_sandbox_id(""))
        self.assertFalse(ka.looks_like_sandbox_id(None))
        self.assertFalse(ka.looks_like_sandbox_id("e2b.local"))
        self.assertFalse(ka.looks_like_sandbox_id("localhost"))

    def test_accepts_a_real_id(self):
        self.assertTrue(ka.looks_like_sandbox_id("ibj2yiriwcmji2xepnxef"))

    def test_reads_id_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sandbox-id"
            p.write_text("iwyqqak4f6k8do5l3gft4\n")
            self.assertEqual(ka.sandbox_id_from_files((str(p),)), "iwyqqak4f6k8do5l3gft4")

    def test_skips_files_with_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "a"
            bad.write_text("e2b.local\n")
            good = Path(d) / "b"
            good.write_text("iwyqqak4f6k8do5l3gft4")
            self.assertEqual(ka.sandbox_id_from_files((str(bad), str(good))),
                             "iwyqqak4f6k8do5l3gft4")

    def test_missing_files_are_not_an_error(self):
        self.assertIsNone(ka.sandbox_id_from_files(("/nonexistent/x",)))

    def test_env_id_is_preferred_but_validated(self):
        with mock.patch.dict(os.environ, {"E2B_SANDBOX_ID": "ibj2yiriwcmji2xepnxef"}):
            self.assertEqual(ka.resolve_sandbox_id(), "ibj2yiriwcmji2xepnxef")
        # the empty string and the hostname must NOT win over the file
        with mock.patch.dict(os.environ, {"E2B_SANDBOX_ID": "e2b.local"}), \
             mock.patch.object(ka, "sandbox_id_from_files", lambda *a: "from-file-id-123"):
            self.assertEqual(ka.resolve_sandbox_id(), "from-file-id-123")


class ApiKeyTests(unittest.TestCase):
    def test_parses_key_from_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('DISCORD_BOT_TOKEN=abc\nE2B_API_KEY="e2b_deadbeef"\n')
            self.assertEqual(ka.key_from_env_file(str(p)), "e2b_deadbeef")

    def test_missing_file_or_key_is_none(self):
        self.assertIsNone(ka.key_from_env_file("/nonexistent/.env"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("DISCORD_BOT_TOKEN=abc\n")
            self.assertIsNone(ka.key_from_env_file(str(p)))

    def test_env_beats_file(self):
        with mock.patch.dict(os.environ, {"E2B_API_KEY": "from-env"}):
            self.assertEqual(ka.resolve_api_key(), "from-env")
        with mock.patch.dict(os.environ, {"E2B_API_KEY": ""}), \
             mock.patch.object(ka, "key_from_env_file", lambda *a: "from-file"):
            self.assertEqual(ka.resolve_api_key(), "from-file")


class SuspensionTests(unittest.TestCase):
    def test_normal_tick_is_not_a_suspension(self):
        self.assertFalse(ka.was_suspended(0.0, 120.0, 120))

    def test_a_paused_gap_is_a_suspension(self):
        self.assertTrue(ka.was_suspended(0.0, 3600.0, 120))

    def test_short_interval_still_has_a_floor(self):
        """With a tiny interval the gap must still be substantial, not noise."""
        self.assertFalse(ka.was_suspended(0.0, 60.0, 1))


class PingTests(unittest.TestCase):
    def _cog(self):
        return ka.KeepAliveCog(bot=mock.Mock())

    def test_ping_returns_true_on_200(self):
        outcome = mock.Mock(status_code=200, text="")
        with mock.patch.object(ka.KeepAliveCog, "_api", mock.AsyncMock(return_value=outcome)):
            self.assertTrue(asyncio.run(self._cog().ping("sid", "key")))

    def test_ping_returns_true_on_204(self):
        """The real API answers 204 No Content. Treating only 200 as success
        made a working keepalive log 'ping failed' on every tick."""
        outcome = mock.Mock(status_code=204, text="")
        with mock.patch.object(ka.KeepAliveCog, "_api", mock.AsyncMock(return_value=outcome)):
            self.assertTrue(asyncio.run(self._cog().ping("sid", "key")))

    def test_ping_failure_is_contained(self):
        outcome = mock.Mock(status_code=500, text="boom")
        with mock.patch.object(ka.KeepAliveCog, "_api", mock.AsyncMock(return_value=outcome)):
            self.assertFalse(asyncio.run(self._cog().ping("sid", "key")))

    def test_ping_network_error_is_contained(self):
        with mock.patch.object(ka.KeepAliveCog, "_api",
                               mock.AsyncMock(side_effect=RuntimeError("no route"))):
            self.assertFalse(asyncio.run(self._cog().ping("sid", "key")))

    def test_unauthorized_is_logged_loudly(self):
        outcome = mock.Mock(status_code=401, text="nope")
        with mock.patch.object(ka.KeepAliveCog, "_api", mock.AsyncMock(return_value=outcome)), \
             self.assertLogs("cogs.e2b_keepalive", level="ERROR") as logs:
            self.assertFalse(asyncio.run(self._cog().ping("sid", "bad")))
        self.assertIn("UNAUTHORIZED", "\n".join(logs.output))


class LoopTests(unittest.TestCase):
    class _Bot:
        async def wait_until_ready(self):
            return None

    class _Stop(Exception):
        pass

    def test_pings_immediately_instead_of_after_the_interval(self):
        """The old loop slept first. Five-minute resume windows died waiting."""
        cog = ka.KeepAliveCog(bot=self._Bot())
        calls = []

        async def fake_ping(self, sid, key):
            calls.append(sid)
            return True

        async def stop_after_first(_):
            raise self._Stop

        with mock.patch.object(ka, "resolve_sandbox_id", lambda: "ibj2yiriwcmji2xepnxef"), \
             mock.patch.object(ka, "resolve_api_key", lambda: "e2b_key"), \
             mock.patch.object(ka.KeepAliveCog, "ping", fake_ping), \
             mock.patch.object(ka, "_sleep", stop_after_first):
            with self.assertRaises(self._Stop):
                asyncio.run(cog._loop())
        self.assertEqual(calls, ["ibj2yiriwcmji2xepnxef"])

    def test_missing_id_disables_the_loop_with_an_error(self):
        cog = ka.KeepAliveCog(bot=self._Bot())
        with mock.patch.object(ka, "resolve_sandbox_id", lambda: None), \
             mock.patch.object(ka, "resolve_api_key", lambda: "e2b_key"), \
             self.assertLogs("cogs.e2b_keepalive", level="ERROR") as logs:
            asyncio.run(cog._loop())
        self.assertIn("DISABLED", "\n".join(logs.output))

    def test_missing_key_disables_the_loop_with_an_error(self):
        cog = ka.KeepAliveCog(bot=self._Bot())
        with mock.patch.object(ka, "resolve_sandbox_id", lambda: "ibj2yiriwcmji2xepnxef"), \
             mock.patch.object(ka, "resolve_api_key", lambda: None), \
             self.assertLogs("cogs.e2b_keepalive", level="ERROR") as logs:
            asyncio.run(cog._loop())
        self.assertIn("DISABLED", "\n".join(logs.output))

    def test_suspension_resets_the_window(self):
        """A resume resets e2b's clock; the age clock must reset with it.

        Without this the bot comes back from a sleep, computes that its process
        has been alive for >23h, stops pinging on purpose, and re-pauses — the
        loop that made it look like it kept stopping.
        """
        cog = ka.KeepAliveCog(bot=self._Bot())
        clock = {"t": 0.0}
        cog.window_start = 0.0

        async def fake_ping(self, sid, key):
            return True

        async def bump_and_continue(_):
            clock["t"] += 3600.0        # a big jump = we were suspended
            if clock["t"] > 7200:
                raise self._Stop

        with mock.patch.object(ka, "time") as fake_time, \
             mock.patch.object(ka, "resolve_sandbox_id", lambda: "ibj2yiriwcmji2xepnxef"), \
             mock.patch.object(ka, "resolve_api_key", lambda: "e2b_key"), \
             mock.patch.object(ka.KeepAliveCog, "ping", fake_ping), \
             mock.patch.object(ka, "_sleep", bump_and_continue):
            fake_time.time = lambda: clock["t"]
            with self.assertRaises(self._Stop):
                asyncio.run(cog._loop())
        self.assertGreater(cog.window_start, 0.0)


class SelfDiscoveryTests(unittest.TestCase):
    """A reincarnated sandbox has no launcher to write its id, and its start
    command env has no E2B_SANDBOX_ID — so it has to find itself."""

    ROWS = [
        {"sandboxID": "parent00000000000aa", "startedAt": "2026-11-19T01:00:00Z", "metadata": {}},
        {"sandboxID": "successor00000000b", "startedAt": "2026-11-19T23:00:00Z",
         "metadata": {"role": "successor", "parent": "parent00000000000aa"}},
        {"sandboxID": "old-successor0000c", "startedAt": "2026-11-18T23:00:00Z",
         "metadata": {"role": "successor", "parent": "someone"}},
    ]

    def _cog_with(self, payload, status=200):
        cog = ka.KeepAliveCog(bot=mock.Mock())
        outcome = mock.Mock(status_code=status)
        outcome.json = lambda: payload
        return cog, mock.patch.object(ka.KeepAliveCog, "_api",
                                      mock.AsyncMock(return_value=outcome)).start()

    def test_picks_the_newest_successor(self):
        cog, _ = self._cog_with(self.ROWS)
        try:
            self.assertEqual(asyncio.run(cog.id_from_api("k")), "successor00000000b")
        finally:
            mock.patch.stopall()

    def test_never_returns_the_parent(self):
        cog, _ = self._cog_with([self.ROWS[0]])
        try:
            self.assertIsNone(asyncio.run(cog.id_from_api("k")))
        finally:
            mock.patch.stopall()

    def test_handles_wrapped_payload(self):
        cog, _ = self._cog_with({"sandboxes": self.ROWS})
        try:
            self.assertEqual(asyncio.run(cog.id_from_api("k")), "successor00000000b")
        finally:
            mock.patch.stopall()

    def test_bad_status_is_none(self):
        cog, _ = self._cog_with({}, status=500)
        try:
            self.assertIsNone(asyncio.run(cog.id_from_api("k")))
        finally:
            mock.patch.stopall()


class WallTests(unittest.TestCase):
    class _Bot:
        async def wait_until_ready(self):
            return None

    def test_the_wall_reincarnates_instead_of_just_pausing(self):
        """Pausing means the bot is offline until the VPS wakes — most of the
        night. The successor boots in ~90s on its own."""
        cog = ka.KeepAliveCog(bot=self._Bot())
        called = []

        async def fake_reincarnate(self, sid, key):
            called.append(sid)

        async def fake_ping(self, sid, key):
            return True

        with mock.patch.object(ka, "LIFETIME_H", 0.0), \
             mock.patch.object(ka, "resolve_sandbox_id", lambda: "ibsandbox1234567"), \
             mock.patch.object(ka, "resolve_api_key", lambda: "k"), \
             mock.patch.object(ka.KeepAliveCog, "_reincarnate", fake_reincarnate), \
             mock.patch.object(ka.KeepAliveCog, "ping", fake_ping):
            asyncio.run(cog._loop())
        self.assertEqual(called, ["ibsandbox1234567"])


class StartScriptTests(unittest.TestCase):
    """start.sh has to arm the keepalive, not just assume the bot can find it."""

    @classmethod
    def setUpClass(cls):
        cls.text = Path(__file__).resolve().parents[1].joinpath("start.sh").read_text()

    def test_exports_the_keepalive_flag(self):
        self.assertIn("export E2B_KEEPALIVE=1", self.text)

    def test_falls_back_to_the_sandbox_id_file(self):
        self.assertIn("/bot/data/sandbox-id", self.text)

    def test_extends_its_own_window_before_the_bot_boots(self):
        self.assertIn("/timeout", self.text)
        self.assertIn('"timeout": 3600', self.text)
        extend = self.text.index("/timeout")
        boot = self.text.index("python -u bot.py")
        self.assertLess(extend, boot)

    def test_rejects_the_hostname_as_an_id(self):
        self.assertIn("e2b.local", self.text)


if __name__ == "__main__":
    unittest.main()
