"""settings.py — non-secret runtime configuration, read from config.json.

Secrets stay in .env (see .env.example); everything an operator might tune —
models, job routing, timings, permission tiers — lives in config.json so
changing a model doesn't mean editing a file full of credentials.

Precedence: config.json → environment variable → built-in default. The env
layer exists so a single run can be overridden without touching the repo.
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("settings")

CONFIG_PATH = Path(os.environ.get("BOT_CONFIG_FILE", Path(__file__).parent / "config.json"))

_cache: dict | None = None


def load(force: bool = False) -> dict:
    """Whole config document (cached; never raises)."""
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        _cache = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("config.json missing — using built-in defaults")
        _cache = {}
    except (json.JSONDecodeError, OSError) as e:
        log.error("config.json unreadable (%s) — using built-in defaults", e)
        _cache = {}
    return _cache


def reset() -> None:
    """Drop the cache (tests, and after a config edit)."""
    global _cache
    _cache = None


def get(path: str, default=None, env: str | None = None):
    """Read a dotted path from config.json, then env, then default.

    config.json WINS over the environment. It is the source of truth for
    non-secret settings, so editing it always takes effect — an environment
    variable trivially inherited from a long-running supervisor used to
    override it silently, which made config edits look broken.

    Env is therefore only a fallback for values config.json doesn't define
    (and the place secrets live). For a one-off override of a *configured*
    value, point BOT_CONFIG_FILE at another file.

    `env` defaults to the upper-cased path with dots as underscores:
    get("ai.model") consults AI_MODEL.
    """
    node = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            node = None
            break
        node = node[part]
    if node is not None and node != "" and node != []:
        return node

    key = env if env is not None else path.replace(".", "_").upper()
    raw = os.environ.get(key)
    if raw not in (None, ""):
        return raw
    return default


def get_list(path: str, default: list | None = None, env: str | None = None) -> list:
    """List-valued setting; accepts a JSON list or a comma-separated string."""
    val = get(path, None, env=env)
    if val is None:
        return list(default or [])
    if isinstance(val, list):
        return val
    return [p.strip() for p in str(val).split(",") if p.strip()]


def get_int(path: str, default: int, env: str | None = None) -> int:
    try:
        return int(get(path, default, env=env))
    except (TypeError, ValueError):
        return default


def get_bool(path: str, default: bool = False, env: str | None = None) -> bool:
    val = get(path, None, env=env)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


# ---- convenience wrappers for the common ones -------------------------------

def ai_model() -> str:
    return str(get("ai.model", "gpt-6-luna", env="AI_MODEL"))


def ai_base_url() -> str:
    return str(get("ai.base_url", "https://api.inferenceport.ai/v1",
                   env="AI_BASE_URL")).rstrip("/")


def ai_fallbacks() -> list[str]:
    return get_list("ai.fallback_models", ["lightning"], env="AI_FALLBACK_MODELS")


def job_model(job: str) -> str | None:
    """Per-job override (Mixture of Models). Falls back to AI_JOB_<JOB>."""
    env_key = f"AI_JOB_{job.upper().replace('-', '_')}"
    val = get(f"ai.jobs.{job}", None, env=env_key)
    return str(val).strip() if val else None


def gateway() -> tuple[str, str]:
    """(base_url, model) for the optional chat gateway."""
    base = str(get("ai.gateway.base_url", "", env="AI_GATEWAY_BASE_URL")).rstrip("/")
    model = str(get("ai.gateway.model", "", env="AI_GATEWAY_MODEL")).strip()
    return base, model


def log_level() -> str:
    return str(get("log_level", "INFO", env="BOT_LOG_LEVEL"))
