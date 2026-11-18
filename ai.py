"""Minimal OpenAI-compatible client for the InferencePort P2G API.

Chat:       POST /v1/chat/completions   (model: gpt-6-luna)
Embeddings: POST /v1/embeddings
Decisions:  POST /v1/systemone          (microsoft/decision-1)
"""

import asyncio
import logging
import os

import httpx

log = logging.getLogger("ai")

API_KEY = os.environ.get("INFERENCEPORT_API_KEY", "")
BASE_URL = os.environ.get("AI_BASE_URL", "https://api.inferenceport.ai/v1").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "gpt-6-luna")
FALLBACK_MODELS = [m.strip() for m in os.environ.get("AI_FALLBACK_MODELS", "lightning").split(",") if m.strip()]

# --- providers: chat can run on a second gateway (e.g. Vercel AI Gateway),
#     while decision + embeddings stay on InferencePort ---
GW_BASE = os.environ.get("AI_GATEWAY_BASE_URL", "").rstrip("/")
GW_KEY = os.environ.get("AI_GATEWAY_KEY", "")
GW_MODEL = os.environ.get("AI_GATEWAY_MODEL", "").strip()


def _provider_for(model: str) -> tuple[str, str, str]:
    """(provider, base_url, key) for a model id. 'provider/model' ids route to the gateway."""
    if "/" in model and GW_BASE and GW_KEY:
        return "gateway", GW_BASE, GW_KEY
    return "port", BASE_URL, API_KEY


def _job_model(job: str) -> str | None:
    """Mixture-of-models: per-job model override (AI_JOB_<JOB> in env)."""
    return os.environ.get(f"AI_JOB_{job.upper().replace('-', '_')}", "").strip() or None


async def chat_for(job: str, messages: list[dict], *, json_out: bool = False, **kwargs) -> str:
    """Route a named job to its model (falls back to the default chain).

    Jobs: reply, thoughts, reflect, dream, dreamtext, soul, rag, quick, automod.
    Set e.g. AI_JOB_DREAM=gemini-4-hydrogen to upgrade a job's model.
    json_out=True returns a parsed dict (tolerating ``` fences), raising AIError
    if the model didn't return a JSON object.
    """
    raw = await chat(messages, model=_job_model(job), **kwargs)
    if not json_out:
        return raw
    import json as _json
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = _json.loads(text)
    except _json.JSONDecodeError as e:
        raise AIError(f"AI did not return valid JSON: {raw[:200]}") from e
    if not isinstance(obj, dict):
        raise AIError("AI returned JSON that is not an object")
    return obj
EMBED_MODEL = os.environ.get("AI_EMBED_MODEL", "text-embedding-3-small")
DECISION_MODEL = os.environ.get("AI_DECISION_MODEL", "microsoft/decision-1")


class AIError(Exception):
    pass


async def _post(path: str, payload: dict, *, timeout: float, retries: int = 2,
                provider: str | None = None) -> dict:
    """POST to the configured provider with retries on transient 5xx (cold backends 502).

    provider: force "port" (decision/embeddings MUST stay here — the gateway has
    no /systemone or /embeddings) or None (auto-route by model id: ids with a
    slash like stealth/glyph-cluster go to the gateway, plain ids to port).
    """
    model = payload.get("model", "")
    if provider:
        prov, base_url, key = provider, (GW_BASE if provider == "gateway" else BASE_URL), \
            (GW_KEY if provider == "gateway" else API_KEY)
    else:
        prov, base_url, key = _provider_for(model)
    if not key:
        raise AIError("no API key configured for provider")
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await client.post(f"{base_url}{path}", json=payload, headers=headers)
                if resp.is_error and resp.status_code >= 500 and attempt < retries:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if resp.status_code in (402, 403):
                    raise AIError(f"gateway needs payment setup: {resp.text[:80]}")
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                raise AIError(f"AI API returned {e.response.status_code}: {e.response.text[:200]}") from e
            except httpx.HTTPError as e:
                last_err = e
                if attempt < retries:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise AIError(f"AI API unreachable: {e}") from e
    raise AIError(f"AI API unreachable: {last_err}")


async def chat(messages: list[dict], *, model: str | None = None, max_tokens: int = 500,
               temperature: float = 0.7, timeout: float = 30.0) -> str:
    """One chat completion with automatic model fallback on upstream 5xx.

    Reasoning models (e.g. the lightning router) can return empty content when
    max_tokens is too small for the thinking phase — retry the chain with a
    bumped budget before giving up.
    """
    # chain of (provider-distinguishing) model ids: gateway primary (if set),
    # then the requested/default model, then the fallbacks
    chain: list[str] = []
    if GW_MODEL and GW_KEY and GW_BASE and model in (None, GW_MODEL):
        chain.append(GW_MODEL)                       # gateway primary
    chain.append(model or MODEL)
    chain += [m for m in FALLBACK_MODELS if m not in chain and m != (model or MODEL)]
    last_err: AIError | None = None

    for budget_mult in (1, 8):
        budget = max(max_tokens, 2000) * budget_mult if budget_mult > 1 else max_tokens
        for m in chain:
            payload = {
                "model": m,
                "messages": messages,
                "max_tokens": budget,
                "temperature": temperature,
            }
            try:
                data = await _post("/chat/completions", payload, timeout=timeout)
            except AIError as e:
                last_err = e
                if "gateway needs payment setup" in str(e):
                    log.info("gateway model %s not unlocked yet — using port chain", m)
                    continue
                if "returned 5" in str(e):  # upstream down — try the next model in the chain
                    log.warning("model %s unavailable (%s), falling back", m, str(e)[:80])
                    continue
                raise
            try:
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
            except (KeyError, IndexError, TypeError) as e:
                raise AIError(f"Unexpected AI API response shape: {data}") from e

            if content:
                return content
            if not msg.get("reasoning"):
                return ""  # genuinely empty reply
            log.debug("model %s returned empty content (reasoning ate the budget), retrying", m)

    # last resort: return the reasoning text rather than nothing
    raise last_err or AIError("all models returned empty content")


async def embed(texts: list[str], *, timeout: float = 60.0) -> list[list[float]]:
    """Batch embeddings via POST /v1/embeddings (OpenAI-compatible)."""
    out: list[list[float]] = []
    for i in range(0, len(texts), 16):  # stay well under the batch cap
        data = await _post(
            "/embeddings",
            {"model": EMBED_MODEL, "input": texts[i:i + 16]},
            timeout=timeout, provider="port",
        )
        out.extend(item["embedding"] for item in data["data"])
    return out


async def decide(state: str, questions: dict, *, timeout: float = 30.0) -> dict:
    """POST /v1/systemone — typed questions with probabilities.

    Returns the provider `answers` object, e.g.
    {"support": {"choice": "yes", "confidence": 0.93, ...}}. Raises AIError on failure.
    """
    data = await _post(
        "/systemone",
        {"model": DECISION_MODEL, "state": state, "questions": questions},
        timeout=timeout, provider="port",
    )
    try:
        return data["answers"]
    except (KeyError, TypeError) as e:
        raise AIError(f"Unexpected systemone response shape: {data}") from e


async def chat_json(messages: list[dict], **kwargs) -> dict:
    """Chat completion parsed as a JSON object (tolerates ``` fences)."""
    import json as _json

    raw = await chat(messages, **kwargs)
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = _json.loads(text)
    except _json.JSONDecodeError as e:
        raise AIError(f"AI did not return valid JSON: {raw[:200]}") from e
    if not isinstance(obj, dict):
        raise AIError("AI returned JSON that is not an object")
    return obj
