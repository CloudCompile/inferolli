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

    def test_restores_backup_before_booting(self):
        restore = self.text.index("restore_latest")
        boot = self.text.index("python -u bot.py")
        self.assertLess(restore, boot)

    def test_supervisor_loop_and_watchdog(self):
        self.assertIn("while true; do", self.text)
        self.assertIn("pgrep -f 'opencode serve'", self.text)
        self.assertIn("pgrep -f 'uvicorn desktop_gate'", self.text)
        self.assertIn("pgrep -f 'cloudflared tunnel'", self.text)
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
