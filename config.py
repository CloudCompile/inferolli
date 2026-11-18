"""Per-guild configuration, persisted to data/config.json."""

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", Path(__file__).parent / "data"))
CONFIG_FILE = DATA_DIR / "config.json"

_lock = asyncio.Lock()

# Shape of a guild's config. Everything is per-guild and off by default.
DEFAULTS = {
    "mod_log_channel_id": None,      # channel where automod actions get logged
    "welcome_channel_id": None,      # channel for join greetings
    "starboard_channel_id": None,    # channel for ⭐-ed posts
    "starboard_threshold": 3,        # stars needed to sail to the starboard
    "models_channel_id": None,       # channel for model-catalog updates
    "dreams_channel_id": None,       # channel where the nightly dream gets posted
    "levels_channel_id": None,       # channel for nightly crew standings
    "automod": {
        "enabled": False,
        "delete_on_low": True,       # severity=low  -> delete message
        "timeout_on_high": True,     # severity=high -> timeout the member
        "timeout_seconds": 600,
    },
    "companion": {                   # person-mode: memory, opinions, decision-gated proactivity
        "enabled": True,
    },
}


def _deep_merge(base: dict, stored: dict) -> dict:
    """Fill any missing keys in `stored` from `base` (recursively, one level
    for section dicts). Old configs (restored from old backups) gain every
    new default key — no more KeyError on freshly-shipped sections."""
    for k, v in base.items():
        if k not in stored:
            stored[k] = json.loads(json.dumps(v))
        elif isinstance(v, dict) and isinstance(stored[k], dict):
            _deep_merge(v, stored[k])
    return stored


def _guild_path(guild_id: int) -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        all_cfg = json.load(f)
    return _deep_merge(json.loads(json.dumps(DEFAULTS)),
                       all_cfg.setdefault(str(guild_id), json.loads(json.dumps(DEFAULTS))))


async def get(guild_id: int) -> dict:
    async with _lock:
        if not CONFIG_FILE.exists():
            return json.loads(json.dumps(DEFAULTS))
        try:
            return json.loads(json.dumps(_guild_path(guild_id)))
        except (json.JSONDecodeError, OSError):
            log.warning("config.json unreadable — serving defaults")
            return json.loads(json.dumps(DEFAULTS))


async def update(guild_id: int, mutate) -> dict:
    """Atomically read-modify-write one guild's config.

    `mutate(cfg)` is called with the guild dict; it may mutate it in place
    or return a replacement. Returns the updated config.
    """
    async with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                all_cfg = json.load(f)
        else:
            all_cfg = {}

        cfg = _deep_merge(json.loads(json.dumps(DEFAULTS)),
                          all_cfg.setdefault(str(guild_id), json.loads(json.dumps(DEFAULTS))))
        result = mutate(cfg)
        if result is not None:
            all_cfg[str(guild_id)] = result

        tmp = CONFIG_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(all_cfg, f, indent=2)
        tmp.replace(CONFIG_FILE)

        cfg = all_cfg[str(guild_id)]
        return json.loads(json.dumps(cfg))
