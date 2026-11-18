"""Tool loop — the bot can LOOK THINGS UP before answering.

Direct mentions run through a multi-turn loop: the model answers with either
a tool call ({tool, args}) or the final {reply, actions}. The harness
executes tools and feeds results back (max hops, then it must answer).

Tools: web_search (TinyFish if keyed, DuckDuckGo otherwise), fetch_page,
channel_history, member_profile, docs_search.
"""

import asyncio
import html
import json
import logging
import os
import re

import discord
import httpx

import ai
from ai import AIError
import memory
import settings

log = logging.getLogger("tools")

TINYFISH_KEY = os.environ.get("TINYFISH_API_KEY", "").strip()
MAX_HOPS = 3

EVIDENCE_RULE = (
    "\n\nEVIDENCE RULE — this one is absolute:\n"
    "- Never claim you checked, looked up, searched, read, or verified something unless a "
    "tool result in THIS conversation actually showed it to you.\n"
    "- \"I think\", \"last I knew\", \"from memory\" are honest and fine. \"I checked\" is not, "
    "if you didn't.\n"
    "- If a lookup failed, say the lookup failed and name what blocked it. Don't paper over "
    "a failed tool call with a confident answer.\n"
    "- Absence of evidence is not evidence: not finding something doesn't prove it's untrue, "
    "outdated, or broken.\n"
    "- Before answering a question about current state (versions, prices, who said what, "
    "what's deployed), prefer one real lookup over a remembered guess."
)

# phrases that imply verification — used only to log when the model asserts
# something it didn't check (no behaviour change, just observability)
_CLAIM_RX = re.compile(
    r"\b(i (?:just |already )?(?:checked|looked|searched|read|verified|confirmed|pulled up)"
    r"|i(?:'ve| have) (?:checked|looked|searched|verified|confirmed)"
    r"|according to (?:the|my) (?:search|lookup|source)"
    r"|i found (?:that |it |the ))\b", re.I)


def claimed_without_evidence(reply: str, tool_calls: int) -> bool:
    """True when a reply asserts verification but no tool ran this turn."""
    return bool(reply) and tool_calls == 0 and bool(_CLAIM_RX.search(reply))


TOOLS_PROMPT = """
TOOLS (you can look things up before answering). Reply ONLY JSON, either:
  {"tool": "<name>", "args": {...}}          ← to look something up first
  {"reply": "<your answer>", "actions": []}  ← your final answer (+optional server actions)

Available tools:
  {"tool": "web_search", "args": {"query": "..."}}          — search the web
  {"tool": "fetch_page", "args": {"url": "https://..."}}    — read a web page
  {"tool": "channel_history", "args": {"channel": "#name", "n": 10}} — recent messages from any channel
  {"tool": "member_profile", "args": {"name": "someone"}}   — everything you know about a member
  {computer_line}
Use them when the answer isn't already in what you know: current events, prices,
anything after your training, or specifics from other channels. Then answer in
your normal voice — cite what you found naturally, don't announce tool use.
"""
COMPUTER_TOOL = '  {"tool": "computer", "args": {"prompt": "..."}}     — delegate a task to your computer: bash, files, AND its live desktop (open a web page in chromium, click/type with xdotool, screenshot) — ADMIN requests only, quick tasks (90s cap)'


# ---------- web search: TinyFish if keyed, DuckDuckGo otherwise ----------

async def web_search(query: str) -> str:
    if TINYFISH_KEY:
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get("https://api.tinyfish.ai/v1/search",
                                params={"query": query},
                                headers={"Authorization": f"Bearer {TINYFISH_KEY}"})
                r.raise_for_status()
                data = r.json()
            results = data.get("results", []) or []
            if results:
                return "\n".join(f"- {r_.get('title', '')}: {r_.get('snippet', '')} ({r_.get('url', '')})"
                                 for r_ in results[:6])
            return "(no results)"
        except Exception as e:
            log.warning("tinyfish search failed (%s) — falling back to ddg", e)

    # keyless fallback: DuckDuckGo html endpoint
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; Inferolli/1.0)"}) as c:
            r = await c.get("https://html.duckduckgo.com/html/", params={"q": query})
            r.raise_for_status()
        body = r.text
        items = re.findall(
            r'class="result__a"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
            body, re.S)
        out = []
        for title, snippet in items[:6]:
            clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            out.append(f"- {clean(title)}: {clean(snippet)[:160]}")
        return "\n".join(out) if out else "(no results)"
    except Exception as e:
        return f"(web search unavailable: {str(e)[:80]})"


async def fetch_page(url: str) -> str:
    if TINYFISH_KEY:
        try:
            async with httpx.AsyncClient(timeout=25.0) as c:
                r = await c.get("https://api.tinyfish.ai/v1/fetch",
                                params={"url": url},
                                headers={"Authorization": f"Bearer {TINYFISH_KEY}"})
                r.raise_for_status()
                text = r.json().get("content") or r.text
                return text[:1500]
        except Exception as e:
            log.warning("tinyfish fetch failed (%s) — direct fetch", e)
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; Inferolli/1.0)"}) as c:
            r = await c.get(url)
            r.raise_for_status()
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:1500] or "(empty page)"
    except Exception as e:
        return f"(couldn't fetch: {str(e)[:80]})"


