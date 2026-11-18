"""abilities.py — channel naming, resolution, the admin-only gate."""

import asyncio
import unittest
from types import SimpleNamespace

import abilities
from tests.fakes import FakeChannel, FakeGuild, FakeMember, FakeMessage


class SanitizeTests(unittest.TestCase):
    def test_spaces_and_case(self):
        self.assertEqual(abilities._sanitize_channel_name("  General Chat  "), "general-chat")

    def test_emoji_and_pipes(self):
        self.assertEqual(abilities._sanitize_channel_name("⚓│crew-rankings"), "⚓│crew-rankings")

    def test_specials_stripped(self):
        self.assertEqual(abilities._sanitize_channel_name("hello!! world?"), "hello-world")

    def test_empty_falls_back(self):
        self.assertEqual(abilities._sanitize_channel_name("!!!"), "channel")

    def test_length_capped(self):
        self.assertEqual(len(abilities._sanitize_channel_name("a" * 200)), 90)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.general = FakeChannel(id=1, name="general")
        self.crew = FakeChannel(id=2, name="⚓│crew-rankings")
        self.guild = FakeGuild(channels=[self.general, self.crew])
        self.guild.categories = [SimpleNamespace(name="Ship Life")]

    def test_exact_name(self):
        self.assertIs(abilities._resolve_channel(self.guild, "general"), self.general)

    def test_readable_part_of_styled_name(self):
        self.assertIs(abilities._resolve_channel(self.guild, "⚓│crew-rankings"), self.crew)

    def test_mention_syntax_not_resolved_by_design(self):
        # abilities receive channel NAMES from the model; raw <#id> is not supported
        self.assertIsNone(abilities._resolve_channel(self.guild, "<#1>"))

    def test_unknown_is_none(self):
        self.assertIsNone(abilities._resolve_channel(self.guild, "no-such-channel"))
        self.assertIsNone(abilities._resolve_channel(self.guild, None))

    def test_category_match(self):
        self.assertEqual(abilities._resolve_category(self.guild, "ship").name, "Ship Life")
        self.assertIsNone(abilities._resolve_category(self.guild, "nope"))


class GateTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def _msg(self, member):
        m = FakeMessage()
        m.author = member
        m.guild = FakeGuild()
        return m

    def test_admin_allowed(self):
        msg = self._msg(FakeMember(admin=True))
        self.assertIsNone(self.run_async(abilities._gate("create_channel", {}, msg)))

    def test_specific_permission_allowed(self):
        member = FakeMember(admin=False)
        member.guild_permissions.manage_roles = True
        msg = self._msg(member)
        self.assertIsNone(self.run_async(abilities._gate("create_role", {}, msg)))

    def test_member_refused(self):
        msg = self._msg(FakeMember(admin=False))
        self.assertIn("admin-only", self.run_async(abilities._gate("create_channel", {}, msg)))

    def test_unknown_action(self):
        msg = self._msg(FakeMember(admin=True))
        self.assertIn("unknown action", self.run_async(abilities._gate("nuke", {}, msg)))

    def test_non_member_author(self):
        msg = FakeMessage()
        msg.author = SimpleNamespace(id=1)      # no guild_permissions (webhook/DM)
        self.assertIn("server members", self.run_async(abilities._gate("create_channel", {}, msg)))


if __name__ == "__main__":
    unittest.main()
