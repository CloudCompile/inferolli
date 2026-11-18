"""RAG over the InferencePort docs (docs.inferenceport.ai).

Builds an embedding index of the docs pages (cached in data/docs-index.json),
answers support questions grounded in retrieved chunks, and uses the
microsoft/decision-1 decision model as a cheap gate to decide whether a
message needs docs at all.
"""

import asyncio
import json
import logging
import math
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx

import ai
from ai import AIError

log = logging.getLogger("rag")

DOCS_BASE = "https://docs.inferenceport.ai/en/latest/"
DOCS_INDEX_FILE = Path(__file__).parent / "data" / "docs-index.json"
MAX_AGE_S = 7 * 24 * 3600  # rebuild weekly

PAGES = [
    "overview.html", "getting-started.html", "download-guide.html",
    "user-guide.html", "troubleshooting.html", "integrations.html",
    "cli/index.html", "cli/install.html", "cli/quickstart.html",
    "cli/authentication.html", "cli/configuration.html", "cli/lightning.html",
    "cli/p2g.html", "cli/featherless.html", "cli/agents.html", "cli/buckets.html",
    "cli/mcp-council.html", "cli/shield.html", "cli/organizations.html",
    "cli/local-models.html", "cli/server.html", "cli/imports.html", "cli/usage.html",
    "cli/scripting.html", "cli/reference.html", "cli/troubleshooting.html",
    "api/index.html", "api/gen-api.html", "api/video-async-api.html",
    "api/p2g-api.html", "api/embeddings-api.html", "api/decision-api.html",
    "api/realtime-api.html", "api/shield.html", "api/rewards-system.html",
    "api/buckets.html",
    "developer/index.html", "developer/tools-workflow.html",
    "developer/gguf-import.html", "developer/huggingface-import.html",
]

CHUNK_CHARS = 1500


class _TextExtractor(HTMLParser):
    """Crude HTML -> text, keeping headings as structure hints."""
    _SKIP = {"script", "style", "nav", "footer"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag in ("h1", "h2", "h3"):
            self.parts.append("\n## ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag in ("p", "li", "pre", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> tuple[str, str]:
    """Returns (title, text)."""
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    title = m.group(1).strip() if m else ""
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    return title, re.sub(r"\n{3,}", "\n\n", text).strip()


def _endpoint_overview_chunk(doc: dict) -> str | None:
    """Compact 'every endpoint on this page' chunk, built from HTTP-method/path
    patterns in the text. Fixes vague queries like 'what endpoints do we support'
    where the real list is scattered across a huge page."""
    paths = re.findall(r"\b(GET|POST|PUT|DELETE|PATCH|WS)\s+(/[^\s`\"',)]+)", doc["text"])
    if not paths:
        return None
    seen, lines = set(), []
    for method, path in paths:
        key = f"{method} {path.rstrip('.,;')}"
        if key not in seen:
            seen.add(key)
            lines.append(f"- {key}")
    if len(lines) < 2:
        return None
    return (f"[{doc['title']} — complete endpoint list]\n"
            "This page documents these endpoints:\n" + "\n".join(lines))


def _chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    chunks = []
    for para_block in text.split("\n\n"):
        if not para_block.strip():
            continue
        if chunks and len(chunks[-1]) + len(para_block) < size:
            chunks[-1] += "\n" + para_block
        else:
            chunks.append(para_block)
    return [c[:size] for c in chunks if len(c.strip()) > 80]


async def _fetch_docs() -> list[dict]:
    """Download all doc pages; returns [{url, text}]."""
    docs = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for path in PAGES:
            url = urljoin(DOCS_BASE, path)
            try:
                r = await client.get(url)
                r.raise_for_status()
                title, text = _html_to_text(r.text)
                docs.append({"url": url, "title": title, "text": text})
            except httpx.HTTPError as e:
                log.warning("docs fetch failed for %s: %s", url, e)
    return docs


async def build_index(force: bool = False) -> int:
    """Build (or refresh) the docs index. Returns chunk count."""
    DOCS_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not force and DOCS_INDEX_FILE.exists():
        age = time.time() - DOCS_INDEX_FILE.stat().st_mtime
        if age < MAX_AGE_S:
            return len(json.loads(DOCS_INDEX_FILE.read_text())["chunks"])

    log.info("building docs index from %d pages...", len(PAGES))
    docs = await _fetch_docs()
    chunks = []
    for doc in docs:
        overview = _endpoint_overview_chunk(doc)
        if overview:
            chunks.append({"url": doc["url"], "title": doc["title"], "text": overview})
        for chunk in _chunk(doc["text"]):
            chunks.append({
                "url": doc["url"],
                "title": doc["title"],
                # page title in every chunk gives vague queries something to match on
                "text": f"[{doc['title']}] {chunk}",
            })
    if not chunks:
        raise AIError("no docs content fetched — index not built")

    embeddings = await ai.embed([c["text"][:6000] for c in chunks])
    for chunk, emb in zip(chunks, embeddings):
        chunk["embedding"] = emb

    tmp = DOCS_INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"model": ai.EMBED_MODEL, "chunks": chunks}))
    tmp.replace(DOCS_INDEX_FILE)
    log.info("docs index built: %d chunks from %d pages", len(chunks), len(docs))
    return len(chunks)


