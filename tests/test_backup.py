"""cogs/backup.py — tarball contents (xdg excluded), config checks."""

import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cogs import backup
from cogs import music


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

    def test_tonearm_session_is_archived(self):
        """The Apple Music login rides the backup — a stale copy logs the
        studio out and re-login needs a 2FA human."""
        profile = Path(self.tmp.name) / "tonearm" / "chrome-profile" / "Default"
        profile.mkdir(parents=True)
        (profile / "Cookies").write_text("session-cookie")
        cfg = Path(self.tmp.name) / "tonearm"
        (cfg / "master-key.txt").write_text("ta-test")
        (cfg / "chrome-profile" / "Local State").write_text("{}")
        old = backup.TONEARM_CFG
        backup.TONEARM_CFG = cfg
        try:
            blob = backup._make_tarball()
        finally:
            backup.TONEARM_CFG = old
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            names = tar.getnames()
        self.assertIn("tonearm/chrome-profile/Default/Cookies", names)
        self.assertIn("tonearm/master-key.txt", names)
        self.assertIn("tonearm/chrome-profile/Local State", names)
        # no cache — only the login files
        self.assertFalse([n for n in names if "Cache" in n or "GPUCache" in n])

    def test_missing_tonearm_profile_is_not_fatal(self):
        old = backup.TONEARM_CFG
        backup.TONEARM_CFG = Path(self.tmp.name) / "nowhere"
        try:
            blob = backup._make_tarball()
        finally:
            backup.TONEARM_CFG = old
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            self.assertIn("data/memory/users/1.json", tar.getnames())


class RestoreTonearmTests(unittest.TestCase):
    """Restore must put the studio session back where tonearm looks for it,
    and never create a stray /bot/tonearm directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._data = backup.DATA_DIR
        self._cfg = backup.TONEARM_CFG
        backup.DATA_DIR = root / "bot" / "data"
        backup.DATA_DIR.mkdir(parents=True)
        backup.TONEARM_CFG = root / "home" / ".config" / "tonearm"

    def tearDown(self):
        backup.DATA_DIR = self._data
        backup.TONEARM_CFG = self._cfg
        self.tmp.cleanup()

    def _archive(self) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in [
                ("data/memory/users.json", b"{}"),
                ("tonearm/chrome-profile/Default/Cookies", b"session"),
                ("tonearm/master-key.txt", b"ta-test"),
            ]:
                ti = tarfile.TarInfo(name)
                ti.size = len(content)
                tar.addfile(ti, io.BytesIO(content))
        return buf.getvalue()

    def test_restore_extracts_tonearm_to_config_dir(self):
        blob = self._archive()
        fake_s3 = mock.Mock()
        fake_s3.get_object.return_value = {"Body": io.BytesIO(blob)}
        with mock.patch.object(backup, "_configured", lambda: True), \
             mock.patch.object(backup, "_s3", lambda: fake_s3), \
             mock.patch.dict(os.environ, {"S3_BUCKET": "b"}):
            summary = backup.restore_latest()

        cookie = backup.TONEARM_CFG / "chrome-profile" / "Default" / "Cookies"
        self.assertTrue(cookie.exists(), "studio cookie restored to config dir")
        self.assertEqual(cookie.read_text(), "session")
        self.assertTrue((backup.TONEARM_CFG / "master-key.txt").exists())
        self.assertTrue((backup.DATA_DIR / "memory" / "users.json").exists())
        self.assertFalse((backup.DATA_DIR.parent / "tonearm").exists(),
                         "must not drop a stray tonearm/ into /bot")
        self.assertIn("restored brain", summary)

    def test_the_music_ledger_rides_along_with_the_cortex(self):
        """data/music.json says which playlists the bot may edit. If a restore
        lost it, every playlist it made would silently become foreign (edits
        refused) — safe, but broken. It must be inside data/ and restored."""
        # music.py sits in cogs/, so its default ledger is <repo>/data/music.json
        self.assertEqual(music.STATE_FILE.parent,
                         Path(backup.__file__).resolve().parent.parent / "data")
        (backup.DATA_DIR / "music.json").write_text('{"playlists": {"p.mine": {}}}')
        s3 = mock.Mock()
        s3.list_objects_v2.return_value = {}  # empty backups/ prefix
        with mock.patch.object(backup, "_configured", lambda: True), \
             mock.patch.object(backup, "_s3", lambda: s3), \
             mock.patch.dict(os.environ, {"S3_BUCKET": "b"}):
            backup.run_backup()
        blob = s3.put_object.call_args_list[0][1]["Body"]  # first put = data.tar.gz
        names = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz").getnames()
        self.assertIn("data/music.json", names)


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
