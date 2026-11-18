"""scripts/shepherd.py — pagination, filtering, timezone-safe age math."""

import sys
import contextlib
import io
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from scripts import shepherd


class FakePaginator:
    def __init__(self, pages):
        self.pages = list(pages)
        self.has_next = bool(self.pages)

    def next_items(self):
        items = self.pages.pop(0) if self.pages else []
        self.has_next = bool(self.pages)
        return items


def fake_e2b(pages):
    return SimpleNamespace(Sandbox=SimpleNamespace(list=lambda **k: FakePaginator(pages)))


class PaginationTests(unittest.TestCase):
    def test_walks_every_page(self):
        pages = [[SimpleNamespace(sandbox_id="a")], [SimpleNamespace(sandbox_id="b")],
                 [SimpleNamespace(sandbox_id="c")]]
        with mock.patch.dict(sys.modules, {"e2b": fake_e2b(pages)}):
            out = shepherd._all_sandboxes()
        self.assertEqual([b.sandbox_id for b in out], ["a", "b", "c"])

    def test_state_filter_is_client_side(self):
        page = [SimpleNamespace(sandbox_id="a", state="running"),
                SimpleNamespace(sandbox_id="b", state="paused")]
        with mock.patch.dict(sys.modules, {"e2b": fake_e2b([page])}):
            out = shepherd._all_sandboxes(state="paused")
        self.assertEqual([b.sandbox_id for b in out], ["b"])

    def test_list_failure_returns_empty(self):
        broken = SimpleNamespace(Sandbox=SimpleNamespace(
            list=mock.Mock(side_effect=RuntimeError("no api key"))))
        with mock.patch.dict(sys.modules, {"e2b": broken}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(shepherd._all_sandboxes(), [])


class FilterTests(unittest.TestCase):
    def test_inferolli_by_name_or_template(self):
        page = [
            SimpleNamespace(sandbox_id="by-name", name="inferolli",
                            template_id="other", started_at=None),
            SimpleNamespace(sandbox_id="by-template", name="",
                            template_id="4no9nprthep01xg42hgy", started_at=None),
            SimpleNamespace(sandbox_id="foreign", name="tonearm",
                            template_id="tonearm-tpl", started_at=None),
        ]
        with mock.patch.dict(sys.modules, {"e2b": fake_e2b([page])}):
            ids = [sid for sid, _ in shepherd.inferolli_sandboxes()]
        self.assertEqual(ids, ["by-name", "by-template"])


class AgeTests(unittest.TestCase):
    def test_aware_datetime(self):
        started = datetime.now(timezone.utc) - timedelta(hours=2)
        self.assertAlmostEqual(shepherd._age_hours(started), 2.0, places=1)

    def test_naive_datetime_does_not_crash(self):
        # the original bug: aware SDK timestamp minus naive utcnow → TypeError
        started = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
        self.assertAlmostEqual(shepherd._age_hours(started), 3.0, places=1)


class EnvTests(unittest.TestCase):
    def test_env_includes_repo_dotenv(self):
        # importing the module must not crash without a .env, and env() must
        # layer .env over the inherited environment without clobbering it
        with mock.patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            with mock.patch.object(shepherd.Path, "exists", lambda self: True), \
                 mock.patch.object(shepherd.Path, "read_text",
                                   lambda self: "FOO=bar\n"):
                out = shepherd.env()
        self.assertEqual(out["FOO"], "bar")
        self.assertEqual(out["PATH"], "/usr/bin")


HEADER = ("SANDBOX ID  TEMPLATE ID  ALIAS  STARTED AT  END AT  STATE  VCPUS  "
          "RAM MIB  ENVD VERSION  METADATA")


class PausedParseTests(unittest.TestCase):
    """The pause/resume loop bug: a loose substring check made the shepherd
    pause+snapshot the LIVE bot every 15 minutes (~10 min freeze each time)."""

    def test_reads_our_paused_row(self):
        text = "\n".join([
            HEADER,
            "ibj2yiriwcmji2xepnxef   4no9nprthep01xg42hgy   inferolli   "
            "11/17/2026, 1:29:00 AM   11/17/2026, 1:34:00 AM   Paused   8   8192   0.10.2   {}",
        ])
        self.assertEqual(shepherd._parse_paused_rows(text), ["ibj2yiriwcmji2xepnxef"])

    def test_foreign_sandbox_ignored(self):
        # tonearm row used to slip through the old `"inferolli" in line` check
        text = "\n".join([
            HEADER,
            "iwyqqak4f6k8do5l3gft4   7ke3a2aksdz1mh4qmjg5   tonearm   "
            "11/17/2026, 1:29:00 AM   11/17/2026, 1:34:00 AM   Paused   2   4096   0.10.2   {}",
        ])
        self.assertEqual(shepherd._parse_paused_rows(text), [])

    def test_running_rows_ignored(self):
        text = "\n".join([
            HEADER,
            "abc   4no9nprthep01xg42hgy   inferolli   11/17/2026, 7:07:57 PM   "
            "11/18/2026, 7:07:57 PM   Running   8   8192   0.10.2   {}",
        ])
        self.assertEqual(shepherd._parse_paused_rows(text), [])

    def test_header_and_garbage_are_safe(self):
        for text in ("", HEADER, "not a table", "   \n\n"):
            self.assertEqual(shepherd._parse_paused_rows(text), [])

    def test_live_sandbox_is_never_resumed(self):
        """The regression: a stale Paused row for a running sandbox must not
        trigger resume (that cycle freezes the bot for minutes)."""
        live = [("live-id", None)]
        with mock.patch.object(shepherd, "inferolli_sandboxes", lambda: live), \
             mock.patch.object(shepherd, "paused_inferolli", lambda: ["live-id"]), \
             mock.patch.object(shepherd.subprocess, "run") as run, \
             mock.patch.object(shepherd.time, "sleep", side_effect=SystemExit), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                shepherd.main()
        cmds = [c.args[0] for c in run.call_args_list if c.args]
        self.assertFalse([c for c in cmds if "resume" in c],
                         "resume must not run on a sandbox that is already running")


if __name__ == "__main__":
    unittest.main()

