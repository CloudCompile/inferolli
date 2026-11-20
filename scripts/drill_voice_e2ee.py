"""Does the DAVE (E2EE) handshake actually complete on this box?

Discord enforces A/V E2EE for non-stage voice calls: a client that cannot do
DAVE gets close code 4017 and never connects. A client that connects but whose
DAVE session is not *ready* sends frames with the legacy scheme, which every
listener discards — real audio, zero sound, no error.

So the only thing worth measuring is: how long until `can_encrypt` is true, and
do the packets we send afterwards look like encrypted audio?

Run it with the bot stopped (same token — two sessions kick each other):
    python3 scripts/drill_voice_e2ee.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import discord

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        if v.strip():
            os.environ[k.strip()] = v.strip().strip('"').strip("'")

from cogs import music  # noqa: E402

VOICE = 1570609109802418239
SECONDS = float(next((a.split('=')[1] for a in sys.argv if a.startswith('--seconds=')), 3))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]


def say(*a):
    print(*a, flush=True)


intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    say("logged in as", client.user)
    channel = client.get_channel(VOICE)
    if channel is None:
        say("!! channel not visible")
        return await client.close()

    music._prefer_plain_voice()   # the bot's DAVE wiring, as at runtime
    vc = await channel.connect(self_deaf=True, timeout=30)
    conn = vc._connection
    say(f"connected: mode={vc.mode} ssrc={vc.ssrc}")
    say(f"  dave={conn.dave_protocol_version} downgraded={conn.dave_downgraded}")

    t0 = time.monotonic()
    last = None
    while time.monotonic() - t0 < 25:
        state = (getattr(conn.dave_protocol_version, "real", conn.dave_protocol_version),
                 getattr(getattr(conn.dave_session, "ready", None), "real", None),
                 conn.can_encrypt)
        if state != last:
            say(f"  t={time.monotonic() - t0:5.1f}s dave={state[0]} ready={state[1]} "
                f"can_encrypt={state[2]}")
            last = state
        if conn.can_encrypt:
            break
        await asyncio.sleep(0.25)

    if not conn.can_encrypt:
        say("!! E2EE never became ready — audio would be discarded by every listener")
        await vc.disconnect()
        return await client.close()

    say(f"--> E2EE ready after {time.monotonic() - t0:.1f}s: sending a {SECONDS:g}s tone")

    captured = {"tone": [], "silence": []}
    current = ["tone"]
    original = conn.send_packet

    def logged(packet):
        captured[current[0]].append(len(packet))
        return original(packet)

    conn.send_packet = logged

    vc.play(music.ToneSource(seconds=SECONDS))
    await asyncio.sleep(SECONDS + 0.4)
    vc.stop()
    await asyncio.sleep(0.3)

    current[0] = "silence"
    vc.play(music.ToneSource(seconds=1, level=0.0))
    await asyncio.sleep(1.4)
    vc.stop()

    for label, sizes in captured.items():
        if not sizes:
            say(f"    {label}: NO PACKETS")
            continue
        say(f"    {label}: n={len(sizes)} size min={min(sizes)} max={max(sizes)} "
            f"avg={sum(sizes) / len(sizes):.0f}")
    if captured["tone"] and captured["silence"]:
        tone = sum(captured["tone"]) / len(captured["tone"])
        quiet = sum(captured["silence"]) / len(captured["silence"])
        say(f"    -> tone {'IS' if tone > quiet * 1.3 else 'is NOT'} bigger than silence")

    await vc.disconnect()
    await client.close()


client.run(TOKEN, log_handler=None)
