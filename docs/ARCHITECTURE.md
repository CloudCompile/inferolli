# Architecture

Inferolli is a Discord bot that behaves like a member rather than a command
interface: it remembers, forms opinions, watches conversations, and has a real
desktop it can operate. This document explains how the pieces fit and where
state lives.

```mermaid
flowchart TB
    subgraph DISCORD["Discord"]
        MSG["messages / mentions / replies"]
        CMD["slash commands"]
        VNC["members watch the desktop"]
    end

    subgraph CORE["bot.py — process"]
        LISTEN["on_message"]
        COMPANION["cogs/companion.py<br/>reply engine"]
        AMBIENT["cogs/ambient.py<br/>spontaneous joins"]
        THOUGHTS["cogs/thoughts.py<br/>inner voice"]
        DREAMS["cogs/dreams.py<br/>nightly consolidation"]
        MOD["cogs/modtools.py + automod.py"]
        ABIL["abilities.py<br/>server actions (tiered)"]
    end

    subgraph BRAIN["memory cortex — data/memory/*.json"]
        STM["stm.json<br/>7 days, every message"]
        USERS["users/&lt;id&gt;.json<br/>facts, episodes, affinity"]
        PERSONA["persona.json<br/>opinions, self-model"]
        THINK["thoughts.json<br/>private thoughts"]
        TOPICS["topics.json<br/>what each room is for"]
    end

    subgraph HANDS["computer — E2B sandbox"]
        OC["opencode serve :4096<br/>per-channel sessions"]
        DESK["Xvfb :99 + XFCE<br/>chromium, xdotool, scrot, ffmpeg"]
        GATE["desktop_gate.py<br/>signed RO/RW links"]
    end

    subgraph AILAYER["ai.py"]
        ROUTER["model chain<br/>per-job overrides"]
        PORT["INFERENCEPORT API"]
        GW["optional gateway"]
    end

    MSG --> LISTEN
    LISTEN --> COMPANION
    LISTEN --> AMBIENT
    COMPANION --> BRAIN
    COMPANION -->|primary reply| OC
    COMPANION -->|fallback| ROUTER
    THOUGHTS --> BRAIN
    DREAMS --> BRAIN
    DREAMS -->|consolidate| USERS
    DREAMS -->|rewrite| PERSONA
    CMD --> MOD
    CMD --> ABIL
    OC --> DESK
    GATE --> DESK
    VNC --> GATE
    ROUTER --> PORT
    ROUTER --> GW
```

## The reply path

A message that mentions the bot goes through, in order:

1. **Screen errand** (`_desktop_errand`) — if the message is a screen request
   ("open X", "search Y"), the bot does it directly, deterministically, and
   sends the screenshot before any model is involved. Model mood cannot break
   this path.
2. **Memory assembly** — the person's file, server lore, thoughts formed in
   *this* room, relevance-gated cross-channel recall, and the room-topic map.
3. **Primary engine** — the persistent `opencode` session for that channel,
   which has bash/files tools and remembers its own thread.
4. **Fallback** — an inline tool loop (`tools_server.py`) with web search,
   channel history, member profiles, docs search, and — for admins — the
   computer.
5. **Delivery** — anything dropped in `/workspace/outbox/` during the reply is
   attached; failures produce a graceful line instead of silence.

## Memory model

| Store | Holds | Lifetime |
|---|---|---|
| `stm.json` | every message seen, with optional embeddings | 7 days, 2500 entries |
| `users/<id>.json` | facts, episodes, affinity, names | until decay |
| `persona.json` | opinions with history, running jokes, self-model | permanent |
| `thoughts.json` | private observations, who they're about | 7 days |
| `topics.json` | one line per channel: what that room is for | 14 days since last update |

Retrieval is **salience × recency × relevance**, and relevance has a floor: if
nothing on file actually relates to the current conversation, recall returns
nothing rather than padding a list. Confidence in a reply should never come
from volume.

## Trust boundaries

| Tier | Who | May |
|---|---|---|
| member | everyone | chat, music, watch the desktop |
| staff | moderators | warn/mute/kick/ban, purge, pin, **touch** the desktop |
| admin | administrators | channel and role structure, shell on the computer |

Tiers come from `config.json` (`permissions`), so what a role may do is a
config decision rather than a hardcoded check. Every action declares its
minimum tier; anything unlisted defaults to admin.

## Music

The bot is the server's DJ. `tonearm` — a studio for the user's own logged-in
Apple Music account — runs **inside the same sandbox**, launched by `start.sh`
with `TONEARM_AUDIO=1 TONEARM_ENABLE_WRITES=1` on `127.0.0.1:8188`, so `/play`
never dials a box that has gone stale. It is also pulled up by the watchdog if
it dies, and `cogs/backup.py` snapshots its logged-in session with the cortex.

| Surface | What it does |
|---|---|
| `/play <anything>` | a song, album, playlist or station; autocompletes across the catalog **and** the library, incl. the account's personal stations |
| `/search <q> [kind]` | songs / albums / playlists / stations / artists, then pick one to play |
| `/station [q]` | personal stations (CJ's Station, Discovery Station…) — these exist only in the personal endpoints, not catalog search — plus Apple's |
| `/playlist list show play new add remove rename delete` | see below |

Voice audio is a live MP3 stream of that player (`discord.FFmpegPCMAudio`);
track changes are posted to `#listening-post` while a party is running.

**The ownership rule.** The bot creates playlists and edits **only those**
(`data/music.json` is the ledger). A playlist that was already in the library
is read-only to it: it can play and search it, never change it, and it says so
plainly instead of failing halfway. This is enforced in code, not in the
persona prompt, and the persona is told the same rule so it never claims
otherwise.

Two implementation details worth keeping:

- MusicKit refuses a whole-library-playlist `setQueue` on this Chromium
  (`mk-140 CONTENT_UNSUPPORTED`) even when every track plays alone, so playing
  a library playlist falls back to `play-song` + `play-later` per track,
  skipping unavailable tracks rather than sinking the whole queue.
- Apple applies deletes asynchronously (204 now, out of the index moments
  later), and leaves nameless tombstones behind, so the library walk pages
  past 100, filters empties, and verifies deletes before reporting them.

## Persistence and failure

The bot lives in one E2B sandbox (`scripts/e2b_launch.py`). Because E2B pauses
sandboxes at 24 hours, `scripts/shepherd.py` watches it, backs the cortex up to
S3 before migrations, and relaunches when needed. `cogs/backup.py` also
snapshots nightly after the dream.

Nothing critical depends on the VPS being awake: the sandbox holds the process,
and the cortex is rebuilt from S3.

## Configuration

- `config.json` — non-secret: models, job routing, timings, permission tiers.
  This is the file to edit for normal changes.
- `.env` — secrets only: Discord token, API keys, backup credentials.

`config.json` wins over the environment, then the built-in default applies.
The environment is a fallback for values config.json doesn't define (and the
home of secrets). Overriding a configured value for one run means pointing
`BOT_CONFIG_FILE` at another file — a stale env var should never silently win.
