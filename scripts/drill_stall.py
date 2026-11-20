"""Drill: the failure that made the bot silent — and that it now survives.

Pauses the studio's player and suspends its sink, which is the state where
PulseAudio hands the monitor no data at all. Before the fix, StudioAudio.read()
blocked there forever: no frames, no packets, no error, and is_playing() still
true. Run inside the sandbox: python3 /bot/drill_stall.py
"""
import asyncio
import subprocess
import threading
import time
from unittest import mock

from cogs import music

KEY = music.KEY


def tonearm(path, method="POST"):
    cmd = ["curl", "-s", "-m", "15", "-X", method, "-H", f"Authorization: Bearer {KEY}",
           f"{music.URL}{path}"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout[:120]


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
                if not source.read():
                    break
                self.frames_pulled += 1
                time.sleep(0.02)

        threading.Thread(target=run, daemon=True).start()

    def is_playing(self):
        return self._playing

    def is_connected(self):
        return True

    def stop(self):
        self._playing = False


async def main():
    print("--> pausing the studio and suspending its sink")
    tonearm("/playback/pause")
    subprocess.run(["pactl", "suspend-sink", "tonearm_audio", "1"],
                   capture_output=True, env=music._pulse_env())
    await asyncio.sleep(1.0)

    print("--> old behaviour path: read 25 frames with the sink suspended")
    started = time.monotonic()
    source = music.StudioAudio("", music._sink_name())
    for _ in range(25):
        source.read()
    took = time.monotonic() - started
    print(f"    frames={source.frames} stalled={source.stalled_frames} "
          f"silent_s={source.silent_seconds():.2f} sounding={source.sounding()} "
          f"dead={source.dead} in {took:.2f}s")
    print(f"    -> {'NO HANG, dead air is measurable' if took < 20 else 'STILL BLOCKED'}")
    source.cleanup()

    print("--> now let _start_stream try to bring the studio back")
    vc = FakeVC()
    cog = music.MusicCog(bot=mock.Mock())
    t0 = time.monotonic()
    why = await cog._start_stream(vc)
    print(f"    why={why!r} after {time.monotonic() - t0:.1f}s")
    src = vc.source
    if src is not None:
        print(f"    mode={src.mode} frames={src.frames} silent={src.silent_frames} "
              f"stalled={src.stalled_frames} sounding={src.sounding()}")
    await asyncio.sleep(2)
    print(f"    frames pulled by the player: {vc.frames_pulled}")
    np = tonearm("/playback/now-playing", "GET")
    print(f"    studio now: {np}")
    vc.stop()


asyncio.run(main())
