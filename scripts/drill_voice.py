"""Drill: attach the studio audio to a fake voice client and prove sound flows.

Run inside the sandbox: python3 /bot/drill_voice.py
"""
import asyncio
import threading
import time
from unittest import mock

from cogs import music


class FakeVC:
    """Mimics discord.py: a player thread pulling 20 ms frames."""

    def __init__(self):
        self.source = None
        self._playing = False
        self.channel = type("C", (), {"mention": "#voice", "name": "voice"})()

    def play(self, source):
        self.source = source
        self._playing = True

        def run():
            while self._playing:
                if not source.read():
                    break
                time.sleep(0.02)

        threading.Thread(target=run, daemon=True).start()

    def is_playing(self):
        return self._playing

    def is_connected(self):
        return True

    def stop(self):
        self._playing = False


async def main():
    cog = music.MusicCog(bot=mock.Mock())
    vc = FakeVC()
    started = time.monotonic()
    why = await cog._start_stream(vc)
    source = vc.source
    print(f"start_stream -> why={why!r} in {time.monotonic() - started:.2f}s")
    if source is not None:
        print(f"  mode={source.mode} frames={source.frames} "
              f"silent={source.silent_frames} sounding={source.sounding()}")
        vc.stop()
        source.cleanup()


asyncio.run(main())
