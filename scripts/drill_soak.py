"""Drill: 60s of a real listening session, from an idle studio.

Reads frames exactly like discord.py's player thread does and reports every
5s whether real sound is still flowing, plus what the studio thinks it is
doing. This is the shape of what a person actually does: /play, then listen.

Run inside the sandbox: python3 /bot/drill_soak.py
"""
import asyncio
import subprocess
import threading
import time
from unittest import mock

from cogs import music


def tonearm(path, method="GET"):
    out = subprocess.run(
        ["curl", "-s", "-m", "12", "-X", method, "-H",
         f"Authorization: Bearer {music.KEY}", f"{music.URL}{path}"],
        capture_output=True, text=True).stdout
    return out


class FakeVC:
    def __init__(self):
        self.source = None
        self._playing = False
        self.channel = type("C", (), {"mention": "#voice", "name": "voice"})()

    def play(self, source, after=None):
        self.source = source
        self._playing = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self._playing:
            if not self.source.read():
                break
            time.sleep(0.02)

    def is_playing(self):
        return self._playing

    def is_connected(self):
        return True

    def stop(self):
        self._playing = False


class Player(threading.Thread):
    """Pulls frames at real time, like AudioPlayer._do_run."""

    def __init__(self, source):
        super().__init__(daemon=True)
        self.source = source
        self.running = True

    def run(self):
        while self.running:
            if not self.source.read():
                break
            time.sleep(0.02)


async def main():
    print("--> attaching to whatever the studio is doing (no poking)")

    # exactly what /play does once it decides to stream
    device = music._sink_name()
    music._wake_sink(device)
    cog = music.MusicCog(bot=mock.Mock())
    vc = FakeVC()
    why = await cog._start_stream(vc)
    print(f"    _start_stream -> {why!r}")
    attached = vc.source

    # keep reading continuously, like the player thread does
    source = attached if attached is not None else music.StudioAudio("", device)
    player = Player(source)
    player.start()
    vc._playing = True
    for i in range(1, 13):
        await asyncio.sleep(5)
        np = tonearm("/playback/now-playing")
        playing = '"isPlaying":true' in np
        inputs = subprocess.run(["pactl", "list", "short", "sink-inputs"],
                                capture_output=True, text=True,
                                env=music._pulse_env()).stdout.count("\n")
        print(f"    t={i*5:>2}s frames={source.frames:<5} silent_s={source.silent_seconds():5.1f} "
              f"stalled={source.stalled_frames:<3} sounding={source.sounding()} "
              f"studio_playing={playing} sink_inputs={inputs}")
    player.running = False
    source.cleanup()


asyncio.run(main())