async def _load_index() -> list[dict]:
    await build_index()  # no-op if cached and fresh
    data = json.loads(DOCS_INDEX_FILE.read_text())
    return data["chunks"]


_STOP = {"the", "a", "an", "is", "are", "do", "does", "what", "how", "i", "we",
         "of", "in", "on", "for", "to", "and", "or", "it", "this", "that", "with"}


async def search(query: str, k: int = 6) -> list[dict]:
    """Top-k chunks for the query: [{url, title, text, score}].

    Hybrid scoring: cosine similarity + a keyword-overlap boost, because vague
    queries ("what endpoints do we support") need title/keyword hits too.
    Max 2 chunks per page so answers see broad coverage.
    """
    chunks = await _load_index()
    [q] = await ai.embed([query[:6000]])
    qn = math.sqrt(sum(a * a for a in q)) or 1.0

    q_terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in _STOP and len(t) > 2]

    scored = []
    for c in chunks:
        dot = sum(a * b for a, b in zip(q, c["embedding"]))
        cn = math.sqrt(sum(b * b for b in c["embedding"])) or 1.0
        cosine = dot / (qn * cn)

        text_l = c["text"].lower()
        hits = sum(1 for t in q_terms if t in text_l)
        overlap = hits / len(q_terms) if q_terms else 0.0

        scored.append({"url": c["url"], "title": c.get("title", ""), "text": c["text"],
                       "score": cosine + 0.25 * overlap})

    scored.sort(key=lambda x: x["score"], reverse=True)

    picked, per_page = [], {}
    for c in scored:
        if per_page.get(c["url"], 0) >= 2:
            continue
        per_page[c["url"]] = per_page.get(c["url"], 0) + 1
        picked.append(c)
        if len(picked) >= k:
            break
    return picked


_SUPPORT_DECISION = {
    "support": {
        "type": "choice",
        "instructions": "Is this message asking for help with the InferencePort AI platform "
                        "(its API, models, pricing, console, CLI, desktop app, embeddings, "
                        "decision models, deployments, or troubleshooting)?",
        "criteria": {
            "yes": "A question or problem about the InferencePort AI platform itself",
            "no": "General chat, another product, or something unrelated",
        },
    },
    "technical": {
        "type": "choice",
        "instructions": "Is this a technical/factual question (APIs, endpoints, code, "
                        "tools, integrations) about any platform — even one that is not InferencePort?",
        "criteria": {
            "yes": "Wants a technically accurate answer (endpoints, code, config, how something works)",
            "no": "Small talk, opinions, social messages — nothing needing factual accuracy",
        },
    },
}


async def classify(text: str) -> str:
    """Decision-model gate. Returns one of:
    'docs'   — technical question or InferencePort question -> try the docs index;
               grounded_answer() says so if the docs don't cover it
    'casual' — normal conversation -> persona reply
    """
    try:
        answers = await ai.decide(text[:2000], _SUPPORT_DECISION, timeout=15.0)

        def _says(name: str) -> bool:
            a = answers.get(name, {})
            return a.get("choice") == "yes" and float(a.get("confidence", 0)) >= 0.7

        return "docs" if (_says("support") or _says("technical")) else "casual"
    except (AIError, ValueError, KeyError) as e:
        log.debug("decision gate failed-open: %s", e)
        return "casual"


_SUPPORT_SYSTEM = """You are the InferencePort AI support assistant in a Discord server.
Answer using ONLY the documentation excerpts provided. Be concise and practical
(2-6 sentences or a short list). Cite the doc page(s) you used as bare URLs at the end.
If the excerpts don't cover the question, say so plainly and clearly — e.g. "the
InferencePort docs don't cover that" — rather than guessing from outside knowledge."""


async def grounded_answer(question: str) -> str:
    """Docs-grounded answer with citations. Raises AIError on failure."""
    try:
        await build_index()
        top = await search(question)
    except AIError as e:
        raise AIError(f"retrieval failed: {e}") from e

    if not top or top[0]["score"] < 0.35:  # nothing relevant in the docs
        return ("I checked the InferencePort docs and they don't cover that — "
                "so I can't answer it accurately. They're at https://docs.inferenceport.ai.")

    context = "\n\n---\n\n".join(
        f"[source: {c['url']}]\n{c['text'][:1200]}" for c in top
    )
    return await ai.chat_for("rag", 
        [
            {"role": "system", "content": _SUPPORT_SYSTEM},
            {"role": "user", "content": f"Docs excerpts:\n\n{context}\n\nQuestion: {question[:1500]}"},
        ],
        max_tokens=700,
        timeout=60.0,
    )
