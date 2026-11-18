"""Minimal Discord stand-ins — just enough surface for the cogs' logic."""

import discord
from types import SimpleNamespace


class FakePermissions:
    def __init__(self, **kw):
        self.administrator = kw.get("administrator", False)
        self.send_messages = kw.get("send_messages", True)
        self.read_messages = kw.get("read_messages", True)
        # a plain member does NOT hold these; tests that need them opt in
        self.manage_roles = kw.get("manage_roles", False)
        self.manage_messages = kw.get("manage_messages", False)
        self.manage_channels = kw.get("manage_channels", False)
        self.moderate_members = kw.get("moderate_members", False)
        self.kick_members = kw.get("kick_members", False)
        self.ban_members = kw.get("ban_members", False)


def _not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown message")


class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent = []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append((content, kw))
        return SimpleNamespace(id=len(self.sent))

    async def defer(self, **kw):
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return SimpleNamespace(id=1000 + len(self.sent))


class FakeInteraction:
    def __init__(self, user=None, guild=None):
        self.user = user or FakeMember()
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeRole:
    def __init__(self, id, name, position=0):
        self.id = id
        self.name = name
        self.position = position

    def __ge__(self, other):
        return self.position >= other.position

    def __gt__(self, other):
        return self.position > other.position


class FakeMember:
    def __init__(self, id=1, name="member", roles=(), bot=False, admin=False, guild=None):
        self.id = id
        self.display_name = name
        self.name = name
        self.bot = bot
        self.guild = guild
        self.roles = list(roles)
        self.guild_permissions = FakePermissions(administrator=admin)
        self.top_role = max(self.roles, key=lambda r: r.position,
                            default=FakeRole(0, "@everyone", 0))

    def __str__(self):
        return self.display_name


class FakeMessage:
    def __init__(self, id=900, content="hello", channel=None):
        self.id = id
        self.content = content
        self.channel = channel
        self.author = None
        self.attachments = []
        self.pinned = False

    async def edit(self, content=None, **kw):
        self.content = content
        return self

    async def pin(self, **kw):
        self.pinned = True
        return None


class FakeChannel:
    def __init__(self, id=10, name="general", guild=None, history=()):
        self.id = id
        self.name = name
        self.guild = guild
        self.sent = []
        self.pinned = []
        self.last_message_id = 0
        self._history = list(history)
        self._existing = {}

    def permissions_for(self, member):
        return FakePermissions()

    async def send(self, content=None, **kw):
        msg = FakeMessage(id=500 + len(self.sent), content=content, channel=self)
        self.sent.append((content, kw))
        self._existing[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        if mid in self._existing:
            return self._existing[mid]
        raise _not_found()

    async def pin(self, **kw):
        return None

    def history(self, limit=None):
        async def gen():
            for m in self._history[:limit]:
                yield m
        return gen()


class FakeGuild:
    def __init__(self, id=100, channels=None, members=None):
        self.id = id
        self.owner_id = 999
        self._channels = list(channels or [])
        self.text_channels = self._channels
        self.members = list(members or [])
        self.roles = []
        self.me = FakeMember(id=555, name="Inferolli", bot=True, guild=self)

    def get_channel(self, cid):
        return next((c for c in self._channels if c.id == cid), None)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeBot:
    """Just enough for cogs that only need .get_cog/.guilds."""

    def __init__(self, guilds=()):
        self.guilds = list(guilds)
        self._cogs = {}

    def get_cog(self, name):
        return self._cogs.get(name)

    async def wait_until_ready(self):
        return None
