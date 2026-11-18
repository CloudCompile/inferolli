"""The computer — Inferolli's hands: opencode (headless agent), shell, chrome.

ADMIN-ONLY in v1: this is real power (file system, package installs, code
execution) inside its own E2B sandbox — disposable, backed-up, contained.
Members can watch the outputs; the keys stay with the keys-holders.

  /oc <prompt>    — delegate a task to opencode (bash, files, git, installs)
  /sh <command>   — run a shell command in the sandbox
  /screen <url>   — headless chrome screenshot of a page
"""

import asyncio
import json
import logging
import os
import tempfile
import time

import discord
from discord import app_commands
from discord.ext import commands

import permissions
import settings
from helpers import is_admin

log = logging.getLogger("cogs.computer")

WORKSPACE = "/workspace"
OC_TIMEOUT = settings.get_int("opencode.timeout_seconds", 200, env="OC_TIMEOUT_S")

# ---------- opencode server client: persistent sessions, one thread per channel ----------

import httpx as _httpx

OC_URL = "http://localhost:4096"
OC_PORT = settings.get_int("opencode.port", 4096, env="OPENCODE_PORT")
OC_AUTH = ("opencode", os.environ.get("OPENCODE_SERVER_PASSWORD", "inferolli-local-4096"))


def _server_password(path: str = "/tmp/opencode-serve.log") -> str | None:
    """v2 prints a generated server password to the serve log — read it."""
    try:
        with open(path) as f:
            for line in f:
                if "server password" in line:
                    return line.split("server password")[1].strip().split()[0]
    except Exception:
        pass
    return None


def _extract_reply(messages: list, user_msg_id: str | None) -> str | None:
    """Pull the assistant text that came AFTER our prompt from a session's
    message list. The API returns NEWEST-FIRST — reversing is the whole trick:
    without it every reply came back one turn late (the LANTERN bug).

    The LAST assistant text is the real answer: agents emit intermediate
    narration ("websearch is flaky, falling back to curl") mid-turn, and the
    first matching text used to be returned as the reply."""
    texts: list[str] = []
    after = False
    for m in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(m, dict):
            continue
        if user_msg_id and m.get("id") == user_msg_id:
            after = True
            continue
        if after and m.get("type") == "assistant":
            turn: list[str] = []
            for part in m.get("content", []) or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    turn.append(part["text"])
            if turn:
                texts = turn          # keep only the newest assistant text
    return "\n".join(texts).strip() or None


def _turn_finished(messages: list, user_msg_id: str | None) -> bool:
    """True once the agent's turn is over (an `idle` marker newer than our
    prompt). Without this we can grab a half-written answer mid-research."""
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "idle":
            return True
        if user_msg_id and m.get("id") == user_msg_id:
            return False
    return False



def _auth():
    """Basic-auth tuple: env password → the generated one from the log → the fixed default."""
    pw = (os.environ.get("OPENCODE_SERVER_PASSWORD")
          or _server_password()
          or "inferolli-local-4096")
    return ("opencode", pw)


async def oc_alive() -> bool:
    try:
        auth = _auth()
        async with _httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"http://localhost:{OC_PORT}/api/info", auth=auth)
            return r.status_code == 200
    except Exception:
        return False


_session_ids: dict[int, str] = {}   # channel_id -> opencode session id


async def oc_session(channel_id: int, title: str) -> str | None:
    """Get (or lazily create) the persistent opencode session for a channel."""
    if channel_id in _session_ids:
        return _session_ids[channel_id]
    # recover from disk if we restarted (sessions are keyed by channel in a state file)
    state_file = "/bot/data/oc-sessions.json"
    try:
        with open(state_file) as f:
            state = json.load(f)
        if str(channel_id) in state:
            _session_ids[channel_id] = state[str(channel_id)]
            return state[str(channel_id)]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    async with _httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(f"http://localhost:{OC_PORT}/api/session", json={"title": title[:80]}, auth=_auth())
        if r.status_code in (200, 201):
            sid = (r.json().get("data") or {}).get("id")
            _session_ids[channel_id] = sid
            try:
                with open(state_file) as f:
                    state = json.load(f)
            except Exception:
                state = {}
            state[str(channel_id)] = sid
            try:
                with open(state_file, "w") as f:
                    json.dump(state, f, indent=1)
            except Exception:
                pass
            return sid
    return None


async def oc_ask(session_id: str, prompt: str, system: str | None = None,
                 timeout: float = 150.0) -> str | None:
    """Send a prompt into a persistent opencode session, then poll for the
    assistant's reply. v2's prompt endpoint is async — the agent works in the
    background and the reply lands in the session's message list."""
    async with _httpx.AsyncClient(timeout=30.0, auth=_auth()) as c:
        r = await c.post(f"http://localhost:{OC_PORT}/api/session/{session_id}/prompt",
                         json={"text": prompt[:6000]})
        if r.status_code != 200:
            log.warning("opencode prompt failed: %s %s", r.status_code, r.text[:120])
            return None
        try:
            user_msg_id = r.json()["data"]["id"]
        except Exception:
            user_msg_id = None

        deadline = time.time() + timeout
        msgs: list = []
        while time.time() < deadline:
            await asyncio.sleep(3)
            r = await c.get(f"http://localhost:{OC_PORT}/api/session/{session_id}/message",
                            auth=_auth())
            if r.status_code != 200:
                continue
            msgs = (r.json().get("data") or [])
            reply = _extract_reply(msgs, user_msg_id)
            # only trust it once the agent has gone idle, otherwise a mid-turn
            # progress line ("websearch is flaky, falling back to curl") ships
            # as the answer
            if reply and _turn_finished(msgs, user_msg_id):
                return reply
        log.warning("opencode reply timed out after %.0fs", timeout)
        if msgs and isinstance(msgs, list):
            late = _extract_reply(msgs, user_msg_id)
            if late:
                log.warning("opencode turn still running — using partial reply")
                return late
        return None


