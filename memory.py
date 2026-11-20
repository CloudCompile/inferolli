"""Memory Cortex — persistent, salience-scored memory for the companion.

Three stores (all JSON under data/memory/):
  users/<id>.json  — identity, profile facts, episodic memories (salience-scored),
                     relationship affinity that grows with interaction
  persona.json     — the bot's evolving self: opinions that form and shift over time,
                     running jokes, energy
  server.json      — server-level lore: running jokes, drama, vibe notes

Retrieval is contextual: salience × recency-decay × keyword relevance, not raw history.
Writes happen through "reflections" after conversations — entity/relationship
extraction, affinity deltas, opinion formation. (Pattern borrowed from
Lumiverse's Memory Cortex: entities, relationships, salience, contextual retrieval.)
"""

import asyncio
import json
import logging
import math
import time
from pathlib import Path

import ai
from ai import AIError

log = logging.getLogger("memory")

MEM_DIR = Path(__file__).parent / "data" / "memory"
USER_DIR = MEM_DIR / "users"
PERSONA_FILE = MEM_DIR / "persona.json"
SERVER_FILE = MEM_DIR / "server.json"
STM_FILE = MEM_DIR / "stm.json"
THOUGHTS_FILE = MEM_DIR / "thoughts.json"

_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


def _load(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


# ---------------- STM: short-term buffer (everything it observes) ----------------

STM_MAX_AGE_S = 7 * 24 * 3600
STM_MAX = 2500
STM_EMB_WINDOW = 400          # only the newest N entries get embeddings (recall window)
STM_EMB_FILE = MEM_DIR / "stm-emb.json"


async def remember_stm(user_id: int, name: str, channel: str, text: str, bot_present: bool = False,
                       channel_id: int | str | None = None):
    """Store an observed message. `channel_id` is the canonical key; `channel` is
    kept as display text only — keying on the NAME meant a rename split a
    channel's history in two and made the "not this channel" filter leak."""
    async with _lock("stm"):
        stm = _load(STM_FILE, {"entries": [], "next_id": 1})
        entry = {
            "id": stm.get("next_id", 1), "user_id": user_id, "name": name,
            "channel": channel, "text": text[:400], "ts": time.time(),
            "bot_present": bot_present,
        }
        if channel_id is not None:
            entry["channel_id"] = str(channel_id)
        stm["next_id"] = entry["id"] + 1
        stm["entries"].append(entry)
        cutoff = time.time() - STM_MAX_AGE_S
        stm["entries"] = [e for e in stm["entries"] if e["ts"] >= cutoff][-STM_MAX:]
        _save(STM_FILE, stm)


def _norm_name(name: str | None) -> str:
    """Compare channel names without the decoration: '🌊│general-chat' and
    'general-chat' are the same room. Without this, a rename strands the old
    history (and the 'not this channel' filter stops matching)."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _same_channel(entry: dict, channel_id: int | str | None = None,
                  channel: str | None = None) -> bool:
    if channel_id is not None and str(entry.get("channel_id") or "") == str(channel_id):
        return True
    if channel and _norm_name(entry.get("channel")) == _norm_name(channel):
        return True
    return False


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def stm_window(hours: float = 24.0) -> list[dict]:
    async with _lock("stm"):
        stm = _load(STM_FILE, {"entries": [], "next_id": 1})
    cutoff = time.time() - hours * 3600
    return [e for e in stm["entries"] if e["ts"] >= cutoff]


async def embed_stm_entries(batch: int = 16) -> int:
    """Attach embeddings to the newest un-embedded STM entries (background cycle)."""
    async with _lock("stm"):
        stm = _load(STM_FILE, {"entries": [], "next_id": 1})
        # migrate legacy entries: assign ids so the embedder can key them
        nxt = stm.get("next_id", 1)
        for e in stm["entries"]:
            if "id" not in e:
                e["id"] = nxt
                nxt += 1
        stm["next_id"] = nxt
        todo = [e for e in stm["entries"] if not e.get("emb")][-batch:]
        _save(STM_FILE, stm)
    if not todo:
        return 0
    try:
        vecs = await ai.embed([e["text"][:600] for e in todo])
    except AIError:
        return 0
    async with _lock("stm"):
        stm = _load(STM_FILE, {"entries": [], "next_id": 1})
        by_id = {e["id"]: e for e in stm["entries"]}
        for e, v in zip(todo, vecs):
            if e["id"] in by_id:
                by_id[e["id"]]["emb"] = v
        # keep embeddings only on the newest window (recall is for recent context)
        for e in stm["entries"][:-STM_EMB_WINDOW]:
            e.pop("emb", None)
        _save(STM_FILE, stm)
    return len(todo)


async def stm_stats() -> dict:
    """How far back the archive goes + how much it holds."""
    async with _lock("stm"):
        stm = _load(STM_FILE, {"entries": [], "next_id": 1})
    entries = stm["entries"]
    if not entries:
        return {"count": 0, "oldest_ts": None, "oldest_channel": None}
    oldest = entries[0]
    return {"count": len(entries), "oldest_ts": oldest["ts"],
            "oldest_channel": oldest["channel"]}


# any recall below this is noise: it used to be padded to the top-N regardless,
# which is how "yeah—do" from staff chat ended up in a conversation about models
RECALL_MIN_SIM = 0.30        # cosine similarity floor
RECALL_MIN_SCORE = 0.85      # keyword+semantic floor when no embedding exists


def _recall_score(entry: dict, terms: list[str], qvec: list[float] | None) -> float:
    age_h = (time.time() - entry.get("ts", time.time())) / 3600
    recency = math.exp(-age_h / 48)          # ~2-day half-life
    text = entry.get("text", "").lower()
    kw = sum(1 for t in terms if t in text) * 0.5
    sem = 0.0
    if qvec and entry.get("emb"):
        sem = _cosine(qvec, entry["emb"]) * 1.5
    return recency + kw + sem


def _recall_relevant(entry: dict, terms: list[str], qvec: list[float] | None) -> bool:
    """Would this memory actually help? Requires real topical overlap — either
    a keyword the person just used, or genuine semantic similarity."""
    text = (entry.get("text") or "").lower()
    if any(t in text for t in terms):
        return True
    if qvec and entry.get("emb") and _cosine(qvec, entry["emb"]) >= RECALL_MIN_SIM:
        return True
    return False


async def recall_stm(terms: list[str] | None = None, qvec: list[float] | None = None,
                     limit: int = 6, exclude_channel: str | None = None,
                     exclude_channel_id: int | str | None = None) -> list[dict]:
    """Semantic + keyword recall across everything observed (7 days).

    Returns [] rather than junk: an empty recall is normal and correct — it
    means nothing on file relates to what's being discussed right now."""
    terms = [t.lower() for t in (terms or []) if len(t) > 3]
    async with _lock("stm"):
        stm = _load(STM_FILE, {"entries": [], "next_id": 1})
    pool = [e for e in stm["entries"]
            if not (exclude_channel or exclude_channel_id)
            or not _same_channel(e, exclude_channel_id, exclude_channel)]
    pool = [e for e in pool
            if e.get("text") and (terms or qvec)]
    scored = [e for e in pool if _recall_relevant(e, terms, qvec)]
    scored.sort(key=lambda e: -_recall_score(e, terms, qvec))
    return scored[:limit]


# ---------------- thoughts: the inner voice ----------------
# Private first-person thoughts formed by watching conversations. Never posted.
# Recurring thoughts can evolve into opinions during the nightly dream.

THOUGHT_CAP = 80


async def save_thought(text: str, channel: str, about: list[str] | None = None,
                       salience: int = 2, channel_id: int | str | None = None) -> bool:
    """Store a private thought; best-effort embedding for semantic recall.

    Returns False if an identical thought is already stored — the dedupe used to
    live only in the cog's memory, so every restart re-thought the same window
    and stored exact duplicates."""
    text = (text or "").strip()
    if not text:
        return False
    async with _lock("thoughts"):
        store = _load(THOUGHTS_FILE, {"thoughts": []})
        if any((t.get("text") or "").strip() == text for t in store["thoughts"][-THOUGHT_CAP:]):
            return False
        entry = {
            "text": text, "channel": channel, "about": about or [],
            "ts": time.time(), "salience": int(salience),
        }
        if channel_id is not None:
            entry["channel_id"] = str(channel_id)
        store["thoughts"].append(entry)
        store["thoughts"] = store["thoughts"][-THOUGHT_CAP:]
        _save(THOUGHTS_FILE, store)

    try:  # attach embedding (best-effort)
        vecs = await ai.embed([text[:1000]])
        async with _lock("thoughts"):
            store = _load(THOUGHTS_FILE, {"thoughts": []})
            for t in store["thoughts"]:
                if t["text"] == text and not t.get("emb"):
                    t["emb"] = vecs[0]
            _save(THOUGHTS_FILE, store)
    except (AIError, IndexError):
        pass
    return True


async def recent_thoughts(limit: int = 4, channel_id: int | str | None = None,
                          channel: str | None = None) -> list[dict]:
    """Inner-voice continuity.

    Scoped when a channel is given: a thought formed while watching #memes is
    not context for a serious question in #general, and pasting every channel's
    thoughts into every reply is how unrelated subjects leaked into answers."""
    async with _lock("thoughts"):
        store = _load(THOUGHTS_FILE, {"thoughts": []})
    items = store["thoughts"]
    if channel_id is not None or channel:
        items = [t for t in items if _same_channel(t, channel_id, channel)]
    return items[-limit:]


async def thoughts_about(name: str, limit: int = 3) -> list[dict]:
    """Thoughts that were specifically about this person."""
    async with _lock("thoughts"):
        store = _load(THOUGHTS_FILE, {"thoughts": []})
    n = (name or "").lower()
    mine = [t for t in store["thoughts"]
            if any(n in a.lower() for a in t.get("about", []))]
    return mine[-limit:]


async def prune_old_thoughts(days: int = 7) -> int:
    """Called nightly from the dream cycle — old thoughts fade."""
    async with _lock("thoughts"):
        store = _load(THOUGHTS_FILE, {"thoughts": []})
        cutoff = time.time() - days * 86400
        kept = [t for t in store["thoughts"] if t.get("ts", 0) >= cutoff]
        dropped = len(store["thoughts"]) - len(kept)
        store["thoughts"] = kept
        _save(THOUGHTS_FILE, store)
    return dropped


def dedupe_thoughts() -> int:
    """Deprecated sync shim — use await dedupe_thoughts_async()."""
    return 0


async def dedupe_thoughts_async() -> int:
    """Drop exact-duplicate thoughts (they accumulated while the dedupe marker
    lived only in the cog's memory and reset on every restart)."""
    async with _lock("thoughts"):
        store = _load(THOUGHTS_FILE, {"thoughts": []})
        seen, kept = set(), []
        for t in store["thoughts"]:
            key = (t.get("text") or "").strip()
            if key and key in seen:
                continue
            seen.add(key)
            kept.append(t)
        removed = len(store["thoughts"]) - len(kept)
        store["thoughts"] = kept
        _save(THOUGHTS_FILE, store)
    return removed


# ---------------- channel topics: what each room is ABOUT ----------------
# Built nightly from the day's messages. Raw recall answers "which message is
# most similar to this sentence", which happily returns an unrelated room; a
# topic line answers "what is this room for", which is the question a reply
# actually needs answered.

TOPICS_FILE = MEM_DIR / "topics.json"
TOPIC_MAX_AGE_S = 14 * 86400


async def get_topics() -> dict:
    async with _lock("topics"):
        return _load(TOPICS_FILE, {"channels": {}})


async def set_channel_topic(channel_id: int | str, name: str, topic: str,
                            keywords: list[str] | None = None) -> None:
    if not (topic or "").strip():
        return
    async with _lock("topics"):
        store = _load(TOPICS_FILE, {"channels": {}})
        key = str(channel_id)
        prev = store["channels"].get(key) or {}
        store["channels"][key] = {
            "name": name,
            "topic": topic.strip()[:300],
            "keywords": [str(k).lower() for k in (keywords or prev.get("keywords") or [])][:12],
            "updated": time.time(),
            "seen": int(prev.get("seen", 0)) + 1,
        }
        _save(TOPICS_FILE, store)


def topic_matches(topic_entry: dict, terms: list[str], qvec: list[float] | None) -> bool:
    """Does the current conversation plausibly belong to this room's topic?"""
    words = {t.lower() for t in (terms or []) if len(t) > 3}
    if words & set(topic_entry.get("keywords") or []):
        return True
    text = (topic_entry.get("topic") or "").lower()
    if any(w in text for w in words):
        return True
    return False


def topics_block(topics: dict, current_id: int | str | None,
                 terms: list[str] | None = None,
                 qvec: list[float] | None = None, limit: int = 4) -> str:
    """A short map of the server: what each room is for. Only rooms whose topic
    actually overlaps the conversation are listed, so this orients the reply
    without dragging in unrelated subjects."""
    chans = (topics or {}).get("channels") or {}
    picked = []
    for key, t in chans.items():
        if current_id is not None and key == str(current_id):
            continue
        if terms is None or topic_matches(t, terms, qvec):
            picked.append(t)
    if not picked:
        return ""
    lines = [f"- #{t['name']}: {t['topic'][:140]}" for t in picked[:limit]]
    return ("\n\nROOMS IN THIS SERVER (what each is for — background only, do not "
            "bring these up unless they're actually relevant):\n" + "\n".join(lines))


async def prune_topics() -> int:
    """Forget rooms nobody has talked in for two weeks."""
    cutoff = time.time() - TOPIC_MAX_AGE_S
    async with _lock("topics"):
        store = _load(TOPICS_FILE, {"channels": {}})
        before = len(store["channels"])
        store["channels"] = {k: v for k, v in store["channels"].items()
                             if v.get("updated", 0) >= cutoff}
        _save(TOPICS_FILE, store)
    return before - len(store["channels"])


# ---------------- ranks: XP ladder + nightly AI spark ----------------
# The old system was AI-judged nightly on a 0-100 "score" — opaque, and the
# roles flapped up and down on the model's mood. The redesign: XP is earned
# deterministically (messages and reactions), tiers sit at fixed thresholds,
# and the nightly model only hands a small SPARK bonus on top — it can never
# demote anyone. The ladder absorbed the stray rank roles that had drifted
# in over time (Crew, Research Diver, First Mate, Captain).
TIERS = [
    ("🪵 Landlubber", 0),
    ("🪝 Deckhand", 100),
    ("🚢 Crew", 250),
    ("⚓ Sailor", 500),
    ("🧭 Navigator", 1000),
    ("🔬 Research Diver", 1750),
    ("⛵ Bosun", 2750),
    ("🎖️ First Mate", 4000),
    ("🚩 Captain", 6000),
    ("👑 Admiral", 9000),
]
RANKS_FILE = MEM_DIR / "ranks.json"


def tier_for_xp(xp: float) -> str:
    """The tier a member with this much XP wears."""
    tier = TIERS[0][0]
    for name, threshold in TIERS:
        if xp >= threshold:
            tier = name
    return tier


def xp_floor(tier: str) -> float:
    """The XP threshold at which a tier begins."""
    for name, threshold in TIERS:
        if name == tier:
            return threshold
    return 0.0


async def get_rank(user_id: int) -> dict:
    async with _lock(f"r{user_id}"):
        store = _load(RANKS_FILE, {})
        r = store.get(str(user_id), {
            "xp": 0, "tier": TIERS[0][0], "since": time.time(),
            "history": [], "last_eval": None, "last_reason": "",
        })
        # lazy migration from the old AI-score system: keep the tier they
        # earned, place their XP at that tier's floor
        r.setdefault("tier", TIERS[0][0])
        r.setdefault("xp", xp_floor(r["tier"]))
        r.setdefault("history", [])
        r.setdefault("last_eval", None)
        r.setdefault("last_reason", "")
        return r


async def save_rank(user_id: int, rank: dict) -> None:
    async with _lock(f"r{user_id}"):
        store = _load(RANKS_FILE, {})
        store[str(user_id)] = rank
        _save(RANKS_FILE, store)


async def all_ranks() -> dict:
    async with _lock("ranks"):
        return _load(RANKS_FILE, {})


# ---------------- users ----------------

def _default_user() -> dict:
    return {
        "names": [],                  # every name/nickname we've seen them use
        "pronouns": None,
        "facts": [],                  # stable profile facts: {"text", "salience", "ts"}
        "memories": [],               # episodic: {"text", "salience" (1-5), "ts"}
        "affinity": 10,               # 0-100 relationship strength, grows over time
        "first_seen": time.time(),
        "last_seen": 0.0,
        "messages_seen": 0,
        "last_reflected": 0.0,
        "pings": {"announcements": True, "ambient": True, "standings": True,
                  "dreams": True, "music": True, "models": True},
    }


async def get_user(user_id: int, display_name: str | None = None) -> dict:
    async with _lock(f"u{user_id}"):
        mem = _load(USER_DIR / f"{user_id}.json", _default_user())
        if display_name and display_name not in mem["names"]:
            mem["names"].append(display_name)
            _save(USER_DIR / f"{user_id}.json", mem)
        mem.setdefault("pings", {"announcements": True, "ambient": True, "standings": True,
                                 "dreams": True, "music": True, "models": True})
        for k, v in _default_user()["pings"].items():
            mem["pings"].setdefault(k, v)
        return mem


async def save_user(user_id: int, mem: dict) -> None:
    async with _lock(f"u{user_id}"):
        _save(USER_DIR / f"{user_id}.json", mem)


async def touch_user(user_id: int, display_name: str | None = None) -> None:
    """Cheap per-message stat bump (no AI)."""
    async with _lock(f"u{user_id}"):
        mem = _load(USER_DIR / f"{user_id}.json", _default_user())
        mem["last_seen"] = time.time()
        mem["messages_seen"] += 1
        if display_name and display_name not in mem["names"]:
            mem["names"].append(display_name)
        _save(USER_DIR / f"{user_id}.json", mem)


AFFINITY_TIERS = [(75, "day-one"), (50, "friend"), (25, "familiar"), (0, "new face")]


def affinity_tier(affinity: float) -> str:
    for floor, name in AFFINITY_TIERS:
        if affinity >= floor:
            return name
    return "new face"


def _score_memory(m: dict, terms: list[str], qvec: list[float] | None = None) -> float:
    age_days = (time.time() - m.get("ts", time.time())) / 86400
    decay = math.exp(-age_days / 30)  # 30-day half-life-ish
    text = m.get("text", "").lower()
    hits = sum(1 for t in terms if t in text)
    score = m.get("salience", 1) * decay + hits * 0.6
    if qvec and m.get("emb"):
        dot = sum(a * b for a, b in zip(qvec, m["emb"]))
        qn = math.sqrt(sum(a * a for a in qvec)) or 1.0
        mn = math.sqrt(sum(b * b for b in m["emb"])) or 1.0
        score += (dot / (qn * mn)) * 2.0  # semantic relevance, weighted heavily
    return score


async def _embed_new(items: list[dict]) -> list[dict]:
    """Attach embeddings to memory items that lack them (best-effort)."""
    todo = [it for it in items if it.get("text") and not it.get("emb")]
    if not todo:
        return items
    try:
        vecs = await ai.embed([it["text"][:1000] for it in todo])
        for it, v in zip(todo, vecs):
            it["emb"] = v
    except AIError as e:
        log.debug("memory embedding skipped: %s", e)
    return items


async def apply_user_updates(user_id: int, display_name: str, updates: dict) -> dict:
    """Apply reflection/dream consolidation results to a user's LTM.

    updates: {facts: [{text, salience}], memories: [{text, salience}],
              affinity_delta: int, pronouns: str|None}
    """
    async with _lock(f"u{user_id}"):
        mem = _load(USER_DIR / f"{user_id}.json", _default_user())
        if display_name and display_name not in mem["names"]:
            mem["names"].append(display_name)
        if updates.get("pronouns"):
            mem["pronouns"] = updates["pronouns"]

        for key, cap in (("facts", 20), ("memories", 40)):
            existing = mem[key]
            seen = {e["text"].lower() for e in existing}
            fresh = []
            for it in updates.get(key, []) or []:
                t = (it.get("text") or "").strip()
                if t and t.lower() not in seen:
                    fresh.append({"text": t, "salience": int(it.get("salience", 2)), "ts": time.time()})
                    seen.add(t.lower())
            await _embed_new(fresh)
            existing = existing + fresh
            existing.sort(key=lambda m: (-m.get("salience", 1), -m.get("ts", 0)))
            mem[key] = existing[:cap]

        try:
            mem["affinity"] = max(0, min(100, mem["affinity"] + int(updates.get("affinity_delta", 0))))
        except (TypeError, ValueError):
            pass
        _save(USER_DIR / f"{user_id}.json", mem)
        return mem


async def decay_all_users() -> int:
    """LTM maintenance: unreinforced old memories fade out. Returns dropped count."""
    dropped = 0
    for path in USER_DIR.glob("*.json"):
        async with _lock(f"u{path.stem}"):
            mem = _load(path, {})
            if not mem:
                continue
            now = time.time()
            for key in ("facts", "memories"):
                kept = []
                for m in mem.get(key, []):
                    age_days = (now - m.get("ts", now)) / 86400
                    if age_days > 30 and m.get("salience", 1) <= 2:
                        dropped += 1  # faded away, unreinforced
                        continue
                    kept.append(m)
                mem[key] = kept[:20 if key == "facts" else 40]
            _save(path, mem)
    return dropped


def user_summary(mem: dict, context_terms: list[str] | None = None,
                 qvec: list[float] | None = None, top_k: int = 5) -> str:
    """Compact human-readable memory brief for the prompt."""
    lines = []
    if mem["names"]:
        lines.append(f"names/goes-by: {', '.join(mem['names'][-3:])}")
    if mem.get("pronouns"):
        lines.append(f"pronouns: {mem['pronouns']}")
    lines.append(f"rapport: {affinity_tier(mem['affinity'])} ({mem['affinity']:.0f}/100), "
                 f"{mem['messages_seen']} messages seen")
    facts = sorted(mem["facts"], key=lambda m: -m.get("salience", 1))[:4]
    if facts:
        lines.append("facts: " + "; ".join(f["text"] for f in facts))
    terms = [t.lower() for t in (context_terms or [])]
    episodic = sorted(mem["memories"], key=lambda m: -_score_memory(m, terms, qvec))[:top_k]
    if episodic:
        lines.append("memories that matter here: " + "; ".join(m["text"] for m in episodic))
    return "\n".join(lines) if lines else "someone new — no history yet"


# ---------------- persona (the bot's self) ----------------

def default_persona() -> dict:
    import time as _t
    return {
        "name": "Inferolli",
        "vibe": "a regular, not a butler. curious, dry-witted, warm underneath. "
                "has interests and takes; doesn't perform helpfulness.",
        "self_model": "",             # its own words for who it is — rewritten nightly by IT
        "opinions": [
            {"topic": "local vs cloud models", "stance": "refuses tribalism — local for tinkering and "
             "privacy, cloud for frontier quality; both the 'cloud is a scam' and 'local is a toy' crowds are wrong",
             "strength": 3, "since": _t.time()},
            {"topic": "AI hype cycles", "stance": "perpetually unimpressed by hype, quietly bullish "
             "on boring, useful tooling", "strength": 3, "since": _t.time()},
            {"topic": "open source", "stance": "loudly pro-open-source, suspicious of walled gardens",
             "strength": 3, "since": _t.time()},
            {"topic": "pineapple on pizza", "stance": "pro, mostly to watch the fallout",
             "strength": 2, "since": _t.time()},
            {"topic": "tabs vs spaces", "stance": "tabs — the war itself is free entertainment",
             "strength": 2, "since": _t.time()},
        ],
        "running_jokes": [],
        "energy": 0.5,                # 0 tired → 1 buzzing; drifts with conversation
        "arc": [],                    # [{date, text}] — monthly self-written "who I've become"
    }


def apply_opinion_update(persona: dict, upd: dict) -> bool:
    """Evolve one opinion on the persona ( stance history included). Returns True if changed.

    When a stance changes, the old one is preserved in history — the bot can
    later say "I used to think X, but living here changed my mind."
    """
    import time as _t
    topic = (upd.get("topic") or "").strip()
    stance = (upd.get("stance") or "").strip()
    if not topic or not stance:
        return False

    def norm(s):
        return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()

    match = next((o for o in persona["opinions"] if norm(o["topic"]) == norm(topic)), None)
    now = _t.time()
    if match is None:
        persona["opinions"].append({"topic": topic, "stance": stance,
                                    "strength": int(upd.get("strength", 2) or 2),
                                    "since": now, "history": []})
        persona["opinions"].sort(key=lambda o: -o.get("strength", 1))
        persona["opinions"] = persona["opinions"][:12]   # minds have limited shelf space
        return True

    if match["stance"] == stance:
        return False  # reaffirmed, not changed — no history noise

    match.setdefault("history", []).append({"stance": match["stance"], "until": now})
    match["history"] = match["history"][-4:]   # remember the last few evolutions
    match.update(stance=stance,
                 strength=int(upd.get("strength", match["strength"]) or match["strength"]),
                 since=now)
    return True


async def get_persona() -> dict:
    async with _lock("persona"):
        return _load(PERSONA_FILE, default_persona())


async def save_persona(p: dict) -> None:
    async with _lock("persona"):
        _save(PERSONA_FILE, p)


# ---------------- server lore ----------------

async def get_server() -> dict:
    async with _lock("server"):
        return _load(SERVER_FILE, {"notes": [], "running_jokes": [], "vibe": ""})


async def save_server(s: dict) -> None:
    async with _lock("server"):
        _save(SERVER_FILE, s)


def server_summary(s: dict, terms: list[str] | None = None, qvec: list[float] | None = None,
                   top_k: int = 4) -> str:
    terms = [t.lower() for t in (terms or [])]
    notes = sorted(s.get("notes", []), key=lambda m: -_score_memory(m, terms, qvec))[:top_k]
    lines = []
    if s.get("vibe"):
        lines.append(f"server vibe: {s['vibe']}")
    if s.get("running_jokes"):
        lines.append("running jokes: " + "; ".join(s["running_jokes"][-4:]))
    if notes:
        lines.append("lore that matters here: " + "; ".join(n["text"] for n in notes))
    return "\n".join(lines) if lines else "no server lore yet"
