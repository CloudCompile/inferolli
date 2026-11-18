"""companion.py — the outbox hand-off (screenshots ride along with replies)."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cogs.companion as companion


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._outbox = companion.OUTBOX
        companion.OUTBOX = self.tmp.name

    def tearDown(self):
        companion.OUTBOX = self._outbox
        self.tmp.cleanup()

    def _file(self, name, age_s=0.0, size=10):
        p = Path(self.tmp.name) / name
        p.write_bytes(b"x" * size)
        if age_s:
            os.utime(p, (time.time() - age_s, time.time() - age_s))
        return p

    def test_new_files_are_picked_up(self):
        self._file("shot.png")
        found = companion._take_outbox(time.time() - 30)
        self.assertEqual([Path(f).name for f in found], ["shot.png"])

    def test_old_files_ignored(self):
        self._file("old.png", age_s=3600)
        self.assertEqual(companion._take_outbox(time.time() - 30), [])

    def test_oversize_ignored(self):
        self._file("huge.mp4", size=companion.OUTBOX_MAX_BYTES + 1)
        self.assertEqual(companion._take_outbox(time.time() - 30), [])

    def test_temp_files_and_dirs_skipped(self):
        self._file("ok.png")
        Path(self.tmp.name, "subdir").mkdir()
        found = companion._take_outbox(time.time() - 30)
        self.assertEqual([Path(f).name for f in found], ["ok.png"])

    def test_cap_of_three(self):
        for i in range(5):
            self._file(f"s{i}.png")
        self.assertEqual(len(companion._take_outbox(time.time() - 30)), companion.OUTBOX_MAX_FILES)

    def test_delivered_files_moved_aside(self):
        p = self._file("shot.png")
        companion._clear_outbox([str(p)])
        self.assertFalse(Path(p).exists())
        self.assertTrue((Path(self.tmp.name) / "sent" / "shot.png").exists())

    def test_prompt_tells_it_how_to_send(self):
        self.assertIn("/workspace/outbox", companion.DESKTOP_BLOCK)
        self.assertIn("scrot", companion.DESKTOP_BLOCK)


if __name__ == "__main__":
    unittest.main()