# ---------- server-side tools ----------

async def channel_history(bot, channel_name: str, n: int = 10) -> str:
    for guild in bot.guilds:
        ch = discord_find_channel(guild, channel_name)
        if ch is None:
            return f"(no channel named {channel_name})"
        lines = []
        async for m in ch.history(limit=max(2, min(n, 25))):
            if not m.author.bot and m.content:
                lines.append(f"{m.author.display_name}: {m.content[:130]}")
        return f"#{ch.name} (recent):\n" + "\n".join(reversed(lines)) if lines else "(quiet in there)"
    return "(no channel found)"


def discord_find_channel(guild, name: str):
    name = name.strip().lstrip("#").lstrip("<").rstrip(">")
    name = name.split("│")[-1].split("|")[-1]
    ch = discord.utils.get(guild.text_channels, name=name)
    if ch is None:
        for c in guild.text_channels:
            if name in c.name:
                return c
    return ch


async def member_profile(bot, name: str) -> str:
    entries = await memory.stm_window(hours=24 * 7)
    theirs = [e for e in entries if name.lower() in e["name"].lower()]
    if not theirs:
        return f"(nothing in the archive from anyone called {name})"
    uid = theirs[-1]["user_id"]
    mem = await memory.get_user(uid, theirs[-1]["name"])
    server = await memory.get_server()
    from cogs.companion import _memory_block
    recent = "\n".join(f"- #{e['channel']}: {e['text'][:110]}" for e in theirs[-6:])
    return (f"what you know about {theirs[-1]['name']}:\n"
            f"{_memory_block(mem, server, [])}\n\ntheir recent messages:\n{recent}")


async def docs_search(query: str) -> str:
    try:
        import rag
        results = rag.search(query)
        if not results:
            return "(no doc matches — if it's not in the docs, say so plainly)"
        return "\n".join(f"- {r.get('title', '?')}: {r.get('text', '')[:150]}" for r in results[:4])
    except Exception as e:
        return f"(docs unavailable: {str(e)[:80]})"


# ---------- the loop ----------

async def run_tool(bot, name: str, args: dict) -> str:
    name = (name or "").strip()
    try:
        if name == "web_search":
            return await web_search(str(args.get("query", ""))[:300])
        if name == "fetch_page":
            return await fetch_page(str(args.get("url", ""))[:500])
        if name == "channel_history":
            return await channel_history(bot, str(args.get("channel", "")), int(args.get("n", 10) or 10))
        if name == "member_profile":
            return await member_profile(bot, str(args.get("name", ""))[:60])
        if name == "docs_search":
            return await docs_search(str(args.get("query", ""))[:300])
        if name == "computer":
            if not settings.get_bool("computer.tool_enabled", True,
                                    env="COMPUTER_TOOL_ENABLED"):
                return "(computer access disabled)"
            prompt = str(args.get("prompt", ""))[:500]
            import subprocess
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: subprocess.run(
                    f"cd /workspace && timeout 90 opencode run '{prompt.replace(chr(39), chr(39) * 6)}'",
                    shell=True, capture_output=True, text=True, timeout=110))
            return ((r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr.strip() else "")
                    ).strip()[:1200] or "(no output)"
        return f"(unknown tool: {name})"
    except Exception as e:
        return f"(tool {name} failed: {str(e)[:80]})"


async def run_loop(bot, base_system: str, user_prompt, *,
                   max_tokens: int = 400, timeout: float = 30.0,
                   allow_actions: bool = True, content_payload=None,
                   admin: bool = False) -> dict:
    """The multi-turn loop. Returns the final {"reply": ..., "actions": [...]} (or {}).

    user_prompt: a string, or (for vision) an OpenAI content-part list —
    the images ride on the first user message.
    admin: enables the computer tool (opencode delegation) for this loop.
    """
    sys_prompt = base_system + TOOLS_PROMPT.replace(
        "{computer_line}", COMPUTER_TOOL if admin else "") + EVIDENCE_RULE
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt if content_payload is None
                 else content_payload}]
    tool_calls = 0
    for hop in range(MAX_HOPS):
        try:
            data = await ai.chat_for("reply", messages, json_out=True,
                                     max_tokens=max_tokens, timeout=timeout)
        except AIError:
            raise
        if "tool" in data and hop < MAX_HOPS - 1:
            tool_calls += 1
            result = await run_tool(bot, data.get("tool"), data.get("args") or {})
            messages.append({"role": "assistant", "content": json.dumps(data)})
            messages.append({"role": "user", "content": f"tool result:\n{result[:1200]}\n\n"
                             "(continue — reply or call another tool)"})
            continue
        # final answer
        reply = str(data.get("reply", "")).strip()
        actions = data.get("actions") or [] if allow_actions else []
        if claimed_without_evidence(reply, tool_calls):
            log.warning("reply claims verification without a tool call: %r", reply[:90])
        return {"reply": reply, "actions": actions}
    return {"reply": "", "actions": []}
