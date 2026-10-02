"""PyBrowser: a Chromium-based remote browser written in Python 3.11.

Chromium runs headless on the server (via Playwright). Its screen is streamed to
the web page as JPEG frames over a WebSocket (CDP screencast), and mouse /
keyboard input from the page is replayed into Chromium.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import Browser, Page, async_playwright

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("pybrowser")

MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "2"))
HOME_URL = os.getenv("HOME_URL", "https://duckduckgo.com")
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "60"))
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "900"))  # seconds
MAX_W, MAX_H = 1920, 1080
# "chrome" = real Google Chrome (has H.264/AAC codecs for video). Set to "" for bundled Chromium.
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")
PROXY_SERVER = os.getenv("PROXY_SERVER", "")  # optional, e.g. http://user:pass@host:port
AUDIO_ENABLED = os.getenv("AUDIO", "1") != "0"
AUDIO_RATE = int(os.getenv("AUDIO_RATE", "32000"))  # Hz, 16-bit stereo PCM
STREAM_EVERY_NTH = int(os.getenv("STREAM_EVERY_NTH", "2"))  # 1 = ~60 fps, 2 = ~30 fps (lighter on CPU)

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
]


# --------------------------------------------------------------------------- #
# App lifecycle
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    pw = await async_playwright().start()
    launch_kw = {
        "headless": True,
        "channel": BROWSER_CHANNEL or None,
        "args": CHROMIUM_ARGS,
        "ignore_default_args": ["--enable-automation", "--mute-audio"],
    }
    if PROXY_SERVER:
        launch_kw["proxy"] = {"server": PROXY_SERVER}
    app.state.browser = await pw.chromium.launch(**launch_kw)
    app.state.active = 0
    app.state.audio_clients = set()
    app.state.audio_on = False
    audio_task = asyncio.create_task(audio_broadcaster(app)) if AUDIO_ENABLED else None
    log.info("Browser %s ready (channel=%s)", app.state.browser.version, BROWSER_CHANNEL or "chromium")
    try:
        yield
    finally:
        if audio_task:
            audio_task.cancel()
        await app.state.browser.close()
        await pw.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def audio_broadcaster(app: FastAPI) -> None:
    """Capture Chrome's audio from the virtual PulseAudio sink and fan it out to listeners."""
    quick_fails = 0
    while quick_fails < 5:
        started = asyncio.get_event_loop().time()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "parec", "-d", "pbsink.monitor", "--format=s16le",
                f"--rate={AUDIO_RATE}", "--channels=2", "--latency-msec=30",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            app.state.audio_on = True
            while True:
                chunk = await proc.stdout.read(6400)  # ~50 ms
                if not chunk:
                    break
                if chunk.count(0) == len(chunk):
                    continue  # pure silence: don't waste bandwidth
                for q in list(app.state.audio_clients):
                    if q.full():
                        q.get_nowait()  # listener too slow: drop oldest to stay live
                    q.put_nowait(chunk)
        except FileNotFoundError:
            log.warning("parec not found: audio disabled")
            break
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            raise
        except Exception:
            log.exception("audio capture error")
        app.state.audio_on = False
        quick_fails = quick_fails + 1 if asyncio.get_event_loop().time() - started < 5 else 0
        await asyncio.sleep(2)
    log.warning("Audio capture unavailable (is PulseAudio running?)")


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
        self._latest: bytes | None = None
        self._frame_evt = asyncio.Event()
        self._inbox: deque = deque()
        self._inbox_evt = asyncio.Event()
        self._tasks: list = []
        self._pos = (-1.0, -1.0)

    # -- outgoing ---------------------------------------------------------- #
    async def send_json(self, obj: dict) -> None:
        async with self._lock:
            try:
                await self.ws.send_text(json.dumps(obj))
            except Exception:
                pass

    async def _on_frame(self, params: dict) -> None:
        # Keep only the newest frame and ack immediately so Chrome never stalls on the network.
        self._latest = base64.b64decode(params["data"])
        self._frame_evt.set()
        try:
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass

    async def _send_frames(self) -> None:
        while True:
            await self._frame_evt.wait()
            self._frame_evt.clear()
            data, self._latest = self._latest, None
            if data is None:
                continue
            try:
                async with self._lock:
                    await self.ws.send_bytes(data)
            except Exception:
                return

    def push(self, m: dict) -> None:
        self._inbox.append(m)
        self._inbox_evt.set()

    async def _input_worker(self) -> None:
        """Process input in order, but merge stale mouse-moves and consecutive wheel events."""
        def kind(x):
            return (x.get("type"), x.get("action")) if x.get("type") == "mouse" else (x.get("type"), None)
        while True:
            await self._inbox_evt.wait()
            while self._inbox:
                m = self._inbox.popleft()
                k = kind(m)
                if k == ("mouse", "move") and self._inbox and kind(self._inbox[0]) == k:
                    continue
                if k == ("mouse", "wheel"):
                    while self._inbox and kind(self._inbox[0]) == k:
                        n = self._inbox.popleft()
                        m["dx"] = float(m.get("dx", 0)) + float(n.get("dx", 0))
                        m["dy"] = float(m.get("dy", 0)) + float(n.get("dy", 0))
                        m["x"], m["y"] = n["x"], n["y"]
                await self.handle(m)
            self._inbox_evt.clear()

    async def _nav_changed(self, loading: bool = False) -> None:
        try:
            title = await self.page.title()
        except Exception:
            title = ""
        await self.send_json({"type": "nav", "url": self.page.url, "title": title, "loading": loading})

    # -- lifecycle --------------------------------------------------------- #
    async def start(self) -> None:
        major = self.browser.version.split(".")[0]
        self.ctx = await self.browser.new_context(
            viewport={"width": self.w, "height": self.h},
            accept_downloads=False,
            # Normal Chrome UA instead of "HeadlessChrome" (sites like YouTube block the latter).
            user_agent=f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
            locale="en-US",
        )
        await self.ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self.ctx.on("page", lambda p: asyncio.create_task(self._on_new_page(p)))
        self.page = await self.ctx.new_page()
        self.page.on(
            "framenavigated",
            lambda f: asyncio.create_task(self._nav_changed(True)) if f == self.page.main_frame else None,
        )
        self.page.on("load", lambda _: asyncio.create_task(self._nav_changed(False)))
        self.page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
        self._tasks = [asyncio.create_task(self._send_frames()), asyncio.create_task(self._input_worker())]
        await self._start_screencast()
        await self.goto(HOME_URL)

    async def _start_screencast(self) -> None:
        self.cdp = await self.ctx.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": JPEG_QUALITY, "maxWidth": self.w, "maxHeight": self.h, "everyNthFrame": STREAM_EVERY_NTH},
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
        for t in self._tasks:
            t.cancel()
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
                if (x, y) != self._pos:
                    await p.mouse.move(x, y)
                    self._pos = (x, y)
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
@app.middleware("http")
async def allow_embedding(request, call_next):
    """Let any site embed this app in an <iframe>."""
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = "frame-ancestors *"
    if "x-frame-options" in resp.headers:
        del resp.headers["x-frame-options"]
    return resp


