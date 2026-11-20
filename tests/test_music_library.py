"""cogs/music.py — catalog search, stations, and the playlist ownership rule.

The rule that matters most: the bot CREATES and EDITS its own playlists, but a
playlist that was already in the library is read-only to it — it can play and
search those, never change them. These tests pin that, plus the queue-building
fallback for library playlists (MusicKit refuses a whole-list setQueue on some
builds, and one unavailable track poisons the list).
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cogs.music as music


def run(coro):
    return asyncio.run(coro)


class RowTests(unittest.TestCase):
    def test_flat_tonearm_row(self):
        row = music._row("songs", {"id": "617154366", "name": "Get Lucky",
                                   "artist": "Daft Punk", "artwork": "http://x",
                                   "duration_ms": 369629})
        self.assertEqual(row["kind"], "songs")
        self.assertEqual(row["id"], "617154366")
        self.assertEqual(row["name"], "Get Lucky")
        self.assertEqual(row["artist"], "Daft Punk")
        self.assertEqual(row["artwork"], "http://x")
        self.assertEqual(row["duration_ms"], 369629)

    def test_raw_apple_row(self):
        row = music._row("albums", {"id": "1", "attributes": {
            "name": "Discovery", "artistName": "Daft Punk",
            "artwork": {"url": "http://art"}}})
        self.assertEqual(row["name"], "Discovery")
        self.assertEqual(row["artist"], "Daft Punk")
        self.assertEqual(row["artwork"], "http://art")

    def test_missing_fields_do_not_explode(self):
        row = music._row("stations", {})
        self.assertEqual(row["name"], "?")
        self.assertEqual(row["id"], "")


class DecodeTests(unittest.TestCase):
    def test_autocomplete_value_roundtrip(self):
        self.assertEqual(music._decode("songs:617154366"), ("songs", "617154366"))
        self.assertEqual(music._decode("playlists:p.YJXV3lJuekYea8V"),
                         ("playlists", "p.YJXV3lJuekYea8V"))

    def test_free_text_is_not_a_kind(self):
        self.assertEqual(music._decode("daft punk"), (None, None))
        self.assertEqual(music._decode("https://music.apple.com/x"), (None, None))
        self.assertEqual(music._decode(""), (None, None))


class ChoiceTests(unittest.TestCase):
    def test_choices_are_labeled_by_kind_and_capped(self):
        rows = [{"kind": "stations", "id": f"ra.{i}", "name": f"S{i}", "artist": ""}
                for i in range(40)]
        out = music._choices(rows)
        self.assertEqual(len(out), 25)
        self.assertTrue(out[0].value.startswith("stations:"))
        self.assertIn("📻", out[0].name)

    def test_long_labels_are_clipped_for_discord(self):
        rows = [{"kind": "songs", "id": "1", "name": "x" * 400, "artist": "y" * 400}]
        choice = music._choices(rows)[0]
        self.assertLessEqual(len(choice.name), 100)
        self.assertLessEqual(len(choice.value), 100)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._state = music.STATE_FILE
        music.STATE_FILE = Path(self.tmp.name) / "music.json"

    def tearDown(self):
        music.STATE_FILE = self._state
        self.tmp.cleanup()

    def test_mark_and_forget(self):
        self.assertFalse(music.is_owned("p.new"))
        music.mark_owned("p.new", "Drill", 7)
        self.assertTrue(music.is_owned("p.new"))
        self.assertEqual(music.owned()["p.new"]["name"], "Drill")
        music.unmark_owned("p.new")
        self.assertFalse(music.is_owned("p.new"))

    def test_preexisting_playlist_is_not_owned(self):
        music.mark_owned("p.mine", "Drill", 7)
        self.assertFalse(music.is_owned("p.YJXV3lJuekYea8V"))

    def test_refusal_explains_the_rule(self):
        msg = music.not_owned_message("ACTUALLY GOOD PHONK")
        self.assertIn("ACTUALLY GOOD PHONK", msg)
        self.assertIn("only edit playlists I", msg)
        self.assertIn("/playlist new", msg)


class DispatchTests(unittest.TestCase):
    def _patch(self, post, get=None):
        return mock.patch.multiple(
            music,
            _post=mock.Mock(side_effect=post),
            _get=mock.Mock(side_effect=get or (lambda path, **kw: {})))

    def test_song_plays_by_id(self):
        calls = []

        async def post(path, **kw):
            calls.append((path, kw))
            return {"ok": True}

        with mock.patch.object(music, "_post", mock.Mock(side_effect=post)):
            summary = run(music._start("songs", "617154366"))
        self.assertEqual(summary["mode"], "song")
        self.assertEqual(calls[0][0], "/playback/play-song/617154366")

    def test_album_and_station_use_play_collection(self):
        calls = []

        async def post(path, **kw):
            calls.append((path, kw))
            return {"ok": True}

        with mock.patch.object(music, "_post", mock.Mock(side_effect=post)):
            run(music._start("albums", "617154241"))
            run(music._start("stations", "ra.u-2538ea70"))
        self.assertEqual(calls[0], ("/playback/play-collection",
                                    {"kind": "album", "id": "617154241"}))
        self.assertEqual(calls[1], ("/playback/play-collection",
                                    {"kind": "station", "id": "ra.u-2538ea70"}))

    def test_library_playlist_falls_back_to_track_by_track(self):
        """Whole-list setQueue answers CONTENT_UNSUPPORTED on a library
        playlist; the bot must build the queue instead, skipping duds."""
        calls = []
        tracks = [{"id": "1", "catalog_id": "1"},
                  {"id": "2", "catalog_id": "2"},
                  {"id": "bad", "catalog_id": "bad"}]

        async def post(path, **kw):
            calls.append((path, kw))
            if path == "/playback/play-collection":
                raise music.StudioError("mk-140 CONTENT_UNSUPPORTED")
            if kw.get("catalog_id") == "bad":
                raise music.StudioError("mk-007 NOT_FOUND")
            return {"ok": True}

        async def get(path, **kw):
            if path.startswith("/library/playlists/"):
                return {"tracks": tracks}
            return {}

        with mock.patch.object(music, "_post", mock.Mock(side_effect=post)), \
             mock.patch.object(music, "_get", mock.Mock(side_effect=get)):
            summary = run(music._start("playlists", "p.YJXV3lJuekYea8V"))

        self.assertEqual(calls[0][0], "/playback/play-collection")
        self.assertIn(("/playback/play-song/1", {}), calls)
        self.assertIn(("/playback/play-later", {"catalog_id": "2"}), calls)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["skipped"], 1)

    def test_catalog_playlist_does_not_fall_back(self):
        calls = []

        async def post(path, **kw):
            calls.append((path, kw))
            return {"ok": True}

        with mock.patch.object(music, "_post", mock.Mock(side_effect=post)):
            summary = run(music._start("playlists", "pl.74657640b88c4587a426160f7441de46"))
        self.assertEqual(summary["mode"], "playlist")
        self.assertEqual(len(calls), 1)

    def test_empty_library_playlist_is_an_honest_error(self):
        async def post(path, **kw):
            raise music.StudioError("nope")

        async def get(path, **kw):
            return {"tracks": []}

        with mock.patch.object(music, "_post", mock.Mock(side_effect=post)), \
             mock.patch.object(music, "_get", mock.Mock(side_effect=get)):
            with self.assertRaises(music.StudioError) as ctx:
                run(music._start("playlists", "p.empty"))
        self.assertIn("empty", str(ctx.exception))

    def test_artist_queues_top_songs(self):
        calls = []

        async def post(path, **kw):
            calls.append((path, kw))
            return {"ok": True}

        async def search(term, types, limit=5):
            return [{"kind": "songs", "id": "1", "name": "a"},
                    {"kind": "songs", "id": "2", "name": "b"}]

        with mock.patch.object(music, "_post", mock.Mock(side_effect=post)), \
             mock.patch.object(music, "_catalog_search", mock.Mock(side_effect=search)):
            summary = run(music._start("artists", "1", "Daft Punk"))
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(calls[0][0], "/playback/play-song/1")


class SongIdTests(unittest.TestCase):
    def test_album_pulls_its_tracks(self):
        async def get(path, **kw):
            self.assertEqual(path, "/catalog/albums/617154241/tracks")
            return {"items": [{"id": "1"}, {"id": "2", "catalog_id": "2"}]}

        with mock.patch.object(music, "_get", mock.Mock(side_effect=get)):
            self.assertEqual(run(music._song_ids("albums", "617154241")), ["1", "2"])

    def test_library_playlist_reads_library_route(self):
        async def get(path, **kw):
            self.assertEqual(path, "/library/playlists/p.abc/tracks")
            return {"tracks": [{"id": "9", "catalog_id": "9"}]}

        with mock.patch.object(music, "_get", mock.Mock(side_effect=get)):
            self.assertEqual(run(music._song_ids("playlists", "p.abc")), ["9"])

    def test_song_is_itself(self):
        self.assertEqual(run(music._song_ids("songs", "617154366")), ["617154366"])

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(music.StudioError):
            run(music._song_ids("stations", "ra.1"))


class AutocompleteTests(unittest.TestCase):
    def test_nothing_when_studio_not_wired(self):
        with mock.patch.multiple(music, URL="", KEY=""):
            self.assertEqual(run(music._ac_play(mock.Mock(), "daft")), [])
            self.assertEqual(run(music._ac_owned_playlist(mock.Mock(), "")), [])

    def test_play_autocomplete_mixes_catalog_and_library(self):
        async def search(term, types, limit=5):
            return [{"kind": "songs", "id": "1", "name": "Get Lucky", "artist": "Daft Punk"}]

        async def lib(force=False):
            return [{"kind": "playlists", "id": "p.mine", "name": "Daft mix", "artist": ""}]

        with mock.patch.multiple(music, URL="http://x", KEY="k"), \
             mock.patch.object(music, "_catalog_search", mock.Mock(side_effect=search)), \
             mock.patch.object(music, "_library_playlists", mock.Mock(side_effect=lib)):
            out = run(music._ac_play(mock.Mock(), "daft"))
        values = [c.value for c in out]
        self.assertIn("songs:1", values)
        self.assertIn("playlists:p.mine", values)

    def test_autocomplete_failure_is_silent(self):
        async def boom(*a, **kw):
            raise RuntimeError("down")

        with mock.patch.multiple(music, URL="http://x", KEY="k"), \
             mock.patch.object(music, "_catalog_search", mock.Mock(side_effect=boom)), \
             mock.patch.object(music, "_library_playlists", mock.Mock(side_effect=boom)):
            self.assertEqual(run(music._ac_play(mock.Mock(), "daft")), [])

    def test_owned_autocomplete_only_offers_my_playlists(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = music.STATE_FILE
            music.STATE_FILE = Path(tmp) / "music.json"
            try:
                music.mark_owned("p.mine", "Drill", 7)
                music.mark_owned("p.other", "Phonk drill", 9)

                async def lib(force=False):
                    return [{"kind": "playlists", "id": "p.user", "name": "Drill yours"},
                            {"kind": "playlists", "id": "p.mine", "name": "Drill", "artist": ""}]

                with mock.patch.multiple(music, URL="http://x", KEY="k"), \
                     mock.patch.object(music, "_library_playlists", mock.Mock(side_effect=lib)):
                    out = run(music._ac_owned_playlist(mock.Mock(), "drill"))
                values = [c.value for c in out]
                self.assertIn("playlists:p.mine", values)
                self.assertNotIn("playlists:p.user", values)
            finally:
                music.STATE_FILE = old


class GuardTests(unittest.TestCase):
    """Editing a playlist the bot didn't make must be refused, not attempted."""

    def _interaction(self):
        inter = mock.Mock()
        inter.response.defer = mock.AsyncMock()
        inter.response.send_message = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        return inter

    def test_add_refuses_someone_elses_playlist(self):
        inter = self._interaction()
        cog = music.MusicCog(bot=mock.Mock())
        with mock.patch.multiple(music, URL="http://x", KEY="k"), \
             tempfile.TemporaryDirectory() as tmp:
            old = music.STATE_FILE
            music.STATE_FILE = Path(tmp) / "music.json"
            try:
                run(music.MusicCog.pl_add.callback(
                    cog, inter, "playlists:p.YJXV3lJuekYea8V", "songs:617154366"))
            finally:
                music.STATE_FILE = old
        inter.followup.send.assert_awaited()
        text = inter.followup.send.call_args[0][0]
        self.assertIn("one of yours", text)
        self.assertIn("only edit playlists I created", text)

    def test_remove_requires_owned_playlist(self):
        inter = self._interaction()
        cog = music.MusicCog(bot=mock.Mock())
        with mock.patch.multiple(music, URL="http://x", KEY="k"):
            run(music.MusicCog.pl_remove.callback(cog, inter, "p.YJXV3lJuekYea8V", 1))
        text = inter.followup.send.call_args[0][0]
        self.assertIn("one of yours", text)

    def test_delete_requires_owned_playlist(self):
        inter = self._interaction()
        cog = music.MusicCog(bot=mock.Mock())
        with mock.patch.multiple(music, URL="http://x", KEY="k"):
            run(music.MusicCog.pl_delete.callback(cog, inter, "p.YJXV3lJuekYea8V"))
        text = inter.response.send_message.call_args[0][0]
        self.assertIn("one of yours", text)


if __name__ == "__main__":
    unittest.main()
