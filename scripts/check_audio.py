"""Is the studio's audio actually reaching the bot right now?

Checks the whole capture path WITHOUT joining voice, so it is safe to run while
the bot is live (a second voice connection on the same token kicks the real bot
out of the channel — that is a bug we have already paid for once).

    python3 scripts/check_audio.py [song_id]

Reports: which pulse dir/sink we picked, whether the studio is playing, whether
its sink has a stream attached, and what the bot's capture actually reads.
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# .env wins over the ambient environment, exactly like bot.py
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ[k.strip()] = v

from cogs import music  # noqa: E402

ARGS = [a for a in sys.argv[1:] if not a.startswith("-")]
FLAGS = [a for a in sys.argv[1:] if a.startswith("-")]
SONG = ARGS[0] if ARGS else "617154366"


def sink_index(sink):
    """pactl's short listing gives indices, so map the name to its number."""
    out = subprocess.run(["pactl", "list", "short", "sinks"],
                         capture_output=True, text=True, env=music._pulse_env())
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] == sink:
            return parts[0]
    return None


def sink_inputs(sink):
    idx = sink_index(sink)
    if idx is None:
        return 0
    out = subprocess.run(["pactl", "list", "short", "sink-inputs"],
                         capture_output=True, text=True, env=music._pulse_env())
    return sum(1 for line in (out.stdout or "").splitlines()
               if len(line.split()) > 1 and line.split()[1] == idx)


async def main():
    print(f"pulse runtime dir : {music._pulse_runtime()}")
    sink = await asyncio.to_thread(music._sink_name)
    print(f"studio sink       : {sink}")
    if not sink:
        print("!! no studio sink — the bot would fall back to the HTTP stream")
        return

    try:
        np = await music._get("/playback/now-playing")
        print(f"studio            : playing={np.get('isPlaying')} title={np.get('title')!r}")
    except Exception as exc:
        print(f"!! studio unreachable: {exc}")
        return

    if "--no-play" not in FLAGS:
        try:
            await music._post(f"/playback/play-song/{SONG}")
            print(f"started track     : {SONG}")
        except Exception as exc:
            print(f"!! could not start a track: {exc}")

    for _ in range(8):
        await asyncio.sleep(1)
        if sink_inputs(sink):
            break

    src = await asyncio.to_thread(music._new_source)
    print(f"source mode       : {src.mode}")
    if src.mode != "pulse":
        print("!! fell back to HTTP — capture is not reading the studio's sink")
        src.cleanup()
        return

    t0 = time.monotonic()
    soak = next((float(a.split("=")[1]) for a in FLAGS if a.startswith("--soak=")), 6.0)
    pulled = 0
    last_report = 0.0
    while time.monotonic() - t0 < soak:
        src.read()
        pulled += 1
        await asyncio.sleep(0.02)
        now = time.monotonic() - t0
        if now - last_report >= 10:
            last_report = now
            print(f"    t={now:>4.0f}s frames={src.frames:<5} silent={src.silent_seconds():.1f}s "
                  f"stalled={getattr(src, 'stalled_frames', 0):<4} sink_inputs={sink_inputs(sink)}")

    elapsed = time.monotonic() - t0
    print(f"frames            : {src.frames} in {elapsed:.1f}s ({src.frames / elapsed:.0f}/s)")
    print(f"silent            : {src.silent_seconds():.1f}s")
    print(f"stalled frames    : {getattr(src, 'stalled_frames', 0)}")
    print(f"sounding          : {src.sounding()}")
    print(f"sink inputs       : {sink_inputs(sink)}")
    src.cleanup()

    ok = src.frames / elapsed > 40 and src.silent_seconds() < 1.0 and src.sounding()
    print("\nRESULT:", "audio path is live" if ok else "NOT delivering real audio")


asyncio.run(main())