@app.get("/healthz")
async def healthz():
    browser: Browser = app.state.browser
    return JSONResponse({"ok": browser.is_connected(), "sessions": app.state.active})


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.websocket("/ws/audio")
async def audio_ws(ws: WebSocket):
    await ws.accept()
    if not app.state.audio_on:
        await ws.send_text(json.dumps({"type": "noaudio"}))
        await ws.close()
        return
    q: asyncio.Queue = asyncio.Queue(maxsize=24)
    app.state.audio_clients.add(q)
    await ws.send_text(json.dumps({"type": "format", "rate": AUDIO_RATE, "channels": 2}))

    async def pump():
        while True:
            await ws.send_bytes(await q.get())

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()  # just waits until the client leaves
    except Exception:
        pass
    finally:
        pump_task.cancel()
        app.state.audio_clients.discard(q)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
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
                sess.push(msg)
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        # Starlette raises this when a frame send failed because the client already left.
        log.debug("websocket closed: %s", exc)
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
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Newsreader:ital,opsz,wght@0,6..72,400..600;1,6..72,400..500&display=swap">
<link rel="icon" href="data:image/svg+xml,%3Csvg%20viewBox%3D%220%200%2096%2096%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Cdefs%3E%3ClinearGradient%20id%3D%22lgf%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%221%22%20y2%3D%221%22%3E%3Cstop%20offset%3D%220%22%20stop-color%3D%22%23f7e39a%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%23b8860b%22%2F%3E%3C%2FlinearGradient%3E%3C%2Fdefs%3E%3Crect%20width%3D%2296%22%20height%3D%2296%22%20rx%3D%2226%22%20fill%3D%22url%28%23lgf%29%22%2F%3E%3Ccircle%20cx%3D%2248%22%20cy%3D%2248%22%20r%3D%2226%22%20fill%3D%22none%22%20stroke%3D%22%2314110a%22%20stroke-width%3D%224%22%2F%3E%3Cellipse%20cx%3D%2248%22%20cy%3D%2248%22%20rx%3D%2211%22%20ry%3D%2226%22%20fill%3D%22none%22%20stroke%3D%22%2314110a%22%20stroke-width%3D%224%22%20opacity%3D%22.9%22%2F%3E%3Cpath%20d%3D%22M22%2048h52M27%2034h42M27%2062h42%22%20stroke%3D%22%2314110a%22%20stroke-width%3D%223.5%22%20stroke-linecap%3D%22round%22%20fill%3D%22none%22%20opacity%3D%22.9%22%2F%3E%3Cpath%20d%3D%22M58%2056l22%208-9%203-3%209z%22%20fill%3D%22%2314110a%22%20stroke%3D%22%23f7e39a%22%20stroke-width%3D%222.5%22%20stroke-linejoin%3D%22round%22%2F%3E%3C%2Fsvg%3E">
<style>
:root{--bg:#0a0907;--bar:#14110ce6;--chip:#1d1912;--chip2:#2c2518;--fg:#f4ecd8;--mut:#9a8f76;--acc:#d4af37;--acc2:#f3d98b;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--sh:0 10px 34px #000a}
:root[data-theme=light]{--bg:#f6f0e1;--bar:#fffaf0e6;--chip:#ede4cd;--chip2:#e0d4b5;--fg:#1d1710;--mut:#7a6d52;--sh:0 10px 34px #0002}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 Inter,system-ui,-apple-system,Segoe UI,sans-serif;overflow:hidden}
#bar{position:absolute;top:0;left:0;right:0;height:56px;display:flex;align-items:center;gap:6px;padding:0 10px;
  background:var(--bar);backdrop-filter:blur(14px);border-bottom:1px solid #d4af3730;z-index:5;animation:down .5s cubic-bezier(.2,.9,.3,1) both}
@keyframes down{from{transform:translateY(-100%);opacity:0}}
.ib{width:36px;height:36px;border:0;border-radius:50%;background:transparent;color:var(--fg);display:grid;place-items:center;
  cursor:pointer;position:relative;overflow:hidden;transition:background .2s,transform .15s}
.ib:hover{background:var(--chip2)}.ib:active{transform:scale(.88)}
.ib svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.spin svg{animation:rot .8s linear infinite;color:var(--acc)}
@keyframes rot{to{transform:rotate(360deg)}}
#pill{flex:1;min-width:0;height:38px;display:flex;align-items:center;gap:8px;padding:0 6px 0 12px;border-radius:19px;
  background:var(--chip);border:1.5px solid transparent;transition:border-color .25s,box-shadow .25s,background .25s}
#pill:focus-within{border-color:var(--acc);box-shadow:0 0 0 4px #d4af3730;background:var(--bg)}
#lock{width:16px;height:16px;flex:none;color:var(--acc);transition:color .3s}
#lock[data-s="0"]{color:var(--bad)}
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
  background:#d4af3740;pointer-events:none;animation:rip .55s ease-out forwards}
@keyframes rip{to{transform:scale(4);opacity:0}}
#splash,#over{position:absolute;inset:56px 0 0 0;display:grid;place-items:center;text-align:center;z-index:4;
  background:var(--bg);transition:opacity .5s,visibility .5s;overflow:hidden}
