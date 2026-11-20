"""Drill: a whole listening session, the way /play does it.

Starts a track through the bot's own studio client, attaches the audio,
then pulls frames for 60s and reports whether real sound keeps flowing.

Run inside the sandbox: python3 /bot/drill_session.py [song_id]
"""
import asyncio
import subprocess
import sys
import threading
import time
from unittest import mock

from cogs import music


class FakeVC:
    def __init__(self):
        self.source = None
        self._playing = False
        self.pulled = 0
        self.channel = type("C", (), {"mention": "#voice", "name": "voice"})()

    def play(self, source, after=None):
        self.source = source
        self._playing = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self._playing:
            if not self.source.read():
                break
            self.pulled += 1
            time.sleep(0.02)

    def is_playing(self):
        return self._playing

    def is_connected(self):
        return True

    def stop(self):
        self._playing = False


def sink_inputs():
    return subprocess.run(["pactl", "list", "short", "sink-inputs"],
                          capture_output=True, text=True,
                          env=music._pulse_env()).stdout.count("\n")


async def main(song):
    cog = music.MusicCog(bot=mock.Mock())
    try:
        await music._post(f"/playback/play-song/{song}")
        print(f"--> started song {song}")
    except Exception as exc:
        print(f"--> could not start the song: {exc}")
        return
    await asyncio.sleep(2)

    vc = FakeVC()
    t0 = time.monotonic()
    why = await cog._start_stream(vc)
    print(f"--> _start_stream -> {why!r} in {time.monotonic() - t0:.1f}s")

    for _ in range(12):
        await asyncio.sleep(5)
        np = {}
        try:
            np = await music._get("/playback/now-playing")
        except Exception:
            pass
        src = vc.source
        print(f"    t={int(time.monotonic() - t0):>2}s frames={getattr(src, 'frames', 0):<5} "
              f"silent_s={getattr(src, 'silent_seconds', lambda: 0)():5.1f} "
              f"pulled={vc.pulled:<5} studio_playing={np.get('isPlaying')} "
              f"sink_inputs={sink_inputs()}")
    vc.stop()
    if vc.source is not None:
        vc.source.cleanup()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "617154366"))
