# Privacy and data

Inferolli is a community bot that keeps long-term memory on purpose — that is
the product. This page states exactly what is stored, where, for how long, and
how to have it removed. It is written for the people talking to it, not for the
operator.

## What is stored

| Data | Where | Retention |
|---|---|---|
| Message text observed in server channels | `data/memory/stm.json` | 7 days, capped at 2500 entries |
| Per-person profile: display names, facts learned, episodes, affinity score | `data/memory/users/<id>.json` | until it decays or is deleted |
| Private thoughts the bot forms about conversations | `data/memory/thoughts.json` | 7 days, exact duplicates dropped |
| One-line topic summary per channel | `data/memory/topics.json` | 14 days after a channel goes quiet |
| The bot's own persona, opinions, self-description | `data/memory/persona.json` | permanent |
| Per-channel agent sessions (conversation transcripts) | sandbox `~/.local/share/opencode/` | until the session is cleared or the sandbox is rebuilt |
| Moderation cases (who was warned/muted and why) | `data/memory/cases.json` | until pruned by an admin |
| Backups of the above | operator's S3 bucket | nightly rotation |
| Runtime logs | sandbox disk, `/bot/bot.log` | until the sandbox is rebuilt |

**Not stored:** message attachments are processed transiently. Passwords, tokens
and private keys are never requested and never stored — don't paste them.

## What leaves the server

To answer, the bot sends the current conversation excerpt, the assembled memory
context (profile, lore, thoughts, recall) and any tool results to its inference
provider (`api.inferenceport.ai`, or the operator's configured gateway). Web
lookups send the query text to the search provider. Nothing is sent to a
provider that the model needs to answer with.

The desktop is a separate case: the bot runs a real machine. Members receive a
signed link to **watch** it; staff and admins can also **touch** it. Whatever is
typed into that machine by a staff member is visible to anyone with the watch
link.

## Your controls

| Action | How |
|---|---|
| See what's remembered about you | ask in channel: the bot can quote its own profile |
| Have your stored profile deleted | ask an admin, or have them remove `data/memory/users/<your-id>.json` |
| Stop the bot remembering a conversation | it only stores channels it can read; remove its read access |
| Opt out of being watched/talked about | ask for your facts and episodes to be cleared; the STM copy ages out in 7 days |
| Delete everything | admins can wipe `data/memory/` — the persona and lore go with it |

Because memory is the point, "delete my data" is honoured by deletion rather
than by pretending nothing was kept. Ask and it will be removed.

## Operator responsibilities

- `.env` holds live credentials and is never committed; it is excluded from git
  and from the container image.
- `config.json` is committed and must never contain secrets — the test suite
  asserts this and CI fails if a credential pattern appears in tracked files.
- Backups contain the full cortex, including message history. Treat the bucket
  as having the same sensitivity as the server itself.
