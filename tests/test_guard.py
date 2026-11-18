"""cogs/guard.py — the harbor watch: raid detection, lockdown, auto-lift.

Two behaviours the code claimed but did not have, both caught here first:
  * `/unlock` had no permission check — any member could lift a lockdown
  * the lockdown embed promised "auto-lifts in 10 minutes" while nothing ever
    lifted it, so a false positive froze the server indefinitely
"""

import asyncio
import time
import unittest
import datetime as dt
from types import SimpleNamespace
from unittest import mock

import ai
from cogs import guard
from tests.fakes import FakeMember, FakeRole


def run(coro):
    """Run to completion. Background auto-lift tasks sleep for minutes, so they
    are cancelled afterwards — but only AFTER the coroutine we care about has
    finished, otherwise cancelling kills the call itself."""
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
    return result


class _ShortSleep:
    """asyncio.sleep stand-in: record the delay instead of waiting 10 minutes."""

    def __init__(self, real):
        self.real = real
        self.calls = []

    def __call__(self, delay, *a, **kw):
        self.calls.append(delay)
        return self.real(0)


class Overwrites(dict):
    def __setitem__(self, k, v):
        super().__setitem__(k, v)

    def __or__(self, other):
        merged = Overwrites(self)
        merged.update(other)
        return merged


class FakeOverwrite:
    def __init__(self):
        self.send_messages = None


class FakeGuardChannel:
    def __init__(self, name, guild):
        self.name = name
        self.id = abs(hash(name)) % 10000
        self.guild = guild
        self.overwrites = Overwrites()
        self.edits = 0
        self.forbidden = False

    def overwrites_for(self, role):
        return self.overwrites.get(role) or FakeOverwrite()

    async def edit(self, overwrites=None, **kw):
        if self.forbidden:
            import discord
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "nope")
        self.edits += 1
        if overwrites is not None:
            self.overwrites = overwrites


class FakeGuardGuild:
    def __init__(self, channels=("#general", "#memes", "📒│mod-log")):
        self.id = 100
        self.name = "test"
        self.default_role = FakeRole(1, "@everyone", 0)
        self.text_channels = [FakeGuardChannel(n, self) for n in channels]
        self.members = []
        self._by_id = {}

    def get_member(self, uid):
        return self._by_id.get(uid)

    def add_fresh_member(self, uid, age_s=3600):
        m = FakeMember(id=uid, name=f"u{uid}", guild=self)
        m.created_at = guard.discord.utils.utcnow() - dt.timedelta(seconds=age_s)
        self._by_id[uid] = m
        self.members.append(m)
        return m


def decide_yes(*a, **k):
    async def inner():
        return {"raid": {"choice": "yes", "confidence": 0.92}}
    return inner()


class BurstDetectionTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuardGuild()
        self.cog = guard.GuardCog(SimpleNamespace())
        self._sleep = _ShortSleep(asyncio.sleep)
        self._patch = mock.patch.object(guard.asyncio, "sleep", self._sleep)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _join(self, uid):
        m = self.guild.add_fresh_member(uid)
        return m

    def test_below_threshold_does_not_ask_the_model(self):
        with mock.patch.object(guard.ai, "decide", mock.AsyncMock()) as d:
            for i in range(guard.BURST_ALERT_AT - 1):
                run(self.cog.check_join_burst(self._join(i)))
        d.assert_not_awaited()

    def test_confident_yes_locks_down(self):
        members = [self._join(i) for i in range(guard.BURST_ALERT_AT)]
        async def yes(state, questions, **kw):
            return {"raid": {"choice": "yes", "confidence": 0.92}}
        with mock.patch.object(guard.ai, "decide", yes), \
             mock.patch.object(guard, "send_mod_log", mock.AsyncMock()), \
             mock.patch("config.get", mock.AsyncMock(return_value={})):
            for m in members:
                run(self.cog.check_join_burst(m))
        self.assertIn(self.guild.id, self.cog.locked_until)
        frozen = [c for c in self.guild.text_channels if c.name != "📒│mod-log"]
        self.assertTrue(all(c.overwrites_for(self.guild.default_role).send_messages is False
                            for c in frozen))

    def test_low_confidence_does_not_lock_down(self):
        members = [self._join(i) for i in range(guard.BURST_ALERT_AT)]
        async def unsure(state, questions, **kw):
            return {"raid": {"choice": "yes", "confidence": 0.4}}
        with mock.patch.object(guard.ai, "decide", unsure), \
             mock.patch.object(guard, "send_mod_log", mock.AsyncMock()), \
             mock.patch("config.get", mock.AsyncMock(return_value={})):
            for m in members:
                run(self.cog.check_join_burst(m))
        self.assertNotIn(self.guild.id, self.cog.locked_until)

    def test_model_failure_fails_open(self):
        members = [self._join(i) for i in range(guard.BURST_ALERT_AT)]
        async def boom(*a, **k):
            raise ai.AIError("down")
        with mock.patch.object(guard.ai, "decide", boom):
            for m in members:
                run(self.cog.check_join_burst(m))
        self.assertNotIn(self.guild.id, self.cog.locked_until)

    def test_mod_log_is_never_frozen(self):
        members = [self._join(i) for i in range(guard.BURST_ALERT_AT)]
        async def yes(state, questions, **kw):
            return {"raid": {"choice": "yes", "confidence": 0.9}}
        with mock.patch.object(guard.ai, "decide", yes), \
             mock.patch.object(guard, "send_mod_log", mock.AsyncMock()), \
             mock.patch("config.get", mock.AsyncMock(return_value={})):
            for m in members:
                run(self.cog.check_join_burst(m))
        modlog = next(c for c in self.guild.text_channels if c.name == "📒│mod-log")
        self.assertIsNone(modlog.overwrites_for(self.guild.default_role).send_messages)


class LockdownLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.guild = FakeGuardGuild()
        self.cog = guard.GuardCog(SimpleNamespace())
        self._sleep = _ShortSleep(asyncio.sleep)
        self._patch = mock.patch.object(guard.asyncio, "sleep", self._sleep)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _lock(self):
        with mock.patch.object(guard, "send_mod_log", mock.AsyncMock()), \
             mock.patch("config.get", mock.AsyncMock(return_value={})):
            run(self.cog.lockdown(self.guild, "test raid"))

    def test_lockdown_freezes_then_lifts(self):
        self._lock()
        frozen = [c for c in self.guild.text_channels if c.name != "📒│mod-log"]
        self.assertTrue(all(c.overwrites_for(self.guild.default_role).send_messages is False
                            for c in frozen))
        with mock.patch.object(guard, "send_mod_log", mock.AsyncMock()):
            lifted = run(self.cog.lift_lockdown(self.guild))
        self.assertTrue(lifted)
        self.assertTrue(all(c.overwrites_for(self.guild.default_role).send_messages is None
                            for c in frozen), "every channel must be unfrozen again")

    def test_lift_when_not_locked_returns_false(self):
        self.assertFalse(run(self.cog.lift_lockdown(self.guild)))

    def test_double_lockdown_is_idempotent(self):
        self._lock()
        first = self.cog.locked_until[self.guild.id]
        self._lock()
        self.assertEqual(self.cog.locked_until[self.guild.id], first)

    def test_lockdown_schedules_an_auto_lift(self):
        """The embed promises auto-lift; nothing implemented it before."""
        self._lock()
        self.assertTrue(self._sleep.calls,
                        "a lockdown must schedule its own release")

    def test_auto_lift_releases_the_server(self):
        with mock.patch.object(guard, "send_mod_log", mock.AsyncMock()), \
             mock.patch("config.get", mock.AsyncMock(return_value={})):
            run(self.cog.lockdown(self.guild, "test raid"))
            self.assertIn(self.guild.id, self.cog.locked_until)
            run(self.cog._auto_lift_task(self.guild, 10))
        self.assertNotIn(self.guild.id, self.cog.locked_until)
        frozen = [c for c in self.guild.text_channels if c.name != "📒│mod-log"]
        self.assertTrue(all(c.overwrites_for(self.guild.default_role).send_messages is None
                            for c in frozen))


class UnlockPermissionTests(unittest.TestCase):
    """The live hole: /unlock checked nothing, so any member could lift it."""

    def setUp(self):
        self.guild = FakeGuardGuild()
        self.cog = guard.GuardCog(SimpleNamespace())

    def _interaction(self, member):
        sent = {}

        class Resp:
            async def send_message(self, content=None, ephemeral=False, **kw):
                sent["msg"] = content

        return SimpleNamespace(guild=self.guild, user=member, response=Resp()), sent

    def test_ordinary_member_cannot_unlock(self):
        self.cog.locked_until[self.guild.id] = time.time() + 600
        member = FakeMember(id=3, name="member", guild=self.guild)
        inter, sent = self._interaction(member)
        run(self.cog.unlock.callback(self.cog, inter))
        self.assertIn(self.guild.id, self.cog.locked_until, "still locked")
        self.assertIn("admin", sent["msg"].lower())

    def test_admin_can_unlock(self):
        self.cog.locked_until[self.guild.id] = time.time() + 600
        admin = FakeMember(id=9, name="boss", admin=True, guild=self.guild)
        inter, sent = self._interaction(admin)
        run(self.cog.unlock.callback(self.cog, inter))
        self.assertNotIn(self.guild.id, self.cog.locked_until)
        self.assertIn("lifted", sent["msg"].lower())


if __name__ == "__main__":
    unittest.main()
