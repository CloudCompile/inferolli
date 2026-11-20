"""Drill: what does the real voice connection actually put on the wire?

Joins with a real VoiceClient, reports the encryption the media connection
negotiated, then records every RTP packet the player sends for a tone versus
digital silence. Packets that are the same size for both mean the encoder is
being fed silence; similar-sized-but-inaudible means the listeners are
discarding them (which is what DAVE/E2EE mismatch looks like).

Run inside the sandbox: python3 /bot/drill_real.py
"""
import asyncio
import faulthandler
import os
import sys

import discord

from cogs import music

VOICE_CHANNEL = 1570609109802418239
TOKEN = os.environ["DISCORD_BOT_TOKEN"]

faulthandler.dump_traceback_later(25, exit=True)  # never hang silently

captured = {"tone": [], "silence": []}
current = ["tone"]

intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True
client = discord.Client(intents=intents)


def say(*args):
    print(*args, flush=True)


@client.event
async def on_ready():
    say("--> logged in as", client.user)
    channel = client.get_channel(VOICE_CHANNEL)
    if channel is None:
        say("!! channel not visible to this client")
        return await client.close()

    music._prefer_plain_voice()
    vc = await channel.connect(self_deaf=True, timeout=30)
    conn = vc._connection
    say(f"--> connected: mode={vc.mode} ssrc={vc.ssrc}")
    say(f"    dave_protocol_version={conn.dave_protocol_version} "
        f"downgraded={conn.dave_downgraded} can_encrypt={conn.can_encrypt} "
        f"session_ready={getattr(getattr(conn, 'dave_session', None), 'ready', None)}")
    try:
        say(f"    encoder_bitrate={vc.encoder.bitrate}")
    except Exception as exc:  # noqa: BLE001
        say(f"    encoder unavailable: {type(exc).__name__}: {exc}")

    original = conn.send_packet

    def logged(packet):
        captured[current[0]].append(packet)
        return original(packet)

    conn.send_packet = logged

    vc.play(music.ToneSource(seconds=3))
    say("    playing a 3s tone")
    await asyncio.sleep(3.4)
    vc.stop()
    await asyncio.sleep(0.3)

    current[0] = "silence"
    vc.play(music.ToneSource(seconds=1, level=0.0))
    say("    playing 1s of digital silence")
    await asyncio.sleep(1.4)
    vc.stop()

    for label, packets in captured.items():
        if not packets:
            say(f"    {label}: NO PACKETS")
            continue
        sizes = [len(p) for p in packets]
        say(f"    {label}: n={len(packets)} size min={min(sizes)} "
            f"max={max(sizes)} avg={sum(sizes) / len(sizes):.0f}")

    if captured["tone"] and captured["silence"]:
        tone = sum(len(p) for p in captured["tone"]) / len(captured["tone"])
        quiet = sum(len(p) for p in captured["silence"]) / len(captured["silence"])
        say(f"    -> tone {'IS' if tone > quiet * 1.3 else 'is NOT'} bigger than "
            f"silence ({tone:.0f} vs {quiet:.0f} bytes)")

    await vc.disconnect()
    await client.close()


try:
    client.run(TOKEN, log_handler=None)
except Exception as exc:  # noqa: BLE001
    say("!! failed:", type(exc).__name__, exc)
