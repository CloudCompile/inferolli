"""roles.py — the self-assign picker: what's pickable, and button behavior."""

import asyncio
import unittest
from unittest import mock

import discord

from cogs import roles
from tests.fakes import FakeGuild, FakeMember, FakeRole


def _role(id, name, position=1, managed=False):
    r = FakeRole(id, name, position)
    r.managed = managed
    return r


class PickableTests(unittest.TestCase):
    def setUp(self):
        self.me_top = FakeRole(1, "Inferolli", position=20)

    def test_ordinary_roles_are_pickable(self):
        for name in ("🔔 pings", "🎨 Creator", "🎮 Gamer"):
            self.assertIsNone(roles.forbidden_role_reason(
                _role(5, name), self.me_top), name)

    def test_earned_ranks_are_never_pickable(self):
        from cogs.levels import TIER_NAMES
        for name in TIER_NAMES:
            self.assertIn("earned crew rank",
                          roles.forbidden_role_reason(_role(5, name), self.me_top))

    def test_staff_and_infrastructure_are_not_pickable(self):
        for name in ("Admin", "staff", "🔇 Bilged", "@everyone"):
            self.assertIsNotNone(roles.forbidden_role_reason(
                _role(5, name), self.me_top), name)

    def test_integration_managed_roles_are_not_pickable(self):
        problem = roles.forbidden_role_reason(_role(5, "Booster", managed=True), self.me_top)
        self.assertIn("integration", problem)

    def test_roles_above_the_bot_are_not_assignable(self):
        problem = roles.forbidden_role_reason(
            _role(5, "👑 Cool Kids", position=25), self.me_top)
        self.assertIn("hierarchy", problem)

    def test_missing_role(self):
        self.assertIsNotNone(roles.forbidden_role_reason(None, self.me_top))


class PickerViewTests(unittest.TestCase):
    def test_buttons_carry_stable_custom_ids(self):
        view = roles.PickerView([(111, "🔔 pings"), (222, "🎨 Creator")])
        ids = [b.custom_id for b in view.children]
        self.assertEqual(ids, ["roles:toggle:111", "roles:toggle:222"])
        self.assertIsNone(view.timeout)          # persistent across restarts

    def test_button_cap(self):
        view = roles.PickerView([(i, f"r{i}") for i in range(40)])
        self.assertEqual(len(view.children), roles.MAX_PICKABLE)


class ToggleTests(unittest.TestCase):
    """The button callback: take or drop the role, tell the member."""

    def _interaction(self, member, role):
        guild = FakeGuild()
        member.guild = guild
        inter = mock.Mock()
        inter.guild = guild
        inter.user = member
        inter.response = mock.Mock()
        inter.response.send_message = mock.AsyncMock()

        def get_role(rid):
            return role if role is not None and rid == role.id else None
        guild.get_role = get_role
        return inter

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_taking_a_role(self):
        member = FakeMember(id=7, roles=[])
        member.add_roles = mock.AsyncMock()
        member.remove_roles = mock.AsyncMock()
        role = _role(222, "🎨 Creator")
        inter = self._interaction(member, role)
        self.run_async(roles.PickerButton(222, "🎨 Creator").callback(inter))
        member.add_roles.assert_awaited_once()
        member.remove_roles.assert_not_awaited()
        self.assertTrue(  # ephemeral — a picker toggle shouldn't spam the channel
            inter.response.send_message.await_args.kwargs.get("ephemeral"))

    def test_dropping_a_role(self):
        role = _role(222, "🎨 Creator")
        member = FakeMember(id=7, roles=[role])
        member.add_roles = mock.AsyncMock()
        member.remove_roles = mock.AsyncMock()
        inter = self._interaction(member, role)
        self.run_async(roles.PickerButton(222, "🎨 Creator").callback(inter))
        member.remove_roles.assert_awaited_once()
        member.add_roles.assert_not_awaited()

    def test_deleted_role_says_so(self):
        member = FakeMember(id=7, roles=[])
        inter = self._interaction(member, None)
        self.run_async(roles.PickerButton(999, "ghost").callback(inter))
        self.assertIn("deleted", inter.response.send_message.await_args[0][0])


class RenderTests(unittest.TestCase):
    """The picker re-renders onto its stored message and repairs deletions."""

    def run_async(self, coro):
        return asyncio.run(coro)

    def _cfg_with(self, chan_id=10, msg_id=500, role_ids=None):
        async def get_cfg(guild_id):
            return {"roles_channel_id": chan_id, "picker_message_id": msg_id,
                    "pickable_roles": role_ids or [222]}
        return get_cfg

    def test_string_ids_from_json_still_resolve(self):
        """config.json round-trips role ids as strings; get_role wants ints.
        Without coercion every picker label became '(deleted role …)'."""
        from tests.fakes import FakeChannel
        chan = FakeChannel(id=10)
        guild = FakeGuild(channels=[chan])
        cog = roles.RolesCog(mock.Mock())

        def get_role(rid):
            self.assertIsInstance(rid, int)
            return _role(rid, "🎨 Creator")
        guild.get_role = get_role

        async def update_cfg(guild_id, mutate):
            return mutate  # unused

        with mock.patch.object(roles, "get_cfg", self._cfg_with(role_ids=["222"])), \
             mock.patch.object(roles, "update_cfg", update_cfg):
            msg = self.run_async(cog.render(guild))
        self.assertIsNotNone(msg)
        sent_embed = chan.sent[0][1].get("embed") if chan.sent else None
        # the button got the real label, not "(deleted role 222)"
        labels = chan.sent[0][1]["view"].children[0].label
        self.assertNotIn("deleted", labels)

    def test_render_reposts_when_the_message_is_gone(self):
        from tests.fakes import FakeChannel
        chan = FakeChannel(id=10)
        guild = FakeGuild(channels=[chan])
        bot = mock.Mock()
        bot.add_view = mock.Mock()
        cog = roles.RolesCog(bot)
        updates = []

        async def update_cfg(guild_id, mutate):
            cfg = {"picker_message_id": 500, "pickable_roles": [222],
                   "roles_channel_id": 10}
            mutate(cfg)
            updates.append(cfg)
            return cfg

        def get_role(rid):
            return _role(rid, "🎨 Creator")
        guild.get_role = get_role

        with mock.patch.object(roles, "get_cfg", self._cfg_with()), \
             mock.patch.object(roles, "update_cfg", update_cfg):
            msg = self.run_async(cog.render(guild))
        # FakeChannel.fetch_message raises NotFound for unknown ids → reposted
        self.assertIsNotNone(msg)
        self.assertEqual(len(chan.sent), 1)
        self.assertEqual(updates[0]["picker_message_id"], msg.id)
        bot.add_view.assert_called_once()


if __name__ == "__main__":
    unittest.main()