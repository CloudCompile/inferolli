"""permissions.py — per-action tiers instead of one binary admin bit.

Binary admin meant a moderator who needs /mute also gets channel deletion.
Tiers make "what may this person do" a config decision.
"""

import unittest

import permissions
import settings
from tests.fakes import FakeGuild, FakeMember


class TierTests(unittest.TestCase):
    def test_administrator_is_admin(self):
        self.assertEqual(permissions.tier_of(FakeMember(admin=True)), "admin")

    def test_plain_member(self):
        self.assertEqual(permissions.tier_of(FakeMember(admin=False)), "member")

    def test_moderate_members_permission_is_staff(self):
        m = FakeMember(admin=False)
        m.guild_permissions.moderate_members = True
        self.assertEqual(permissions.tier_of(m), "staff")

    def test_manage_messages_permission_is_staff(self):
        m = FakeMember(admin=False)
        m.guild_permissions.manage_messages = True
        self.assertEqual(permissions.tier_of(m), "staff")

    def test_non_member_object_is_member(self):
        class NoPerms:
            pass
        self.assertEqual(permissions.tier_of(NoPerms()), "member")

    def test_guild_owner_is_admin(self):
        guild = FakeGuild()
        m = FakeMember(admin=False, guild=guild)
        guild.owner_id = m.id
        self.assertEqual(permissions.tier_of(m), "admin")


class ActionTests(unittest.TestCase):
    """Action→tier comes from config.json (permissions.tier_actions)."""

    def setUp(self):
        self._orig = settings.load
        self._cfg = {"permissions": {"tier_actions": {
            "channel.create": "admin",
            "member.mute": "staff",
            "chat": "member",
        }}}
        settings.load = lambda force=False: self._cfg

    def tearDown(self):
        settings.load = self._orig

    def test_unknown_action_defaults_to_admin(self):
        self.assertEqual(permissions.required_tier("nope.nothing"), "admin")

    def test_member_can_chat(self):
        self.assertIsNone(permissions.refusal(FakeMember(admin=False), "chat"))

    def test_member_cannot_mute(self):
        reason = permissions.refusal(FakeMember(admin=False), "member.mute")
        self.assertIn("staff", reason)

    def test_staff_can_mute_but_not_create_channels(self):
        m = FakeMember(admin=False)
        m.guild_permissions.moderate_members = True
        self.assertIsNone(permissions.refusal(m, "member.mute"))
        self.assertIsNotNone(permissions.refusal(m, "channel.create"))

    def test_admin_can_do_everything(self):
        m = FakeMember(admin=True)
        for action in ("chat", "member.mute", "channel.create"):
            self.assertIsNone(permissions.refusal(m, action))

    def test_allows_boolean(self):
        m = FakeMember(admin=False)
        m.guild_permissions.moderate_members = True
        self.assertTrue(permissions.allows(m, "member.mute"))
        self.assertFalse(permissions.allows(m, "channel.create"))

    def test_is_staff_includes_admin(self):
        self.assertTrue(permissions.is_staff(FakeMember(admin=True)))
        self.assertFalse(permissions.is_staff(FakeMember(admin=False)))


class RepoConfigTests(unittest.TestCase):
    def test_every_configured_action_has_a_valid_tier(self):
        cfg = settings.load(force=True)
        actions = cfg["permissions"]["tier_actions"]
        for action, tier in actions.items():
            if action.startswith("_"):
                continue
            self.assertIn(tier, permissions.TIERS, action)

    def test_moderation_actions_are_staff(self):
        actions = settings.load(force=True)["permissions"]["tier_actions"]
        for action in ("member.warn", "member.mute", "member.kick", "member.ban"):
            self.assertEqual(actions[action], "staff", action)


if __name__ == "__main__":
    unittest.main()
