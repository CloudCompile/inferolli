# discord-bot 🤖

AI-powered Discord bot for **setting up servers**, **AI automod**, and
(optional) **proactive community chatting**. Python + discord.py, powered by
`deepseek-v4.1-flash-1017` via the OpenAI-compatible INFERENCEPORT API.

## Features

- **`/setup quickstart`** — builds a whole community layout in one go:
  categories (Information / Text / Voice), `#welcome` `#announcements` `#rules`
  `#general` `#off-topic` `#bot-commands` + voice, `Member` / `Mod` / `Muted`
  roles, and permission overwrites (read-only info channels, Muted role wired
  into every channel).
- **`/setup role|channel|lock|unlock`** — fine-grained building blocks.
- **AI AutoMod** — classifies every message (toxicity, harassment, scams,
  spam); deletes on low/medium, timeouts on high, escalates repeat offenders
  (3+ strikes/hour → 1 h timeout). Logs everything to a mod-log channel.
  **Fails open**: if the AI is unreachable, it does nothing.
- **Mentions & replies** — @ the bot (or reply to it) and it always answers with
  recent-channel context. No toggle needed.
- **Proactive mode** (off by default) — with a chance per message, joins
  conversations using the last few messages as context, plus AI welcomes for
  new members. Rate-limited per channel.
- **`/ask`** — one-off AI questions, **`/config`** — per-server toggles.

## Setup

1. **Create the application**: <https://discord.com/developers/applications> →
   New Application → Bot tab → **Reset Token** → copy it.
2. Enable both privileged intents on the Bot tab:
   **Server Members Intent** and **Message Content Intent**.
3. Configure and run:

```bash
cd ~/discord-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in the two required keys
python bot.py
```

4. Use the **Invite URL** printed in the log on startup to add the bot to your
   server (it requests the moderation/channel permissions it needs).

## First run in your server

```
/setup quickstart          ← the whole layout
/config modlog #bot-commands
/config automod on         ← AI moderation
/config proactive on       ← optional community vibes
/config show               ← see current settings
```

## Configuration

Two files, deliberately split:

| File | Holds | Committed? |
|---|---|---|
| `config.json` | models, per-job routing, timings, permission tiers | **yes** — edit this for normal changes |
| `.env` | secrets only: Discord token, API keys, backup credentials | no |

Changing a model is a `config.json` edit, not a secrets edit:

```json
{ "ai": { "model": "deepseek-v4.1-flash-1017",
          "fallback_models": ["gpt-6-luna", "lightning"],
          "jobs": { "dream": "gemini-4-hydrogen" } } }
```

**`config.json` wins** over the environment (which only supplies values the
file doesn't define, and holds the secrets). A stale `AI_MODEL` inherited from
a long-running supervisor can't silently override your config.

Per-server settings (log channels, toggles) live separately in
`data/config.json` and are managed with `/config`.

Setup and config commands are administrator-only. Moderation commands require
the **staff** tier, server-structure changes require **admin** — see
[permissions.py](permissions.py) and `permissions.tier_actions` in
`config.json`.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the pieces fit, the reply
  path, the memory model, trust boundaries.
- [docs/PRIVACY.md](docs/PRIVACY.md) — exactly what is stored, for how long, and
  how to have it removed.

## Tests

The suite is hermetic — no Discord, no network, no credentials:

```bash
python -m unittest discover -s tests    # 228 tests, ~0.3s
```

CI runs it on Python 3.11 and 3.12, plus a credential scan of tracked files and
a `config.json` validation job (`.github/workflows/tests.yml`).

## Running persistently

A systemd unit ships as `discord-bot.service`:

```bash
sudo cp discord-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now discord-bot
journalctl -u discord-bot -f        # tail logs
```
