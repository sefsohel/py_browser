"""PyBrowser: a Chromium-based remote browser written in Python 3.11.

Chromium runs headless on the server (via Playwright). Its screen is streamed to
the web page as JPEG frames over a WebSocket (CDP screencast), and mouse /
keyboard input from the page is replayed into Chromium.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import Browser, Page, async_playwright

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("pybrowser")

MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "2"))
HOME_URL = os.getenv("HOME_URL", "https://duckduckgo.com")
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "60"))
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "900"))  # seconds
MAX_W, MAX_H = 1920, 1080

# --- Login (all values have defaults; override with Railway variables) ---
AUTH_USER = os.getenv("BROWSER_USER", "admin")
AUTH_PASSWORD = os.getenv("BROWSER_PASSWORD", "pybrowser")
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)  # random per start if unset
SESSION_TTL = int(os.getenv("SESSION_TTL", "86400"))  # seconds (default 24 h)
COOKIE = "pb_session"

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--mute-audio",
]


# --------------------------------------------------------------------------- #
# App lifecycle
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    pw = await async_playwright().start()
    app.state.browser = await pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
    app.state.active = 0
    log.info("Chromium %s ready", app.state.browser.version)
    if AUTH_PASSWORD == "pybrowser":
        log.warning("Using the DEFAULT password. Set BROWSER_PASSWORD in Railway variables!")
    try:
        yield
    finally:
        await app.state.browser.close()
        await pw.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_url(raw: str) -> str | None:
    """Turn address-bar input into a safe URL (http/https only) or a search."""
    raw = raw.strip()
    if not raw:
        return None
    if raw == "about:blank":
        return raw
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        return raw if raw.lower().startswith(("http://", "https://")) else None
    if " " in raw or ("." not in raw and "localhost" not in raw):
        return "https://duckduckgo.com/?q=" + quote_plus(raw)
    return "https://" + raw


def clamp(value: str | None, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(float(value))))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


BUTTONS = {0: "left", 1: "middle", 2: "right"}


class Session:
    """One browser context + page, streamed to one WebSocket client."""

    def __init__(self, ws: WebSocket, browser: Browser, w: int, h: int):
        self.ws, self.browser, self.w, self.h = ws, browser, w, h
        self.ctx = None
        self.page: Page | None = None
        self.cdp = None
        self._lock = asyncio.Lock()

    # -- outgoing ---------------------------------------------------------- #
    async def send_json(self, obj: dict) -> None:
        async with self._lock:
            try:
                await self.ws.send_text(json.dumps(obj))
            except Exception:
                pass

    async def _on_frame(self, params: dict) -> None:
        try:
            async with self._lock:
                await self.ws.send_bytes(base64.b64decode(params["data"]))
            # Ack only after sending: natural back-pressure for slow clients.
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass

    async def _nav_changed(self, loading: bool = False) -> None:
        try:
            title = await self.page.title()
        except Exception:
            title = ""
        await self.send_json({"type": "nav", "url": self.page.url, "title": title, "loading": loading})

    # -- lifecycle --------------------------------------------------------- #
    async def start(self) -> None:
        self.ctx = await self.browser.new_context(
            viewport={"width": self.w, "height": self.h},
            accept_downloads=False,
        )
        self.ctx.on("page", lambda p: asyncio.create_task(self._on_new_page(p)))
        self.page = await self.ctx.new_page()
        self.page.on(
            "framenavigated",
            lambda f: asyncio.create_task(self._nav_changed(True)) if f == self.page.main_frame else None,
        )
        self.page.on("load", lambda _: asyncio.create_task(self._nav_changed(False)))
        self.page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
        await self._start_screencast()
        await self.goto(HOME_URL)

    async def _start_screencast(self) -> None:
        self.cdp = await self.ctx.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": JPEG_QUALITY, "maxWidth": self.w, "maxHeight": self.h, "everyNthFrame": 1},
        )

    async def _stop_screencast(self) -> None:
        try:
            await self.cdp.send("Page.stopScreencast")
            await self.cdp.detach()
        except Exception:
            pass

    async def _on_new_page(self, popup: Page) -> None:
        """Popups / target=_blank open in the single visible tab instead."""
        try:
            if await popup.opener() is None:
                return
            await popup.wait_for_load_state("domcontentloaded", timeout=10000)
            url = popup.url
            await popup.close()
            if url and url != "about:blank":
                await self.goto(url)
        except Exception:
            pass

    async def close(self) -> None:
        await self._stop_screencast()
        try:
            await self.ctx.close()
        except Exception:
            pass

    # -- incoming ---------------------------------------------------------- #
    async def goto(self, raw: str) -> None:
        url = normalize_url(raw)
        if not url:
            await self.send_json({"type": "error", "message": "Only http(s) URLs are allowed"})
            return
        try:
            await self.page.goto(url, wait_until="commit", timeout=30000)
        except Exception as exc:
            await self.send_json({"type": "error", "message": str(exc).splitlines()[0][:200]})
            await self._nav_changed(False)

    async def handle(self, m: dict) -> None:
        t, p = m.get("type"), self.page
        try:
            if t == "goto":
                await self.goto(str(m.get("url", "")))
            elif t == "home":
                await self.goto(HOME_URL)
            elif t == "back":
                await p.go_back(wait_until="commit")
            elif t == "forward":
                await p.go_forward(wait_until="commit")
            elif t == "reload":
                await p.reload(wait_until="commit")
            elif t == "mouse":
                x, y = float(m["x"]), float(m["y"])
                action = m.get("action")
                await p.mouse.move(x, y)
                if action == "down":
                    await p.mouse.down(button=BUTTONS.get(m.get("button", 0), "left"),
                                       click_count=int(m.get("clicks", 1)))
                elif action == "up":
                    await p.mouse.up(button=BUTTONS.get(m.get("button", 0), "left"),
                                     click_count=int(m.get("clicks", 1)))
                elif action == "wheel":
                    await p.mouse.wheel(float(m.get("dx", 0)), float(m.get("dy", 0)))
            elif t == "key":
                key = m.get("key")
                if isinstance(key, str) and key and key not in ("Dead", "Unidentified", "AltGraph"):
                    if m.get("action") == "down":
                        await p.keyboard.down(key)
                    else:
                        await p.keyboard.up(key)
            elif t == "text":
                await p.keyboard.insert_text(str(m.get("text", ""))[:100_000])
            elif t == "resize":
                self.w = clamp(m.get("w"), 320, MAX_W, self.w)
                self.h = clamp(m.get("h"), 240, MAX_H, self.h)
                await self._stop_screencast()
                await p.set_viewport_size({"width": self.w, "height": self.h})
                await self._start_screencast()
        except Exception as exc:  # never let one bad event kill the session
            log.debug("event %s failed: %s", t, exc)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Authentication (in-app login page + signed session cookie)
# --------------------------------------------------------------------------- #
_fails: dict[str, list[float]] = {}


def make_token() -> str:
    exp = int(time.time()) + SESSION_TTL
    sig = hmac.new(SECRET_KEY.encode(), f"{AUTH_USER}:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(token: str | None) -> bool:
    try:
        exp, sig = (token or "").split(".", 1)
        good = hmac.new(SECRET_KEY.encode(), f"{AUTH_USER}:{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def is_locked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    _fails[ip] = recent
    return len(recent) >= 5


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in ("/healthz", "/login", "/logout") or valid_token(request.cookies.get(COOKIE)):
        return await call_next(request)
    if path == "/" and request.method == "GET":
        return HTMLResponse(LOGIN_HTML, headers={"Cache-Control": "no-store"})
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@app.post("/login")
async def login(request: Request):
    ip = client_ip(request)
    if is_locked(ip):
        return JSONResponse({"error": "Too many attempts. Try again in a few minutes."}, status_code=429)
    try:
        data = await request.json()
    except Exception:
        data = {}
    user, pw = str(data.get("username", "")), str(data.get("password", ""))
    ok = hmac.compare_digest(user.encode(), AUTH_USER.encode()) & hmac.compare_digest(pw.encode(), AUTH_PASSWORD.encode())
    if not ok:
        _fails.setdefault(ip, []).append(time.time())
        await asyncio.sleep(1)  # slow down guessing
        return JSONResponse({"error": "Incorrect username or password"}, status_code=401)
    _fails.pop(ip, None)
    resp = JSONResponse({"ok": True})
    secure = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    # SameSite=None (HTTPS only) lets the login cookie work when the site is embedded in an iframe.
    resp.set_cookie(COOKIE, make_token(), max_age=SESSION_TTL, httponly=True,
                    samesite="none" if secure else "lax", secure=secure, path="/")
    return resp


@app.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/me")
async def me():
    return {"user": AUTH_USER}


@app.get("/healthz")
async def healthz():
    browser: Browser = app.state.browser
    return JSONResponse({"ok": browser.is_connected(), "sessions": app.state.active})


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not valid_token(ws.cookies.get(COOKIE)):
        await ws.close(code=4401)
        return
    await ws.accept()
    if app.state.active >= MAX_SESSIONS:
        await ws.send_text(json.dumps({"type": "error", "message": "Server busy: max sessions reached"}))
        await ws.close(code=1013)
        return

    app.state.active += 1
    w = clamp(ws.query_params.get("w"), 320, MAX_W, 1280)
    h = clamp(ws.query_params.get("h"), 240, MAX_H, 720)
    sess = Session(ws, app.state.browser, w, h)
    try:
        await sess.start()
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                await sess.send_json({"type": "error", "message": "Closed after inactivity"})
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                await sess.handle(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("session crashed")
    finally:
        app.state.active -= 1
        await sess.close()
        try:
            await ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Front-end (single page, no external assets)
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PyBrowser</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20viewBox%3D%220%200%2096%2096%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Cdefs%3E%3ClinearGradient%20id%3D%22lgf%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%221%22%20y2%3D%221%22%3E%3Cstop%20offset%3D%220%22%20stop-color%3D%22%237c9cff%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%23b57cff%22%2F%3E%3C%2FlinearGradient%3E%3C%2Fdefs%3E%3Crect%20width%3D%2296%22%20height%3D%2296%22%20rx%3D%2226%22%20fill%3D%22url%28%23lgf%29%22%2F%3E%3Ccircle%20cx%3D%2248%22%20cy%3D%2248%22%20r%3D%2226%22%20fill%3D%22none%22%20stroke%3D%22%23fff%22%20stroke-width%3D%224%22%2F%3E%3Cellipse%20cx%3D%2248%22%20cy%3D%2248%22%20rx%3D%2211%22%20ry%3D%2226%22%20fill%3D%22none%22%20stroke%3D%22%23fff%22%20stroke-width%3D%224%22%20opacity%3D%22.9%22%2F%3E%3Cpath%20d%3D%22M22%2048h52M27%2034h42M27%2062h42%22%20stroke%3D%22%23fff%22%20stroke-width%3D%223.5%22%20stroke-linecap%3D%22round%22%20fill%3D%22none%22%20opacity%3D%22.9%22%2F%3E%3Cpath%20d%3D%22M58%2056l22%208-9%203-3%209z%22%20fill%3D%22%23fff%22%20stroke%3D%22%237c9cff%22%20stroke-width%3D%222.5%22%20stroke-linejoin%3D%22round%22%2F%3E%3C%2Fsvg%3E">
<style>
:root{--bg:#0e1015;--bar:#161922e6;--chip:#232837;--chip2:#2f3548;--fg:#e9ebf2;--mut:#8a92a8;--acc:#7c9cff;--acc2:#b57cff;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--sh:0 10px 34px #0007}
:root[data-theme=light]{--bg:#eceff5;--bar:#ffffffe6;--chip:#e5e8f0;--chip2:#d6dbe8;--fg:#1a1e2b;--mut:#657090;--sh:0 10px 34px #0002}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;overflow:hidden}
#bar{position:absolute;top:0;left:0;right:0;height:56px;display:flex;align-items:center;gap:6px;padding:0 10px;
  background:var(--bar);backdrop-filter:blur(14px);border-bottom:1px solid #ffffff12;z-index:5;animation:down .5s cubic-bezier(.2,.9,.3,1) both}
@keyframes down{from{transform:translateY(-100%);opacity:0}}
.ib{width:36px;height:36px;border:0;border-radius:50%;background:transparent;color:var(--fg);display:grid;place-items:center;
  cursor:pointer;position:relative;overflow:hidden;transition:background .2s,transform .15s}
.ib:hover{background:var(--chip2)}.ib:active{transform:scale(.88)}
.ib svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.spin svg{animation:rot .8s linear infinite;color:var(--acc)}
@keyframes rot{to{transform:rotate(360deg)}}
#pill{flex:1;min-width:0;height:38px;display:flex;align-items:center;gap:8px;padding:0 6px 0 12px;border-radius:19px;
  background:var(--chip);border:1.5px solid transparent;transition:border-color .25s,box-shadow .25s,background .25s}
#pill:focus-within{border-color:var(--acc);box-shadow:0 0 0 4px #7c9cff29;background:var(--bg)}
#lock{width:16px;height:16px;flex:none;color:var(--ok);transition:color .3s}
#lock[data-s="0"]{color:var(--warn)}
#lock svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#url{flex:1;min-width:0;height:100%;border:0;outline:0;background:transparent;color:var(--fg);font:inherit;font-size:14px}
#url::placeholder{color:var(--mut)}
#dot{width:9px;height:9px;border-radius:50%;margin:0 6px;background:var(--warn);flex:none;transition:background .3s}
#dot.on{background:var(--ok);animation:pulse 2.4s infinite}#dot.off{background:var(--bad)}
@keyframes pulse{0%{box-shadow:0 0 0 0 #4ade8088}70%,100%{box-shadow:0 0 0 8px #4ade8000}}
#prog{position:absolute;left:0;bottom:-1px;height:3px;width:0;opacity:0;background:linear-gradient(90deg,var(--acc),var(--acc2));
  border-radius:0 3px 3px 0;box-shadow:0 0 10px var(--acc)}
#stage{position:absolute;top:56px;left:0;right:0;bottom:0;background:#fff}
#cv{width:100%;height:100%;display:block;outline:none;opacity:0;transition:opacity .5s}
#cv.show{opacity:1}.loading #cv{filter:brightness(.96)}
.rip{position:absolute;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;border:2px solid var(--acc);
  background:#7c9cff33;pointer-events:none;animation:rip .55s ease-out forwards}
@keyframes rip{to{transform:scale(4);opacity:0}}
#splash,#over{position:absolute;inset:56px 0 0 0;display:grid;place-items:center;text-align:center;z-index:4;
  background:var(--bg);transition:opacity .5s,visibility .5s;overflow:hidden}
#splash.hide,#over.hide{opacity:0;visibility:hidden}
.blob{position:absolute;width:380px;height:380px;border-radius:50%;filter:blur(70px);opacity:.35;animation:float 9s ease-in-out infinite alternate}
.b1{background:var(--acc);left:12%;top:8%}.b2{background:var(--acc2);right:10%;bottom:6%;animation-delay:-4s}
@keyframes float{to{transform:translate(60px,-40px) scale(1.2)}}
.card{position:relative;animation:rise .7s cubic-bezier(.2,.9,.3,1) both}
@keyframes rise{from{transform:translateY(24px);opacity:0}}
.logo{display:block;width:84px;height:84px;margin:0 auto 18px;filter:drop-shadow(0 10px 22px #7c9cff55);animation:bob 2.2s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-8px)}}
.ring{width:26px;height:26px;margin:16px auto 0;border-radius:50%;border:3px solid var(--chip2);border-top-color:var(--acc);animation:rot .8s linear infinite}
.card h1{margin:0 0 4px;font-size:22px}.card p{margin:0;color:var(--mut)}
.btn{margin-top:18px;border:0;border-radius:22px;padding:10px 22px;font:inherit;font-weight:600;color:#fff;cursor:pointer;
  background:linear-gradient(135deg,var(--acc),var(--acc2));transition:transform .15s,box-shadow .2s}
.btn:hover{transform:translateY(-2px);box-shadow:0 8px 20px #7c9cff55}.btn:active{transform:scale(.95)}
#toasts{position:absolute;right:14px;bottom:14px;display:flex;flex-direction:column;gap:8px;z-index:9;pointer-events:none}
.toast{background:var(--bar);backdrop-filter:blur(12px);border:1px solid #ffffff1a;border-left:4px solid var(--bad);color:var(--fg);
  padding:10px 14px;border-radius:10px;box-shadow:var(--sh);max-width:340px;animation:in .35s cubic-bezier(.2,.9,.3,1) both}
.toast.out{animation:out .3s forwards}
@keyframes in{from{transform:translateX(120%);opacity:0}}@keyframes out{to{transform:translateX(120%);opacity:0}}
@media (max-width:560px){#bar .opt{display:none}}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01s!important;transition-duration:.01s!important}}
</style></head>
<body>
<div id="bar">
  <button class="ib" id="back" title="Back (Alt+&larr;)"><svg viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></svg></button>
  <button class="ib opt" id="fwd" title="Forward (Alt+&rarr;)"><svg viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7"/></svg></button>
  <button class="ib" id="rel" title="Reload (Ctrl+R)"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/></svg></button>
  <button class="ib opt" id="home" title="Home"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/></svg></button>
  <div id="pill">
    <span id="lock" data-s="1"><svg viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg></span>
    <input id="url" placeholder="Search or enter address (Ctrl+L)" autocomplete="off" spellcheck="false">
  </div>
  <span id="dot" class="" title="Connection"></span>
  <button class="ib opt" id="theme" title="Toggle theme"><svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg></button>
  <button class="ib opt" id="full" title="Fullscreen"><svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg></button>
  <button class="ib opt" id="logout" title="Sign out"><svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg></button>
  <div id="prog"></div>
</div>
<div id="stage"><canvas id="cv" tabindex="0"></canvas></div>
<div id="splash"><div class="blob b1"></div><div class="blob b2"></div>
  <div class="card"><svg class="logo" role="img" aria-label="PyBrowser" viewBox="0 0 96 96" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="lg1" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7c9cff"/><stop offset="1" stop-color="#b57cff"/></linearGradient></defs><rect width="96" height="96" rx="26" fill="url(#lg1)"/><circle cx="48" cy="48" r="26" fill="none" stroke="#fff" stroke-width="4"/><ellipse cx="48" cy="48" rx="11" ry="26" fill="none" stroke="#fff" stroke-width="4" opacity=".9"/><path d="M22 48h52M27 34h42M27 62h42" stroke="#fff" stroke-width="3.5" stroke-linecap="round" fill="none" opacity=".9"/><path d="M58 56l22 8-9 3-3 9z" fill="#fff" stroke="#7c9cff" stroke-width="2.5" stroke-linejoin="round"/></svg><p id="splashTxt">Starting your private browser&hellip;</p><div class="ring"></div></div></div>
<div id="over" class="hide"><div class="blob b1"></div><div class="blob b2"></div>
  <div class="card"><svg class="logo" role="img" aria-label="PyBrowser" viewBox="0 0 96 96" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="lg3" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7c9cff"/><stop offset="1" stop-color="#b57cff"/></linearGradient></defs><rect width="96" height="96" rx="26" fill="url(#lg3)"/><circle cx="48" cy="48" r="26" fill="none" stroke="#fff" stroke-width="4"/><ellipse cx="48" cy="48" rx="11" ry="26" fill="none" stroke="#fff" stroke-width="4" opacity=".9"/><path d="M22 48h52M27 34h42M27 62h42" stroke="#fff" stroke-width="3.5" stroke-linecap="round" fill="none" opacity=".9"/><path d="M58 56l22 8-9 3-3 9z" fill="#fff" stroke="#7c9cff" stroke-width="2.5" stroke-linejoin="round"/></svg><h1 id="overH">Disconnected</h1><p id="overP">The session ended.</p><button class="btn" id="reco">Reconnect</button></div></div>
<div id="toasts"></div>
<script>
const $=id=>document.getElementById(id);
const stage=$('stage'),cv=$('cv'),ctx=cv.getContext('2d'),url=$('url'),prog=$('prog'),dot=$('dot'),lock=$('lock'),rel=$('rel'),splash=$('splash'),over=$('over'),root=document.documentElement;
let ws,last=0,lastErr='',q=Promise.resolve(),loadT,opened=false;
const size=()=>({w:Math.max(320,Math.min(1920,stage.clientWidth|0)),h:Math.max(240,Math.min(1080,stage.clientHeight|0))});
const send=o=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify(o))};

function toast(t){const d=document.createElement('div');d.className='toast';d.textContent=t;$('toasts').append(d);
  setTimeout(()=>{d.classList.add('out');setTimeout(()=>d.remove(),300)},4200)}

function setLoading(on){
  clearTimeout(loadT);root.classList.toggle('loading',on);rel.classList.toggle('spin',on);
  if(on){prog.style.transition='none';prog.style.opacity=1;prog.style.width='0%';void prog.offsetWidth;
    prog.style.transition='width 8s cubic-bezier(.1,.8,.2,1)';prog.style.width='86%';loadT=setTimeout(()=>setLoading(false),30000)}
  else{prog.style.transition='width .25s';prog.style.width='100%';
    setTimeout(()=>{prog.style.transition='opacity .35s';prog.style.opacity=0},260)}
}

function connect(){
  lastErr='';opened=false;dot.className='';over.classList.add('hide');splash.classList.remove('hide');
  $('splashTxt').textContent='Starting your private browser\u2026';
  const {w,h}=size();
  ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws?w='+w+'&h='+h);
  ws.onopen=()=>{opened=true;dot.className='on';cv.focus()};
  ws.onmessage=e=>{
    if(typeof e.data==='string'){
      const m=JSON.parse(e.data);
      if(m.type==='nav'){
        if(document.activeElement!==url)url.value=m.url==='about:blank'?'':m.url;
        lock.dataset.s=m.url.startsWith('https:')?'1':'0';
        document.title=m.title?m.title+' \u2013 PyBrowser':'PyBrowser';
        setLoading(!!m.loading);
      }else if(m.type==='error'){lastErr=m.message;toast(m.message)}
      return;
    }
    q=q.then(async()=>{
      const bmp=await createImageBitmap(e.data);
      if(cv.width!==bmp.width||cv.height!==bmp.height){cv.width=bmp.width;cv.height=bmp.height}
      ctx.drawImage(bmp,0,0);bmp.close();
      if(!cv.classList.contains('show')){cv.classList.add('show');splash.classList.add('hide')}
    }).catch(()=>{});
  };
  ws.onclose=()=>{
    if(!opened)fetch('/api/me').then(r=>{if(r.status===401)location.reload()}).catch(()=>{});
    dot.className='off';cv.classList.remove('show');setLoading(false);
    $('overH').textContent=/busy/i.test(lastErr)?'Server is busy':'Disconnected';
    $('overP').textContent=lastErr||'The session ended.';
    splash.classList.add('hide');over.classList.remove('hide');
  };
}
$('reco').onclick=connect;

$('back').onclick=()=>send({type:'back'});
$('fwd').onclick=()=>send({type:'forward'});
rel.onclick=()=>send({type:'reload'});
$('home').onclick=()=>send({type:'home'});
$('logout').onclick=async()=>{try{await fetch('/logout',{method:'POST'})}catch(e){}location.reload()};
$('full').onclick=()=>document.fullscreenElement?document.exitFullscreen():root.requestFullscreen().catch(()=>{});
$('theme').onclick=()=>{const t=root.dataset.theme==='dark'?'light':'dark';root.dataset.theme=t;try{localStorage.setItem('pb-theme',t)}catch(e){}};
try{const t=localStorage.getItem('pb-theme');if(t)root.dataset.theme=t}catch(e){}

url.addEventListener('keydown',e=>{
  if(e.key==='Enter'){send({type:'goto',url:url.value});url.blur();cv.focus()}
  else if(e.key==='Escape'){url.blur();cv.focus()}
});
url.addEventListener('focus',()=>setTimeout(()=>url.select(),0));

const pt=e=>{const r=cv.getBoundingClientRect();return{x:(e.clientX-r.left)*cv.width/r.width,y:(e.clientY-r.top)*cv.height/r.height}};
cv.addEventListener('mousemove',e=>{const n=performance.now();if(n-last<30)return;last=n;send({type:'mouse',action:'move',...pt(e)})});
cv.addEventListener('mousedown',e=>{
  cv.focus();send({type:'mouse',action:'down',button:e.button,clicks:e.detail||1,...pt(e)});
  const r=stage.getBoundingClientRect(),d=document.createElement('div');d.className='rip';
  d.style.left=(e.clientX-r.left)+'px';d.style.top=(e.clientY-r.top)+'px';stage.append(d);d.onanimationend=()=>d.remove();
});
cv.addEventListener('mouseup',e=>send({type:'mouse',action:'up',button:e.button,clicks:e.detail||1,...pt(e)}));
cv.addEventListener('wheel',e=>{e.preventDefault();send({type:'mouse',action:'wheel',dx:e.deltaX,dy:e.deltaY,...pt(e)})},{passive:false});
cv.addEventListener('contextmenu',e=>e.preventDefault());

const mod=e=>e.ctrlKey||e.metaKey;
cv.addEventListener('keydown',e=>{
  const k=e.key.toLowerCase();
  if(mod(e)&&k==='v')return;                                   // let the paste event fire
  if((mod(e)&&k==='l')||e.key==='F6'){e.preventDefault();url.focus();return}
  if((mod(e)&&k==='r')||e.key==='F5'){e.preventDefault();send({type:'reload'});return}
  if(e.altKey&&e.key==='ArrowLeft'){e.preventDefault();send({type:'back'});return}
  if(e.altKey&&e.key==='ArrowRight'){e.preventDefault();send({type:'forward'});return}
  e.preventDefault();send({type:'key',action:'down',key:e.key});
});
cv.addEventListener('keyup',e=>{if(mod(e)&&e.key.toLowerCase()==='v')return;e.preventDefault();send({type:'key',action:'up',key:e.key})});
cv.addEventListener('paste',e=>{e.preventDefault();const t=e.clipboardData.getData('text');if(t)send({type:'text',text:t})});

let rt;addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>{const {w,h}=size();send({type:'resize',w,h})},250)});
connect();
</script></body></html>
"""


