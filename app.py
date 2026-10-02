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

    async def _nav_changed(self) -> None:
        try:
            title = await self.page.title()
        except Exception:
            title = ""
        await self.send_json({"type": "nav", "url": self.page.url, "title": title})

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
            lambda f: asyncio.create_task(self._nav_changed()) if f == self.page.main_frame else None,
        )
        self.page.on("load", lambda _: asyncio.create_task(self._nav_changed()))
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

    async def handle(self, m: dict) -> None:
        t, p = m.get("type"), self.page
        try:
            if t == "goto":
                await self.goto(str(m.get("url", "")))
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
@app.get("/healthz")
async def healthz():
    browser: Browser = app.state.browser
    return JSONResponse({"ok": browser.is_connected(), "sessions": app.state.active})


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


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
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PyBrowser</title>
<style>
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:#1e1e22;font:14px system-ui,sans-serif;color:#eee}
#bar{display:flex;gap:6px;padding:6px;background:#2b2b31;height:44px}
#bar button{width:34px;border:0;border-radius:6px;background:#3a3a42;color:#eee;font-size:16px;cursor:pointer}
#bar button:hover{background:#4a4a54}
#url{flex:1;min-width:0;border:0;border-radius:6px;padding:0 12px;background:#18181b;color:#eee;font-size:14px}
#stage{position:absolute;top:44px;left:0;right:0;bottom:0;background:#fff}
#cv{width:100%;height:100%;display:block;outline:none}
#msg{position:absolute;left:50%;top:12px;transform:translateX(-50%);background:#000c;color:#fff;
     padding:8px 14px;border-radius:8px;cursor:pointer}
#msg[hidden]{display:none}
</style></head>
<body>
<div id="bar">
  <button id="back" title="Back">&#8592;</button>
  <button id="fwd" title="Forward">&#8594;</button>
  <button id="rel" title="Reload">&#8635;</button>
  <input id="url" placeholder="Search or enter address" autocomplete="off" spellcheck="false">
</div>
<div id="stage"><canvas id="cv" tabindex="0"></canvas><div id="msg" hidden></div></div>
<script>
const $=id=>document.getElementById(id);
const stage=$('stage'),cv=$('cv'),ctx=cv.getContext('2d'),url=$('url'),msg=$('msg');
let ws,last=0,canReconnect=false;
const size=()=>({w:Math.max(320,Math.min(1920,stage.clientWidth|0)),h:Math.max(240,Math.min(1080,stage.clientHeight|0))});
const send=o=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify(o))};
const note=(t,reconnect=false)=>{msg.textContent=t;msg.hidden=false;canReconnect=reconnect};

function connect(){
  const {w,h}=size();
  ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws?w='+w+'&h='+h);
  ws.onopen=()=>{msg.hidden=true;cv.focus()};
  ws.onmessage=async e=>{
    if(typeof e.data==='string'){
      const m=JSON.parse(e.data);
      if(m.type==='nav'){if(document.activeElement!==url)url.value=m.url;document.title=m.title||'PyBrowser'}
      else if(m.type==='error'){note(m.message)}
      return;
    }
    const bmp=await createImageBitmap(e.data);
    if(cv.width!==bmp.width||cv.height!==bmp.height){cv.width=bmp.width;cv.height=bmp.height}
    ctx.drawImage(bmp,0,0);bmp.close();
  };
  ws.onclose=()=>note('Disconnected \u2014 click to reconnect',true);
}
msg.onclick=()=>{if(canReconnect){msg.hidden=true;connect()}else msg.hidden=true};

$('back').onclick=()=>send({type:'back'});
$('fwd').onclick=()=>send({type:'forward'});
$('rel').onclick=()=>send({type:'reload'});
url.addEventListener('keydown',e=>{if(e.key==='Enter'){send({type:'goto',url:url.value});cv.focus()}});
url.addEventListener('focus',()=>url.select());

const pt=e=>{const r=cv.getBoundingClientRect();return{x:(e.clientX-r.left)*cv.width/r.width,y:(e.clientY-r.top)*cv.height/r.height}};
cv.addEventListener('mousemove',e=>{const n=performance.now();if(n-last<30)return;last=n;send({type:'mouse',action:'move',...pt(e)})});
cv.addEventListener('mousedown',e=>{cv.focus();send({type:'mouse',action:'down',button:e.button,clicks:e.detail||1,...pt(e)})});
cv.addEventListener('mouseup',e=>send({type:'mouse',action:'up',button:e.button,clicks:e.detail||1,...pt(e)}));
cv.addEventListener('wheel',e=>{e.preventDefault();send({type:'mouse',action:'wheel',dx:e.deltaX,dy:e.deltaY,...pt(e)})},{passive:false});
cv.addEventListener('contextmenu',e=>e.preventDefault());
const isPaste=e=>(e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='v';
cv.addEventListener('keydown',e=>{if(isPaste(e))return;e.preventDefault();send({type:'key',action:'down',key:e.key})});
cv.addEventListener('keyup',e=>{if(isPaste(e))return;e.preventDefault();send({type:'key',action:'up',key:e.key})});
cv.addEventListener('paste',e=>{e.preventDefault();const t=e.clipboardData.getData('text');if(t)send({type:'text',text:t})});

let rt;addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>{const {w,h}=size();send({type:'resize',w,h})},250)});
connect();
</script></body></html>
"""