#splash.hide,#over.hide{opacity:0;visibility:hidden}
.blob{position:absolute;width:380px;height:380px;border-radius:50%;filter:blur(70px);opacity:.2;animation:float 9s ease-in-out infinite alternate}
.b1{background:var(--acc);left:12%;top:8%}.b2{background:var(--acc2);right:10%;bottom:6%;animation-delay:-4s}
@keyframes float{to{transform:translate(60px,-40px) scale(1.2)}}
.card{position:relative;animation:rise .7s cubic-bezier(.2,.9,.3,1) both}
@keyframes rise{from{transform:translateY(24px);opacity:0}}
.logo{display:block;width:84px;height:84px;margin:0 auto 18px;filter:drop-shadow(0 10px 22px #d4af3755);animation:bob 2.2s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-8px)}}
.ring{width:26px;height:26px;margin:16px auto 0;border-radius:50%;border:3px solid var(--chip2);border-top-color:var(--acc);animation:rot .8s linear infinite}
.card h1{margin:0 0 6px;font:500 30px/1.15 Newsreader,'Iowan Old Style',Georgia,serif;letter-spacing:-.01em}
.card p{margin:0;color:var(--mut);font:italic 400 19px/1.4 Newsreader,'Iowan Old Style',Georgia,serif}
.btn{margin-top:18px;border:0;border-radius:22px;padding:10px 22px;font:inherit;font-weight:600;color:#14110a;cursor:pointer;
  background:linear-gradient(135deg,var(--acc2),var(--acc));transition:transform .15s,box-shadow .2s}
.btn:hover{transform:translateY(-2px);box-shadow:0 8px 20px #d4af3755}.btn:active{transform:scale(.95)}
#toasts{position:absolute;right:14px;bottom:14px;display:flex;flex-direction:column;gap:8px;z-index:9;pointer-events:none}
.toast{background:var(--bar);backdrop-filter:blur(12px);border:1px solid #ffffff1a;border-left:4px solid var(--bad);color:var(--fg);
  padding:10px 14px;border-radius:10px;box-shadow:var(--sh);max-width:340px;animation:in .35s cubic-bezier(.2,.9,.3,1) both}
.toast.out{animation:out .3s forwards}
@keyframes in{from{transform:translateX(120%);opacity:0}}@keyframes out{to{transform:translateX(120%);opacity:0}}
#snd .w{opacity:.55}#snd.live .w{opacity:1;animation:wave .9s ease-in-out infinite}#snd.muted .w{display:none}#snd.off{opacity:.4}
@keyframes wave{50%{opacity:.3}}
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
  <button class="ib" id="snd" title="Sound (click page or here to enable)"><svg viewBox="0 0 24 24"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path class="w" d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/></svg></button>
  <button class="ib opt" id="theme" title="Toggle theme"><svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg></button>
  <button class="ib opt" id="full" title="Fullscreen"><svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg></button>
  <div id="prog"></div>
</div>
<div id="stage"><canvas id="cv" tabindex="0"></canvas></div>
<div id="splash"><div class="blob b1"></div><div class="blob b2"></div>
  <div class="card"><svg class="logo" role="img" aria-label="PyBrowser" viewBox="0 0 96 96" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="lg1" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f7e39a"/><stop offset="1" stop-color="#b8860b"/></linearGradient></defs><rect width="96" height="96" rx="26" fill="url(#lg1)"/><circle cx="48" cy="48" r="26" fill="none" stroke="#14110a" stroke-width="4"/><ellipse cx="48" cy="48" rx="11" ry="26" fill="none" stroke="#14110a" stroke-width="4" opacity=".9"/><path d="M22 48h52M27 34h42M27 62h42" stroke="#14110a" stroke-width="3.5" stroke-linecap="round" fill="none" opacity=".9"/><path d="M58 56l22 8-9 3-3 9z" fill="#14110a" stroke="#f7e39a" stroke-width="2.5" stroke-linejoin="round"/></svg><p id="splashTxt">Starting your private browser&hellip;</p><div class="ring"></div></div></div>
<div id="over" class="hide"><div class="blob b1"></div><div class="blob b2"></div>
  <div class="card"><svg class="logo" role="img" aria-label="PyBrowser" viewBox="0 0 96 96" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="lg2" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f7e39a"/><stop offset="1" stop-color="#b8860b"/></linearGradient></defs><rect width="96" height="96" rx="26" fill="url(#lg2)"/><circle cx="48" cy="48" r="26" fill="none" stroke="#14110a" stroke-width="4"/><ellipse cx="48" cy="48" rx="11" ry="26" fill="none" stroke="#14110a" stroke-width="4" opacity=".9"/><path d="M22 48h52M27 34h42M27 62h42" stroke="#14110a" stroke-width="3.5" stroke-linecap="round" fill="none" opacity=".9"/><path d="M58 56l22 8-9 3-3 9z" fill="#14110a" stroke="#f7e39a" stroke-width="2.5" stroke-linejoin="round"/></svg><h1 id="overH">Disconnected</h1><p id="overP">The session ended.</p><button class="btn" id="reco">Reconnect</button></div></div>
<div id="toasts"></div>
<script>
const $=id=>document.getElementById(id);
const stage=$('stage'),cv=$('cv'),ctx=cv.getContext('2d',{alpha:false,desynchronized:true}),url=$('url'),prog=$('prog'),dot=$('dot'),lock=$('lock'),rel=$('rel'),splash=$('splash'),over=$('over'),root=document.documentElement;
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

let pend=null,drawing=false;
function drawLatest(){                      // always draw the newest frame, skip stale ones
  if(drawing||!pend)return;drawing=true;const b=pend;pend=null;
  createImageBitmap(b).then(bmp=>{
    if(cv.width!==bmp.width||cv.height!==bmp.height){cv.width=bmp.width;cv.height=bmp.height}
    ctx.drawImage(bmp,0,0);bmp.close();
    if(!cv.classList.contains('show')){cv.classList.add('show');splash.classList.add('hide')}
  }).catch(()=>{}).finally(()=>{drawing=false;if(pend)requestAnimationFrame(drawLatest)});
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
    pend=e.data;drawLatest();
  };
  ws.onclose=()=>{
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
$('full').onclick=()=>document.fullscreenElement?document.exitFullscreen():root.requestFullscreen().catch(()=>{});
$('theme').onclick=()=>{const t=root.dataset.theme==='dark'?'light':'dark';root.dataset.theme=t;try{localStorage.setItem('pb-theme',t)}catch(e){}};
try{const t=localStorage.getItem('pb-theme');if(t)root.dataset.theme=t}catch(e){}

url.addEventListener('keydown',e=>{
  if(e.key==='Enter'){send({type:'goto',url:url.value});url.blur();cv.focus()}
  else if(e.key==='Escape'){url.blur();cv.focus()}
});
url.addEventListener('focus',()=>setTimeout(()=>url.select(),0));

const pt=e=>{const r=cv.getBoundingClientRect();return{x:(e.clientX-r.left)*cv.width/r.width,y:(e.clientY-r.top)*cv.height/r.height}};
cv.addEventListener('mousemove',e=>{const n=performance.now();if(n-last<16)return;last=n;send({type:'mouse',action:'move',...pt(e)})});
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
/* ---------- sound ---------- */
let actx,gain,nextT=0,aws,rem=new Uint8Array(0),arate=32000,muted=false,aT;
const snd=$('snd');
function initAudio(){
  if(actx){if(actx.state==='suspended')actx.resume();return}
  try{actx=new (window.AudioContext||window.webkitAudioContext)({latencyHint:'interactive'})}catch(e){return}
  gain=actx.createGain();gain.connect(actx.destination);connectAudio();
}
function connectAudio(){
  aws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws/audio');
  aws.binaryType='arraybuffer';
  aws.onmessage=e=>{
    if(typeof e.data==='string'){const m=JSON.parse(e.data);
      if(m.type==='format')arate=m.rate;
      if(m.type==='noaudio'){snd.classList.add('off');snd.title='Sound unavailable on this server'}return}
    playPcm(new Uint8Array(e.data));
  };
  aws.onclose=()=>setTimeout(()=>{if(actx)connectAudio()},2000);
}
function playPcm(u8){
  if(muted||!actx)return;
  if(rem.length){const t=new Uint8Array(rem.length+u8.length);t.set(rem);t.set(u8,rem.length);u8=t}
  const n=Math.floor(u8.length/4);rem=u8.slice(n*4);if(!n)return;
  const pcm=new Int16Array(u8.buffer,u8.byteOffset,n*2);
  const buf=actx.createBuffer(2,n,arate),L=buf.getChannelData(0),R=buf.getChannelData(1);
  for(let i=0;i<n;i++){L[i]=pcm[2*i]/32768;R[i]=pcm[2*i+1]/32768}
  const src=actx.createBufferSource();src.buffer=buf;src.connect(gain);
  const now=actx.currentTime;
  if(nextT<now+0.02)nextT=now+0.09;          // (re)start with a small jitter buffer
  else if(nextT-now>0.45)return;             // too far behind: drop this chunk to stay in sync
  src.start(nextT);nextT+=buf.duration;
  snd.classList.add('live');clearTimeout(aT);aT=setTimeout(()=>snd.classList.remove('live'),400);
}
snd.onclick=()=>{
  if(!actx){initAudio();return}
  muted=!muted;gain.gain.value=muted?0:1;snd.classList.toggle('muted',muted);
  if(!muted&&actx.state==='suspended')actx.resume();
};
addEventListener('pointerdown',e=>{if(!e.target.closest('#snd'))initAudio()});
addEventListener('keydown',()=>initAudio());

connect();
</script></body></html>
"""
