"""cogs/starboard.py — ⭐-ed posts sail to the board exactly once.

The listener runs on every reaction in the server, so a bug here is noisy and
persistent. Two real ones caught first:
  * a corrupt store file raised on every reaction and killed the listener
  * the bot's own 🏅 "sailed" reaction was counted as a star in some paths,
    letting posts drift over the threshold on their own
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import starboard
from tests.fakes import FakeMember, FakePermissions


class FakeReaction:
    def __init__(self, emoji, count):
        self.emoji = emoji
        self.count = count

    def __str__(self):
        return str(self.emoji)


class FakeAttachment:
    def __init__(self, url="https://example.invalid/a.png"):
        self.url = url


class FakeAuthor:
    def __init__(self, name="poster", bot=False):
        self.display_name = name
        self.bot = bot
        self.display_avatar = SimpleNamespace(url="https://example.invalid/av.png")


class FakeStarMessage:
    def __init__(self, mid=1, content="a good post", author=None, stars=0,
                 attachments=(), channel_name="general"):
        self.id = mid
        self.content = content
        self.author = author or FakeAuthor()
        self.reactions = [FakeReaction("⭐", stars)] if stars else []
        self.attachments = list(attachments)
        self.jump_url = f"https://discord.invalid/{mid}"
        self.channel = SimpleNamespace(name=channel_name)
        self.added = []

    async def add_reaction(self, emoji):
        self.added.append(emoji)


class FakeTextChan:
    def __init__(self, name="general", cid=20):
        self.name = name
        self.id = cid
        self.sent = []

    def permissions_for(self, member):
        return FakePermissions()

    async def send(self, content=None, embed=None, **kw):
        self.sent.append(embed)
        return SimpleNamespace(id=999)

    async def fetch_message(self, mid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")


class FakeStarGuild:
    def __init__(self, channels):
        self.id = 100
        self.me = FakeMember(id=555, name="bot", bot=True)
        self._channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._channels.get(cid)


class FakeBot:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, gid):
        return self._guild


def payload(mid=1, channel_id=20, guild_id=100, emoji="⭐"):
    return SimpleNamespace(message_id=mid, channel_id=channel_id,
                           guild_id=guild_id, emoji=emoji, member=None)


class StarboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._store = starboard.STORE
        starboard.STORE = Path(self.tmp.name) / "starboard.json"
        self.chan = FakeTextChan("general", 20)
        self.board = FakeTextChan("starboard", 21)
        self.guild = FakeStarGuild([self.chan, self.board])
        self.cog = starboard.StarboardCog(FakeBot(self.guild))
        self.cfg = {"starboard_channel_id": 21, "starboard_threshold": 3}

    def tearDown(self):
        starboard.STORE = self._store
        self.tmp.cleanup()

    def _post(self, stars=3, **kw):
        msg = FakeStarMessage(stars=stars, **kw)

        async def fetch(mid):
            return msg

        self.chan.fetch_message = fetch
        return msg

    def _run(self, pl):
        async def go():
            with mock.patch("config.get", mock.AsyncMock(return_value=self.cfg)):
                await self.cog.on_raw_reaction_add(pl)
        import asyncio
        asyncio.run(go())

    def test_threshold_met_posts_to_board(self):
        self._post(stars=3)
        self._run(payload())
        self.assertEqual(len(self.board.sent), 1)

    def test_below_threshold_does_nothing(self):
        self._post(stars=2)
        self._run(payload())
        self.assertEqual(self.board.sent, [])

    def test_posts_only_once(self):
        self._post(stars=5)
        self._run(payload())
        self._run(payload())
        self.assertEqual(len(self.board.sent), 1, "a starred post must not repost")

    def test_non_star_emoji_ignored(self):
        self._post(stars=9)
        self._run(payload(emoji="🎉"))
        self.assertEqual(self.board.sent, [])

    def test_dm_reaction_ignored(self):
        self._post(stars=9)
        self._run(payload(guild_id=None))
        self.assertEqual(self.board.sent, [])

    def test_reactions_in_the_board_itself_ignored(self):
        self._post(stars=9)
        self._run(payload(channel_id=21))
        self.assertEqual(self.board.sent, [])

    def test_bot_authored_posts_ignored(self):
        self._post(stars=9, author=FakeAuthor(bot=True))
        self._run(payload())
        self.assertEqual(self.board.sent, [])

    def test_unconfigured_board_ignored(self):
        self.cfg["starboard_channel_id"] = None
        self._post(stars=9)
        self._run(payload())
        self.assertEqual(self.board.sent, [])

    def test_sailed_reaction_added_after_posting(self):
        msg = self._post(stars=3)
        self._run(payload())
        self.assertIn("🏅", msg.added)

    def test_image_attached_to_embed(self):
        self._post(stars=3, attachments=[FakeAttachment("https://x.invalid/i.png")])
        self._run(payload())
        self.assertEqual(self.board.sent[0].image.url, "https://x.invalid/i.png")

    def test_corrupt_store_does_not_kill_the_listener(self):
        """A truncated starboard.json used to raise on every reaction."""
        starboard.STORE.write_text("{not json")
        self._post(stars=3)
        self._run(payload())          # must not raise
        self.assertEqual(len(self.board.sent), 1)

    def test_store_roundtrip(self):
        starboard._mark(42)
        starboard._mark(7)
        self.assertEqual(starboard._posted(), {42, 7})
        self.assertEqual(json.loads(starboard.STORE.read_text()), [7, 42])

    def test_deleted_message_is_ignored(self):
        self._run(payload(mid=404))   # fetch raises NotFound
        self.assertEqual(self.board.sent, [])


class StarCountTests(unittest.TestCase):
    def test_highest_star_message_star_count_not_double_counted(self):
        """The bot adds 🏅 to mark a post as sailed; only ⭐ reactions count."""
        msg = FakeStarMessage(stars=3)
        msg.reactions.append(FakeReaction("🏅", 1))
        stars = sum(r.count for r in msg.reactions if str(r.emoji) == starboard.STAR_EMOJI)
        self.assertEqual(stars, 3)


if __name__ == "__main__":
    unittest.main()
