"""scripts/e2b_launch.py — paginated sweeps, kill-by-template/name, keep."

os.environ is primed so importing the module (which asserts an API key and
reads .env) stays hermetic and offline.
"""

import os
import sys
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("E2B_API_KEY", "test-key")

from scripts import e2b_launch as launcher  # noqa: E402

TEMPLATE = "4no9nprthep01xg42hgy"


class FakePaginator:
    def __init__(self, pages):
        self.pages = list(pages)
        self.has_next = bool(self.pages)

    def next_items(self):
        items = self.pages.pop(0) if self.pages else []
        self.has_next = bool(self.pages)
        return items


class SweepTests(unittest.TestCase):
    def _fake(self, pages, killed):
        class FakeSandbox:
            @staticmethod
            def list(**kw):
                return FakePaginator([list(p) for p in pages])

            @staticmethod
            def kill(sid):
                killed.append(sid)

        return SimpleNamespace(Sandbox=FakeSandbox)

    def test_all_sandboxes_walks_pages(self):
        fake = self._fake([[SimpleNamespace(sandbox_id="a")],
                           [SimpleNamespace(sandbox_id="b")]], [])
        with mock.patch.dict(sys.modules, {"e2b": fake}), \
             mock.patch.object(launcher, "e2b", fake):
            out = launcher._all_sandboxes()
        self.assertEqual([b.sandbox_id for b in out], ["a", "b"])

    def test_kill_selects_inferolli_and_spares_keep_and_others(self):
        killed = []
        page = [
            SimpleNamespace(sandbox_id="stale", name="inferolli", template_id=TEMPLATE),
            SimpleNamespace(sandbox_id="current", name="inferolli", template_id=TEMPLATE),
            SimpleNamespace(sandbox_id="template-only", name="", template_id=TEMPLATE),
            SimpleNamespace(sandbox_id="tonearm", name="tonearm", template_id="tonearm-tpl"),
        ]
        fake = self._fake([page], killed)
        with mock.patch.dict(sys.modules, {"e2b": fake}), \
             mock.patch.object(launcher, "e2b", fake), \
             contextlib.redirect_stdout(io.StringIO()):
            n = launcher.kill_all_inferolli(keep="current")
        self.assertEqual(n, 2)
        self.assertEqual(sorted(killed), ["stale", "template-only"])

    def test_kill_failure_is_counted_not_raised(self):
        class FakeSandbox:
            @staticmethod
            def list(**kw):
                return FakePaginator([[SimpleNamespace(
                    sandbox_id="x", name="inferolli", template_id=TEMPLATE)]])

            @staticmethod
            def kill(sid):
                raise RuntimeError("api hiccup")

        fake = SimpleNamespace(Sandbox=FakeSandbox)
        with mock.patch.dict(sys.modules, {"e2b": fake}), \
             mock.patch.object(launcher, "e2b", fake), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.kill_all_inferolli(), 0)


if __name__ == "__main__":
    unittest.main()
