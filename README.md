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

export DISCORD_BOT_TOKEN="..."
export INFERENCEPORT_API_KEY="..."
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

| Env var | Purpose | Default |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Discord application token | required |
| `INFERENCEPORT_API_KEY` | AI API key | required |
| `AI_BASE_URL` | OpenAI-compatible base URL | `https://api.inferenceport.com/v1` |
| `AI_MODEL` | Chat model | `deepseek-v4.1-flash-1017` |

Per-server settings live in `data/config.json` (auto-created). All setup and
config commands are **administrator-only**; automod exempts admins.

## Running persistently

A systemd unit ships as `discord-bot.service`:

```bash
sudo cp discord-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now discord-bot
journalctl -u discord-bot -f        # tail logs
```
