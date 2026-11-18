"""The desktop gate — Discord-authenticated access to the sandbox desktop.

Flow:
  /desktop_link (in Discord) → the bot issues a signed token via the
  interaction (running the command IS the Discord-membership proof).
  The token carries {uid, rw, exp} signed with HMAC(secret = the E2B key).

  Opening the link with the token → the gate sets a cookie and serves the
  noVNC viewer. The /websockify websocket is proxied to:
     x11vnc :5900 (read-write)  ← admins + staff
     x11vnc :5901 (read-only)   ← everyone else
  No valid token/cookie → nothing at all.

Run: uvicorn desktop_gate:app --host 127.0.0.1 --port 8080
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("desktop-gate")

SECRET = hashlib.sha256((os.environ.get("E2B_API_KEY", "dev") + "desktop-gate").encode()).digest()
TOKEN_TTL = 24 * 3600
NOVNC = "/usr/share/novnc"
RO_PORT, RW_PORT = 5901, 5900

app = FastAPI()

# noVNC's vnc.html pulls its UI from /app/..., /core/..., /vendor/... — without
# these mounts the viewer loads as a blank page (all assets 404)
for _sub in ("app", "core", "vendor"):
    if Path(NOVNC, _sub).is_dir():
        app.mount(f"/{_sub}", StaticFiles(directory=str(Path(NOVNC, _sub))), name=_sub)


def _sign(payload: bytes) -> str:
    return hmac.new(SECRET, payload, hashlib.sha256).hexdigest()[:32]


def make_token(uid: int, rw: bool) -> str:
    body = json.dumps({"uid": uid, "rw": rw, "exp": time.time() + TOKEN_TTL}).encode()
    return body.hex() + "." + _sign(body)


def read_token(token: str) -> dict | None:
    try:
        body_hex, sig = token.split(".", 1)
        body = bytes.fromhex(body_hex)
        if not hmac.compare_digest(_sign(body), sig):
            return None
        data = json.loads(body)
        if data["exp"] < time.time():
            return None
        return data
    except Exception:
        return None


def _cookie_class(request: Request) -> dict | None:
    tok = request.cookies.get("dgtok")
    return read_token(tok) if tok else None


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def index(request: Request, token: str = ""):
    data = _cookie_class(request) or (read_token(token) if token else None)
    if not data:
        return PlainTextResponse(
            "🔒 this desktop is Discord-gated.\n\nrun /desktop_link in the Discord server — "
            "the link it gives you carries your access.\n"
            "(members watch · admins+staff can interact)", status_code=401)
    resp = RedirectResponse("/vnc.html?path=websockify&autoconnect=true&resize=scale", 302)
    resp.set_cookie("dgtok", token or request.cookies.get("dgtok", ""),
                    max_age=TOKEN_TTL, httponly=True)
    return resp


# the noVNC static viewer
@app.get("/vnc.html")
async def vnc_html():
    return HTMLResponse((Path(NOVNC, "vnc.html").read_text()).replace(
        "</body>",
        "<script>document.title='inferolli desktop'</script></body>"))


@app.get("/websockify")
async def ws_http_guard():
    return PlainTextResponse("websocket only", status_code=426)


@app.websocket("/websockify")
async def ws_proxy(websocket: WebSocket):
    data = _cookie_class(websocket) or (
        read_token(websocket.query_params.get("token", ""))
        if websocket.query_params.get("token") else None)
    if not data:
        await websocket.close(code=4401)
        return
    upstream_port = RW_PORT if data.get("rw") else RO_PORT

    await websocket.accept()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", upstream_port)
    except Exception:
        await websocket.close(code=1011)
        return

    async def to_vnc():
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data_bytes = msg.get("bytes")
                if data_bytes is None and "text" in msg:
                    data_bytes = msg["text"].encode()
                if data_bytes:
                    writer.write(data_bytes)
                    await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def to_ws():
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                await websocket.send_bytes(chunk)
        except Exception:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    await asyncio.gather(to_vnc(), to_ws())
