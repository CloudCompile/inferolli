"""cogs/admin.py + cogs/setup_server.py — admin gating consistency.

The tier system says "an admin is a Discord administrator OR someone wearing a
configured admin role". Command gating that only checks the Discord permission
would lock out role-based admins — a confusing split where the same person can
moderate but not configure.
"""

import unittest
from unittest import mock

import discord

from cogs import admin, setup_server
from tests.fakes import FakeGuild, FakeMember, FakeRole

ADMIN_ROLE_ID = 1565794638504136704
STAFF_ROLE_ID = 1565832623002091692


class AdminGateTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild()
        self.admin_role = FakeRole(ADMIN_ROLE_ID, "Admin", 19)
        self.staff_role = FakeRole(STAFF_ROLE_ID, "staff", 20)

    def test_discord_administrator_passes(self):
        m = FakeMember(id=1, name="boss", admin=True, guild=self.guild)
        self.assertTrue(admin._is_admin(m))

    def test_configured_admin_role_passes(self):
        """Wearing the Admin role is admin standing — same as the permission."""
        m = FakeMember(id=2, name="roler", admin=False, guild=self.guild,
                       roles=[self.admin_role])
        self.assertTrue(admin._is_admin(m))

    def test_staff_role_is_not_admin(self):
        m = FakeMember(id=3, name="staffer", admin=False, guild=self.guild,
                       roles=[self.staff_role])
        self.assertFalse(admin._is_admin(m))

    def test_plain_member_is_refused(self):
        m = FakeMember(id=4, name="member", admin=False, guild=self.guild)
        self.assertFalse(admin._is_admin(m))


class SetupGateTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuild()
        self.admin_role = FakeRole(ADMIN_ROLE_ID, "Admin", 19)

    def test_configured_admin_role_can_run_setup(self):
        m = FakeMember(id=2, name="roler", admin=False, guild=self.guild,
                       roles=[self.admin_role])
        self.assertTrue(setup_server._is_admin(m))

    def test_plain_member_cannot_run_setup(self):
        m = FakeMember(id=4, name="member", admin=False, guild=self.guild)
        self.assertFalse(setup_server._is_admin(m))


if __name__ == "__main__":
    unittest.main()
