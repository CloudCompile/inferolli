"""cogs/proposals.py — the member→admin approval flow.

This is the gate that decides whether a member's request becomes a real server
change, so the tests are adversarial: the things that must be impossible are
"a proposer approves their own proposal" and "execution happens without a
keys-holder's say-so".
"""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import proposals
from tests.fakes import FakeGuild, FakeMember


def run(coro):
    """Run a coroutine on a private loop, then drop any 24h expiry tasks so the
    loop can close cleanly instead of emitting 'task was destroyed' noise."""
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        return result
    finally:
        loop.close()


class FakeReactions:
    def __init__(self):
        self.added = []

    async def add(self, emoji):
        self.added.append(emoji)


class FakeEmbed:
    def __init__(self, **kw):
        self.title = kw.get("title", "")
        self.description = kw.get("description", "")
        self.colour = kw.get("colour", 0)

    def set_footer(self, text=None, **kw):
        self.footer = text

    def add_field(self, **kw):
        pass


class FakeMsg:
    def __init__(self, id=500):
        self.id = id
        self.embeds = [FakeEmbed(title="proposal", description="desc")]
        self.reactions = FakeReactions()
        self.edited = None
        self.channel = SimpleNamespace(mention="#proposals")

    async def add_reaction(self, emoji):
        await self.reactions.add(emoji)

    async def edit(self, embed=None, **kw):
        if embed is not None:
            self.edited = embed
        return self


class FakeTextChannel:
    def __init__(self, name="bot-commands"):
        self.name = name
        self.id = 10
        self.mention = "#" + name
        self.sent = []

    async def send(self, content=None, embed=None, **kw):
        msg = FakeMsg(id=len(self.sent) + 500)
        self.sent.append(msg)
        return msg

    async def fetch_message(self, mid):
        for m in self.sent:
            if m.id == mid:
                return m
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "nope")


class FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, cid):
        return self._channel


def payload(msg_id, member, emoji="✅", guild_perms=None):
    return SimpleNamespace(
        message_id=msg_id, member=member, emoji=SimpleNamespace(name=emoji),
        channel_id=10, guild_id=100,
    )


class ProposalCreationTests(unittest.TestCase):
    def setUp(self):
        self.channel = FakeTextChannel()
        self.cog = proposals.ProposalsCog(FakeBot(self.channel))
        self.guild = FakeGuild()
        self.guild.text_channels = [self.channel]
        self.guild.system_channel = self.channel
        self.proposer = FakeMember(id=1, name="member", guild=self.guild)

    def test_supported_action_creates_a_vote_message(self):
        async def go():
            return await self.cog.create(self.guild, {"type": "create_channel", "name": "memes"},
                                         self.proposer)

        msg = run(go())
        self.assertIsNotNone(msg)
        self.assertIn(msg.id, self.cog._open)
        self.assertIn("✅", msg.reactions.added)
        self.assertIn("❌", msg.reactions.added)

    def test_unsupported_action_is_refused(self):
        async def go():
            return await self.cog.create(self.guild, {"type": "nuke_server"}, self.proposer)

        self.assertIsNone(run(go()))
        self.assertEqual(self.cog._open, {})

    def test_every_allowed_type_is_offered_to_members(self):
        """The allowlist and abilities.execute's list must agree, otherwise a
        refusal silently becomes a dead end."""
        allowed = {"create_channel", "create_category", "create_role",
                   "archive_channel", "timeout_member", "set_topic"}
        src = __import__("inspect").getsource(
            __import__("abilities").execute)
        for atype in allowed:
            self.assertIn(f'"{atype}"', src, f"{atype} missing from execute()")