def _run_shell(cmd: str, timeout: int, cwd: str | None = None) -> str:
    import subprocess
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr.strip() else "")
        return (out.strip() or "(no output)") + f"\n[exit {r.returncode}]"
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)"
    except Exception as e:
        return f"(error: {e})"


_run = _run_shell  # legacy alias


class ComputerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="oc", description="Delegate a task to opencode (the bot's computer) — admin")
    @app_commands.describe(prompt="what to do — it has bash, files, git, package installs in /workspace")
    async def oc(self, interaction: discord.Interaction, prompt: str):
        if not is_admin(interaction.user):
            return await interaction.response.send_message(
                "the computer is admin-only for now.", ephemeral=True)
        await interaction.response.defer()
        started = time.time()
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _run(f"opencode run '{prompt.replace(chr(39), chr(39)*6)}'",
                               OC_TIMEOUT, WORKSPACE))
        took = time.time() - started
        body = f"🖥️ **opencode** ({took:.0f}s)\n```\n{result[:1800]}\n```"
        if len(result) > 1800:
            body += f"\n*…{len(result)} chars total — ask me to show more*"
        await interaction.followup.send(body)

    @app_commands.command(name="sh", description="Run a shell command in the bot's sandbox — admin")
    async def sh(self, interaction: discord.Interaction, command: str):
        if not is_admin(interaction.user):
            return await interaction.response.send_message(
                "the computer is admin-only for now.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _run(command, 120, WORKSPACE))
        await interaction.followup.send(f"```\n{result[:1900]}\n```", ephemeral=True)

    @app_commands.command(name="screen", description="Screenshot a web page (headless chrome) — admin")
    async def screen(self, interaction: discord.Interaction, url: str):
        if not is_admin(interaction.user):
            return await interaction.response.send_message(
                "the computer is admin-only for now.", ephemeral=True)
        await interaction.response.defer()
        if not url.startswith("http"):
            url = "https://" + url
        result = await asyncio.get_event_loop().run_in_executor(None, lambda: _run(
            # any chromium-family browser (the image ships chromium; chrome if present)
            f"B=$(command -v google-chrome || command -v chromium || command -v chromium-browser); "
            f"$B --headless=new --no-sandbox --disable-gpu --window-size=1280,1400 "
            f"--screenshot=/workspace/shot.png --virtual-time-budget=8000 '{url}' 2>&1 | tail -1",
            60, WORKSPACE))
        try:
            await interaction.followup.send(
                f"🖥️ {url[:80]}",
                file=discord.File("/workspace/shot.png", filename="screenshot.png"))
        except FileNotFoundError:
            await interaction.followup.send(f"screenshot failed: {result[:200]}")

    @app_commands.command(name="desktop_link", description="Get your personal link to the live desktop — Discord-authed")
    async def desktop_link(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        import subprocess
        async def run(cmd, timeout: int = 30):
            # to_thread: every call here is a subprocess — never block the loop
            return await asyncio.to_thread(
                lambda: subprocess.run(cmd, shell=True, capture_output=True,
                                       text=True, timeout=timeout))
        # gate + tunnel alive?
        r = await run("pgrep -f 'uvicorn desktop_gate' >/dev/null && echo up || echo down")
        if "down" in r.stdout:
            await run(
                "export DISPLAY=:99; (x11vnc -display :99 -forever -shared -nopw -quiet "
                "-viewonly -rfbport 5901 > /tmp/x11vnc-ro.log 2>&1 &); "
                "(x11vnc -display :99 -forever -shared -nopw -quiet -rfbport 5900 "
                "> /tmp/x11vnc-rw.log 2>&1 &); "
                "(nohup python3 -m uvicorn desktop_gate:app --host 127.0.0.1 --port 8080 "
                "> /tmp/gate.log 2>&1 &); "
                "(sleep 2; nohup cloudflared tunnel --url http://localhost:8080 --no-autoupdate "
                "> /tmp/cloudflared.log 2>&1 &)", timeout=40)
            await asyncio.sleep(8)

        url = ""
        for _ in range(6):
            # tail, not head: the log can hold an older (dead) URL above the live one
            r = await run("grep -oE 'https://[a-z0-9-]+\\.trycloudflare\\.com' /tmp/cloudflared.log | tail -1")
            url = r.stdout.strip()
            if url:
                # prove the tunnel actually reaches the gate before handing it out
                h = await run(f"curl -s --max-time 8 {url}/health")
                if "ok" in h.stdout:
                    break
                url = ""
            await asyncio.sleep(3)
        if not url:
            return await interaction.followup.send(
                f"tunnel didn't come up: {(r.stderr or r.stdout)[:150]}")

        # signed tokens: anyone at staff tier or above gets touch, everyone else
        # watch-only (role ids live in config.json, not in code)
        import desktop_gate
        uid = interaction.user.id
        rw = permissions.is_staff(interaction.user)
        touch = f"{url}/?token={desktop_gate.make_token(uid, rw=True)}"
        watch = f"{url}/?token={desktop_gate.make_token(uid, rw=False)}"

        await interaction.followup.send(
            f"🖥️ **your links** (valid 24h):\n"
            + ("✋ **TOUCH** (you're admin/staff — you can click and type):\n" if rw else "")
            + (touch + "\n" if rw else "")
            + f"👀 **WATCH-ONLY** (share this one): {watch}\n"
            + "the view is my actual computer — XFCE desktop, chrome, everything.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ComputerCog(bot))
