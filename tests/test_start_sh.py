"""start.sh — structural invariants of the sandbox entrypoint."""

import subprocess
import unittest
from pathlib import Path

SH = Path(__file__).parent.parent / "start.sh"


class StartShTests(unittest.TestCase):
    def setUp(self):
        self.text = SH.read_text()

    def test_syntax(self):
        r = subprocess.run(["bash", "-n", str(SH)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_single_xvfb_launch(self):
        launches = [ln for ln in self.text.splitlines()
                    if ln.strip().startswith("Xvfb :99 -screen")]
        self.assertEqual(len(launches), 1, f"duplicate Xvfb launches: {launches}")

    def test_waits_for_env_then_idles(self):
        self.assertIn("while [ ! -f /bot/.env ]", self.text)
        self.assertIn("exec sleep infinity", self.text)

    def test_bootstraps_env_from_the_environment(self):
        """A reincarnated sandbox has no launcher to write /bot/.env, and the
        canonical copy is in S3 — unreachable without credentials. Before this,
        such a sandbox idled for 10 minutes and never started the bot at all."""
        idle = self.text.index("exec sleep infinity")
        bootstrap = self.text.index("rebuilding it from the environment")
        self.assertLess(bootstrap, idle)
        for key in ("DISCORD_BOT_TOKEN", "S3_ENDPOINT", "E2B_API_KEY"):
            self.assertIn(key, self.text)

    def test_only_idles_without_any_credentials(self):
        self.assertIn('if [ ! -s /bot/.env ]', self.text)

    def test_an_empty_env_keeps_waiting_before_idling(self):
        """A drill measured the keeper's /bot/.env write landing 4.5 minutes
        after boot (the SDK connect retried until the sandbox was ready).
        Idling at the first empty check would kill the sandbox the keeper just
        created."""
        wait = self.text.index("while [ ! -s /bot/.env ]")
        idle = self.text.index("exec sleep infinity")
        self.assertLess(wait, idle)
        self.assertIn('"$WAITED" -lt 600', self.text)

    def test_rebuilds_when_any_credential_is_present(self):
        """A drill passed only the S3 keys. The gate demanded a Discord token,
        so nothing was rebuilt and the sandbox waited out the whole loop."""
        self.assertIn("have_any_creds", self.text)
        gate = self.text[self.text.index("have_any_creds() {"):]
        gate = gate[:gate.index("}")]
        for key in ("E2B_API_KEY", "S3_BUCKET", "S3_ACCESS_KEY_ID"):
            self.assertIn(key, gate)

    def test_bootstrap_is_not_behind_a_long_wait(self):
        """The first attempt must happen BEFORE the wait loop.

        A reincarnated sandbox boots with credentials in the environment but no
        .env file. When the rebuild sat after the wait, it sat behind a ten
        minute sleep — so the successor never started, and a drill that waited
        four minutes saw nothing at all.
        """
        first_bootstrap = self.text.index("rebuild_env_from_environment\nfi")
        wait_loop = self.text.index("while [ ! -f /bot/.env ]")
        self.assertLess(first_bootstrap, wait_loop)
        # the wait BEFORE bootstrap stays short; only the secrets-in-flight wait
        # after it may be long
        self.assertLess(self.text.index('"$WAITED" -lt 120'),
                        self.text.index('"$WAITED" -lt 600'))

    def test_restores_backup_before_booting(self):
        restore = self.text.index("restore_latest")
        boot = self.text.index("python -u bot.py")
        self.assertLess(restore, boot)

    def test_supervisor_loop_and_watchdog(self):
        self.assertIn("while true; do", self.text)
        self.assertIn("pgrep -f 'opencode serve'", self.text)
        self.assertIn("pgrep -f 'uvicorn desktop_gate'", self.text)
        # the tunnel check is a HEALTH check, not a liveness check: a paused
        # sandbox gets its quick tunnel reaped and cloudflared retries the dead
        # id forever while pgrep says it's alive
        self.assertIn("ensure_tunnel", self.text)
        fn = self.text[self.text.index("ensure_tunnel() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("trycloudflare", fn)
        self.assertIn("/health", fn)
        self.assertIn("pkill -x cloudflared", fn)
        self.assertIn("pgrep -f 'Xvfb :99'", self.text)

    def test_no_fixed_opencode_password_is_exported(self):
        """opencode mints a new password each start.

        This test used to assert the opposite — that start.sh exported a constant
        password — which locked in the bug: the server ignored it, the client kept
        sending it, every request 401'd, and the primary reply engine was dead.
        """
        self.assertNotRegex(self.text, r"export\s+OPENCODE_SERVER_PASSWORD=\S")
        self.assertIn("unset OPENCODE_SERVER_PASSWORD", self.text)

    def test_waits_for_the_generated_password_to_appear(self):
        self.assertIn("server password", self.text)

    def test_env_sourced_before_opencode(self):
        env_line = self.text.index(". /bot/.env")
        oc_line = self.text.index("opencode serve --port 4096")
        self.assertLess(env_line, oc_line)


if __name__ == "__main__":
    unittest.main()