class ProposalVotingTests(unittest.TestCase):
    def setUp(self):
        self.channel = FakeTextChannel()
        self.cog = proposals.ProposalsCog(FakeBot(self.channel))
        self.guild = FakeGuild()

    def _open_proposal(self, action=None, proposer_id=1):
        msg = FakeMsg(id=500)
        self.channel.sent = [msg]
        self.cog._open[500] = {
            "action": action or {"type": "create_channel", "name": "memes"},
            "proposer": proposer_id,
            "expires": time.time() + 3600,
            "title": "open a new channel",
        }
        return msg

    def _member(self, admin=False, name="x", mid=2):
        m = FakeMember(id=mid, name=name, admin=admin, guild=self.guild)
        return m

    def test_admin_approval_executes_the_action(self):
        self._open_proposal()
        admin = self._member(admin=True, name="boss", mid=9)
        with mock.patch.object(proposals.abilities, "do_action",
                               mock.AsyncMock(return_value="created #memes")):
            run(self.cog.on_raw_reaction_add(payload(500, admin)))
        self.assertNotIn(500, self.cog._open)

    def test_rejection_does_not_execute(self):
        self._open_proposal()
        admin = self._member(admin=True, name="boss", mid=9)
        with mock.patch.object(proposals.abilities, "do_action", mock.AsyncMock()) as do:
            run(self.cog.on_raw_reaction_add(payload(500, admin, emoji="❌")))
        do.assert_not_awaited()
        self.assertNotIn(500, self.cog._open)

    def test_ordinary_member_cannot_approve(self):
        self._open_proposal()
        member = self._member(admin=False, name="nobody", mid=3)
        with mock.patch.object(proposals.abilities, "do_action", mock.AsyncMock()) as do:
            run(self.cog.on_raw_reaction_add(payload(500, member)))
        do.assert_not_awaited()
        self.assertIn(500, self.cog._open, "a non-admin vote must not close the proposal")

    def test_bot_cannot_approve(self):
        self._open_proposal()
        bot = self._member(admin=True, name="bot", mid=4)
        bot.bot = True
        with mock.patch.object(proposals.abilities, "do_action", mock.AsyncMock()) as do:
            run(self.cog.on_raw_reaction_add(payload(500, bot)))
        do.assert_not_awaited()

    def test_proposer_cannot_approve_their_own_proposal(self):
        """The escalation this flow exists to prevent: a member proposes an
        action, then reacts ✅ themselves."""
        proposer = self._member(admin=False, name="member", mid=1)
        self._open_proposal(proposer_id=proposer.id)
        with mock.patch.object(proposals.abilities, "do_action", mock.AsyncMock()) as do:
            run(self.cog.on_raw_reaction_add(payload(500, proposer)))
        do.assert_not_awaited()
        self.assertIn(500, self.cog._open)

    def test_staff_cannot_self_approve_an_admin_tier_action(self):
        """Live escalation found by these tests: a staff member holds
        manage_channels, which satisfied the old vote check, then approved
        their own proposal for an admin-tier action. The tier system must
        decide, not the handy permission."""
        staff = self._member(admin=False, name="staffer", mid=7)
        staff.guild_permissions.manage_channels = True
        self._open_proposal(action={"type": "create_channel", "name": "secret"},
                            proposer_id=staff.id)
        with mock.patch.object(proposals.abilities, "do_action", mock.AsyncMock()) as do:
            run(self.cog.on_raw_reaction_add(payload(500, staff)))
        do.assert_not_awaited()

    def test_staff_can_still_approve_a_staff_tier_action(self):
        """Tier correctness must not over-restrict: timeout_member is staff."""
        staff = self._member(admin=False, name="staffer", mid=7)
        staff.guild_permissions.moderate_members = True
        other = self._member(admin=False, name="proposer", mid=1)
        self._open_proposal(action={"type": "timeout_member", "user": "42"},
                            proposer_id=other.id)
        with mock.patch.object(proposals.abilities, "do_action",
                               mock.AsyncMock(return_value="muted")) as do:
            run(self.cog.on_raw_reaction_add(payload(500, staff)))
        do.assert_awaited_once()

    def test_staff_cannot_approve_an_admin_tier_action_by_someone_else(self):
        staff = self._member(admin=False, name="staffer", mid=7)
        staff.guild_permissions.manage_channels = True
        self._open_proposal(proposer_id=1)
        with mock.patch.object(proposals.abilities, "do_action", mock.AsyncMock()) as do:
            run(self.cog.on_raw_reaction_add(payload(500, staff)))
        do.assert_not_awaited()

    def test_expired_proposal_is_not_executed(self):
        self._open_proposal()
        self.cog._open[500]["expires"] = time.time() - 1
        admin = self._member(admin=True, name="boss", mid=9)
        with mock.patch.object(proposals.abilities, "do_action", mock.AsyncMock()) as do:
            run(self.cog.on_raw_reaction_add(payload(500, admin)))
        do.assert_not_awaited()
        self.assertNotIn(500, self.cog._open)

    def test_unknown_message_is_ignored(self):
        admin = self._member(admin=True, name="boss", mid=9)
        with mock.patch.object(proposals.abilities, "do_action", mock.AsyncMock()) as do:
            run(self.cog.on_raw_reaction_add(payload(999, admin)))
        do.assert_not_awaited()

    def test_vote_is_single_use(self):
        self._open_proposal()
        admin = self._member(admin=True, name="boss", mid=9)
        with mock.patch.object(proposals.abilities, "do_action",
                               mock.AsyncMock(return_value="created #memes")) as do:
            run(self.cog.on_raw_reaction_add(payload(500, admin)))
            run(self.cog.on_raw_reaction_add(payload(500, admin)))
        self.assertEqual(do.await_count, 1, "a second reaction must not re-execute")


if __name__ == "__main__":
    unittest.main()
