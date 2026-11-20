# Why nobody can hear the music

Voice audio goes through more links than any other feature, and every one of
them can fail while the bot reports success. This is the chain, and the
measurement that proves each link. Work down it in order — do not skip to the
end because "it says it is playing".

## The chain

1. **A source has audio.** `StudioAudio` reads the studio's PulseAudio sink
   monitor. Prove it: `scripts/check_audio.py --no-play` should report ~49-50
   frames/s with little silence.
2. **The bot can encrypt.** Discord enforces DAVE (E2EE) for non-stage voice
   calls. Two ways this kills audio silently:
   - advertising `max_dave_protocol_version=0` → voice close code **4017**,
     "E2EE/DAVE protocol required" (`_prefer_plain_voice`);
   - a session that connects but cannot encrypt → frames go out with the
     **legacy** scheme and every DAVE client discards them
     (`_voice_can_encrypt`, `davey>=0.1,<0.2` — 0.2.0 shares no usable key
     with discord.py 2.7.1).
   Prove it: `scripts/drill_voice_e2ee.py` must reach `can_encrypt=True` and
   send tone packets (~285 B) distinct from digital silence (~135 B).
3. **The listener's client receives them.** In the client's
   `~/.config/discordcanary/logs/discord-webrtc_0`:
   `Inbound stats for user: <bot id> ... packets received: N, audio frames normal: M`.
   Both must grow while a tone plays.
4. **The client renders into a sink something captures.** This is where it
   hid for days — see below.
5. **The remote desktop streams that sink.** `audio: capture
   opus:...:remote_out.monitor started` in `~/remote` logs.

The user's Discord runs *on this box* (`/remote`, Discord Canary), so their
ears are the `remote_out` sink monitor. Measured with
`parec --device=remote_out.monitor` while playing a known tone.

## The trap: deafen mutes the voice stream and never unmutes it

Deafen does not just turn volumes down inside Discord — it sets `Mute: yes` on
the `WEBRTC VoiceEngine` sink input in PulseAudio. Un-deafening does not
reliably lift that. Every flag the bot can read then looks right:

```
dave=1 · e2ee_ready=True · can_encrypt=True
Inbound stats ... packets: 415, audio frames normal: 628, lost: 0
RemoteUser::SetMute ... muted: 0 · RemoteUser::UpdateVolume ... effectiveGain: 1.99526
```

and the monitor records digital silence anyway, because the stream is muted two
layers below anything Discord reports.

This is also the honest answer to "why did I hear the deafen sound but not the
voice?" The blip comes from Chromium's audio service on the `discord-canary`
stream; voice is the `WEBRTC VoiceEngine` stream. Two streams, so one can be
muted while the other is fine — and that makes "the client cannot reach the
audio server" look true when the server was never the problem.

See it and clear it:

```bash
pactl list sink-inputs | grep -E 'application.name|Mute'   # Mute: yes?
pactl set-sink-input-mute <index> 0
```

`_join` now clears it for anyone who is not self-deafened (deafen is a choice,
a leftover mute is not), `/voicecheck` prints the line `client out … MUTED`,
and the mute is visible as `sink-input-by-application-name:WEBRTC VoiceEngine`
in `~/.config/pulse/*stream-volumes.tdb`, so it survives client restarts.

Proof the fix is real, captured from `remote_out.monitor` on this box while a
6 s tone played:

```
t 0.9-6.6s   -7.5 dBFS   A440 0.5986   A660 0.0001     <- the bot's beep
t 8.0-19.8s  -9…-13 dBFS broadband                    <- the studio's music
```

## The trap: two sound servers, one socket

Both of these listen on `/run/user/1000/pulse/native`:

- `~/remote/session.sh` starts `pulseaudio -D --exit-idle-time=-1 --disable-shm`
  (it creates the `remote_out` null sink and supervises it);
- `pipewire-pulse.socket` is enabled and socket-activates `pipewire-pulse`.

The user-level unit is now masked (`systemctl --user mask --now
pipewire-pulse.socket`), so only `pulseaudio` can own the path. A
`pipewire-pulse` process still shows in `ps` — it runs as **gdm** and owns
`/run/user/121/pulse/native`, a different runtime dir, so it cannot take
`/run/user/1000`'s socket. Check `cat /run/user/1000/pulse/pid` rather than
`pgrep`: it names the daemon that actually owns the path.

Whoever owns the path wins, and **a client keeps the connection it opened at
startup**. When the two daemons swap, apps started before the swap stay bound
to the daemon that no longer owns the path: their audio enters a graph that
nothing captures, `pactl` cannot even see their streams, and nothing errors.
Startup order is the whole bug — e.g. a client launched at 14:55 keeps playing
into the 14:35 daemon after PipeWire takes the socket at 20:18.

Diagnose it:

```bash
ss -xlp | grep pulse/native                     # more than one LISTEN = problem
for p in <client pids>; do ls -l /proc/$p/fd; done
```

and compare socket inodes against each server's peer inodes — the audio
service living on the dead daemon is the signature.

Consequences worth knowing:

- UI sounds (join chime) can still be audible while voice is silent, because
  they are different streams in different processes — see the mute trap above,
  which looks identical from the outside.
- The client's ADM keeps reporting `StartPlayout` / `Playing` / a full playout
  duration, because it *is* rendering — into the dead graph.
- Killing the losing daemon unlinks the socket path on shutdown and the
  survivor may not recreate it, so new clients get `Connection refused` until
  the surviving server is restarted. Supervisers then respawn the other one,
  so expect the flap to come back.

Fix: one sound server for the whole session, then restart anything that was
started before the fix (a Discord reload — Ctrl+R — is enough to make its
renderer and audio service re-attach). After any change, re-run the chain
above from link 3, not from link 1.

Even after that restart, check link 4's mute: a client whose ADM re-attached
cleanly still plays nothing if its voice stream came back `Mute: yes`.
