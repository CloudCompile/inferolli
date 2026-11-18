"""cogs/backup.py — tarball contents (xdg excluded), config checks."""

import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cogs import backup


class TarballTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._data = backup.DATA_DIR
        backup.DATA_DIR = root
        (root / "memory" / "users").mkdir(parents=True)
        (root / "memory" / "users" / "1.json").write_text("{}")
        (root / "xdg" / "opencode").mkdir(parents=True)
        (root / "xdg" / "opencode" / "db.sqlite").write_text("stale")
        (root / "xdg" / "nested").mkdir()
        (root / "xdg" / "nested" / "x.bin").write_text("y")

    def tearDown(self):
        backup.DATA_DIR = self._data
        self.tmp.cleanup()

    def test_memory_is_archived(self):
        blob = backup._make_tarball()
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            names = tar.getnames()
        self.assertIn("data/memory/users/1.json", names)

    def test_opencode_storage_excluded(self):
        blob = backup._make_tarball()
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            names = tar.getnames()
        # the poisoned opencode sqlite must never ride into a fresh sandbox
        self.assertFalse([n for n in names if "/xdg/" in n], names)


class ConfigTests(unittest.TestCase):
    def test_not_configured_without_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(backup._configured())
            self.assertIn("not configured", backup.run_backup())

    def test_configured_with_credentials(self):
        env = {"S3_ENDPOINT": "https://s3", "S3_BUCKET": "b",
               "S3_ACCESS_KEY_ID": "k", "S3_SECRET_ACCESS_KEY": "s"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(backup._configured())


if __name__ == "__main__":
    unittest.main()
