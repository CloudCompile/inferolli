"""Drill: the real /play handler, end to end, with a fake voice client.

Runs the actual slash-command callback: tonearm playback + voice attach +
measurement, and prints what the person would see.

Run inside the sandbox: python3 /bot/drill_play.py "get lucky"
"""
import asyncio
import sys
import threading
import time
from unittest import mock

from cogs import music


class FakeVoiceChannel:
    mention = "#🎧│voice"
    name = "🎧│voice"

    def __init__(self):
        self.guild = None

    def permissions_for(self, member):
        return type("P", (), {"connect": True, "speak": True})()


class FakeVC:
    def __init__(self, channel):
        self.source = None
        self._playing = False
        self.channel = channel
        self.frames_played = 0

    def play(self, source, after=None):
        self.source = source
        self._playing = True

        def run():
            while self._playing:
                if not source.read():
                    break
                self.frames_played += 1
                time.sleep(0.02)

        threading.Thread(target=run, daemon=True).start()

    def is_playing(self):
        return self._playing

    def is_connected(self):
        return True

    def stop(self):
        self._playing = False

    async def move_to(self, channel):
        self.channel = channel


class FakeGuild:
    name = "drill"

    def __init__(self, vc):
        self.voice_client = vc
        self.me = object()

    def get_channel(self, cid):
        return None


class FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.user = type("U", (), {"id": 1, "display_name": "drill"})()
        self.user.voice = type("V", (), {"channel": guild.voice_client.channel})()
        self.response = mock.Mock()
        self.response.defer = mock.AsyncMock()
        self.response.send_message = mock.AsyncMock()
        self.response.is_done = lambda: False
        self.followup = mock.Mock()
        self.followup.send = mock.AsyncMock()


async def main(query):
    channel = FakeVoiceChannel()
    vc = FakeVC(channel)
    guild = FakeGuild(vc)
    channel.guild = guild
    cog = music.MusicCog(bot=mock.Mock())
    cog.bot.loop = asyncio.get_running_loop()

    inter = FakeInteraction(guild)
    started = time.monotonic()
    await music.MusicCog.play.callback(cog, inter, query)
    elapsed = time.monotonic() - started

    embed = inter.followup.send.call_args[1]["embed"]
    print(f"/play {query!r} took {elapsed:.2f}s")
    print(f"  title:  {embed.title}")
    print(f"  footer: {embed.footer.text if embed.footer.text else '(none)'}")
    if embed.description and "⚠️" in embed.description:
        print(f"  WARNING: {embed.description.splitlines()[-1]}")
    await asyncio.sleep(1.5)
    src = vc.source
    print(f"  played frames: {vc.frames_played} | mode: {getattr(src, 'mode', '?')} "
          f"| sounding: {getattr(src, 'sounding', lambda: None)()}")
    vc.stop()
    if hasattr(src, "cleanup"):
        src.cleanup()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "get lucky"))