LOGIN_HTML = r"""<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in &ndash; PyBrowser</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20viewBox%3D%220%200%2096%2096%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Cdefs%3E%3ClinearGradient%20id%3D%22lgf%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%221%22%20y2%3D%221%22%3E%3Cstop%20offset%3D%220%22%20stop-color%3D%22%237c9cff%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%23b57cff%22%2F%3E%3C%2FlinearGradient%3E%3C%2Fdefs%3E%3Crect%20width%3D%2296%22%20height%3D%2296%22%20rx%3D%2226%22%20fill%3D%22url%28%23lgf%29%22%2F%3E%3Ccircle%20cx%3D%2248%22%20cy%3D%2248%22%20r%3D%2226%22%20fill%3D%22none%22%20stroke%3D%22%23fff%22%20stroke-width%3D%224%22%2F%3E%3Cellipse%20cx%3D%2248%22%20cy%3D%2248%22%20rx%3D%2211%22%20ry%3D%2226%22%20fill%3D%22none%22%20stroke%3D%22%23fff%22%20stroke-width%3D%224%22%20opacity%3D%22.9%22%2F%3E%3Cpath%20d%3D%22M22%2048h52M27%2034h42M27%2062h42%22%20stroke%3D%22%23fff%22%20stroke-width%3D%223.5%22%20stroke-linecap%3D%22round%22%20fill%3D%22none%22%20opacity%3D%22.9%22%2F%3E%3Cpath%20d%3D%22M58%2056l22%208-9%203-3%209z%22%20fill%3D%22%23fff%22%20stroke%3D%22%237c9cff%22%20stroke-width%3D%222.5%22%20stroke-linejoin%3D%22round%22%2F%3E%3C%2Fsvg%3E">
<style>
:root{--bg:#0e1015;--bar:#161922e6;--chip:#232837;--chip2:#2f3548;--fg:#e9ebf2;--mut:#8a92a8;--acc:#7c9cff;--acc2:#b57cff;--ok:#4ade80;--bad:#f87171;--sh:0 10px 34px #0007}
:root[data-theme=light]{--bg:#eceff5;--bar:#ffffffe6;--chip:#e5e8f0;--chip2:#d6dbe8;--fg:#1a1e2b;--mut:#657090;--sh:0 10px 34px #0002}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;overflow:hidden}
body{display:grid;place-items:center}
.blob{position:absolute;width:420px;height:420px;border-radius:50%;filter:blur(80px);opacity:.35;animation:float 9s ease-in-out infinite alternate}
.b1{background:var(--acc);left:8%;top:6%}.b2{background:var(--acc2);right:8%;bottom:4%;animation-delay:-4s}
@keyframes float{to{transform:translate(60px,-40px) scale(1.2)}}
#card{position:relative;width:min(380px,92vw);padding:34px 28px 28px;border-radius:22px;background:var(--bar);backdrop-filter:blur(16px);
  border:1px solid #ffffff1a;box-shadow:var(--sh);text-align:center;animation:rise .7s cubic-bezier(.2,.9,.3,1) both}
@keyframes rise{from{transform:translateY(26px);opacity:0}}
#card.shake{animation:shake .45s}
@keyframes shake{20%,60%{transform:translateX(-9px)}40%,80%{transform:translateX(9px)}}
.logo{display:block;width:84px;height:84px;margin:0 auto 18px;filter:drop-shadow(0 10px 22px #7c9cff55);animation:bob 2.2s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-6px)}}
h1{margin:0 0 4px;font-size:22px}p.s{margin:0 0 22px;color:var(--mut)}
.f{position:relative;margin-bottom:12px;text-align:left}
.f input{width:100%;height:46px;padding:0 44px 0 14px;border-radius:12px;border:1.5px solid transparent;background:var(--chip);color:var(--fg);
  font:inherit;outline:0;transition:border-color .25s,box-shadow .25s,background .25s}
.f input:focus{border-color:var(--acc);box-shadow:0 0 0 4px #7c9cff29;background:var(--bg)}
.f input::placeholder{color:var(--mut)}
#eye{position:absolute;right:6px;top:5px;width:36px;height:36px;border:0;border-radius:50%;background:transparent;color:var(--mut);cursor:pointer;transition:color .2s,background .2s}
#eye:hover{color:var(--fg);background:var(--chip2)}
#eye svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#go{width:100%;height:46px;margin-top:6px;border:0;border-radius:12px;font:inherit;font-weight:600;color:#fff;cursor:pointer;position:relative;
  background:linear-gradient(135deg,var(--acc),var(--acc2));transition:transform .15s,box-shadow .25s,filter .2s}
#go:hover{transform:translateY(-2px);box-shadow:0 10px 24px #7c9cff55}#go:active{transform:scale(.97)}
#go:disabled{cursor:default;filter:saturate(.7)}
#go.busy span{opacity:0}
#go.busy::after{content:"";position:absolute;left:50%;top:50%;width:20px;height:20px;margin:-10px;border-radius:50%;border:3px solid #fff5;border-top-color:#fff;animation:rot .7s linear infinite}
#go.ok{background:var(--ok)}
@keyframes rot{to{transform:rotate(360deg)}}
#err{min-height:20px;margin-top:12px;color:var(--bad);font-size:13px}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01s!important;transition-duration:.01s!important}}
</style></head>
<body>
<div class="blob b1"></div><div class="blob b2"></div>
<form id="card" autocomplete="on">
  <svg class="logo" role="img" aria-label="PyBrowser" viewBox="0 0 96 96" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="lg2" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7c9cff"/><stop offset="1" stop-color="#b57cff"/></linearGradient></defs><rect width="96" height="96" rx="26" fill="url(#lg2)"/><circle cx="48" cy="48" r="26" fill="none" stroke="#fff" stroke-width="4"/><ellipse cx="48" cy="48" rx="11" ry="26" fill="none" stroke="#fff" stroke-width="4" opacity=".9"/><path d="M22 48h52M27 34h42M27 62h42" stroke="#fff" stroke-width="3.5" stroke-linecap="round" fill="none" opacity=".9"/><path d="M58 56l22 8-9 3-3 9z" fill="#fff" stroke="#7c9cff" stroke-width="2.5" stroke-linejoin="round"/></svg><p class="s">Sign in to start browsing</p>
  <div class="f"><input id="u" name="username" placeholder="Username" autocomplete="username" autofocus required></div>
  <div class="f"><input id="p" name="password" type="password" placeholder="Password" autocomplete="current-password" required>
    <button type="button" id="eye" title="Show password"><svg viewBox="0 0 24 24"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg></button></div>
  <button id="go" type="submit"><span>Sign in</span></button>
  <div id="err" role="alert"></div>
</form>
<script>
const $=id=>document.getElementById(id),card=$('card'),u=$('u'),p=$('p'),go=$('go'),err=$('err');
try{const t=localStorage.getItem('pb-theme');if(t)document.documentElement.dataset.theme=t}catch(e){}
$('eye').onclick=()=>{p.type=p.type==='password'?'text':'password';p.focus()};
function fail(m){err.textContent=m;card.classList.remove('shake');void card.offsetWidth;card.classList.add('shake');go.classList.remove('busy');go.disabled=false;p.select()}
card.onsubmit=async e=>{
  e.preventDefault();err.textContent='';go.classList.add('busy');go.disabled=true;
  try{
    const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});
    if(r.ok){go.classList.remove('busy');go.classList.add('ok');go.firstChild.textContent='Welcome \u2713';setTimeout(()=>location.reload(),400);return}
    const d=await r.json().catch(()=>({}));fail(d.error||'Sign-in failed');
  }catch(x){fail('Network error. Please try again.')}
};
</script></body></html>
"""
