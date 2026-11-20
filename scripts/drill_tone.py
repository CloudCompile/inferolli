"""Drill: play /voicecheck's tone through a fake voice client and count frames.

Run inside the sandbox: python3 /bot/drill_tone.py
"""
import asyncio
import threading
import time
from unittest import mock

from cogs import music


class FakeVC:
    def __init__(self):
        self.source = None
        self._playing = False
        self.frames_pulled = 0
        self.channel = type("C", (), {"mention": "#voice", "name": "voice"})()

    def play(self, source, after=None):
        self.source = source
        self._playing = True

        def run():
            while self._playing:
                data = source.read()
                if not data:
                    break
                self.frames_pulled += 1
                time.sleep(0.02)
            if after:
                after(None)

        threading.Thread(target=run, daemon=True).start()

    def is_playing(self):
        return self._playing

    def is_connected(self):
        return True

    def stop(self):
        self._playing = False


async def main():
    vc = FakeVC()
    tone = music.ToneSource(seconds=6)
    started = time.monotonic()
    vc.play(tone)
    await asyncio.sleep(2.0)
    state = f"after 2s: frames={tone.frames} played={vc.frames_pulled} dead={tone.dead}"
    await asyncio.sleep(4.5)
    state2 = f"after 6.5s: frames={tone.frames} played={vc.frames_pulled} dead={tone.dead}"
    print(state)
    print(state2)
    print(f"total {time.monotonic() - started:.1f}s, {vc.frames_pulled / 50:.1f}s of audio")
    vc.stop()


asyncio.run(main())
