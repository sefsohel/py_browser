"""PyBrowser: A Chromium-based remote browser written in Python 3.11.

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
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import Browser, Page, async_playwright


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("pybrowser")

MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "2"))

HOME_URL = os.getenv(
    "HOME_URL",
    "https://duckduckgo.com"
)

JPEG_QUALITY = int(
    os.getenv("JPEG_QUALITY", "60")
)

IDLE_TIMEOUT = int(
    os.getenv("IDLE_TIMEOUT", "0")
)

HEARTBEAT_TIMEOUT = 75

MAX_W = int(
    os.getenv("MAX_W", "1280")
)

MAX_H = int(
    os.getenv("MAX_H", "720")
)

BROWSER_CHANNEL = os.getenv(
    "BROWSER_CHANNEL",
    "chrome"
)

PROXY_SERVER = os.getenv(
    "PROXY_SERVER",
    ""
)

AUDIO_ENABLED = os.getenv(
    "AUDIO",
    "1"
) != "0"

AUDIO_RATE = int(
    os.getenv("AUDIO_RATE", "32000")
)


# --------------------------------------------------------------------------- #
# Quality presets
# --------------------------------------------------------------------------- #

PRESETS = {
    "potato": (0.45, 35, 3),
    "smooth": (0.60, 45, 2),
    "balanced": (0.80, 55, 2),
    "sharp": (1.00, 70, 1),
}

LEVELS = list(PRESETS)

DEFAULT_QUALITY = os.getenv(
    "QUALITY",
    "balanced"
)

if DEFAULT_QUALITY not in PRESETS:
    DEFAULT_QUALITY = "balanced"


# Optional Playwright cookies
COOKIES_JSON = os.getenv(
    "COOKIES_JSON",
    ""
)


# --------------------------------------------------------------------------- #
# Chromium arguments
# --------------------------------------------------------------------------- #

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",

    "--disable-features=IsolateOrigins,site-per-process,Translate",

    "--renderer-process-limit=3",

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

        "ignore_default_args": [
            "--enable-automation",
            "--mute-audio",
        ],
    }

    if PROXY_SERVER:
        launch_kw["proxy"] = {
            "server": PROXY_SERVER
        }

    app.state.browser = await pw.chromium.launch(
        **launch_kw
    )

    app.state.sessions = []

    app.state.pw = pw

    app.state.launch_kw = launch_kw

    app.state.audio_clients = set()

    app.state.audio_on = False

    audio_task = (
        asyncio.create_task(
            audio_broadcaster(app)
        )
        if AUDIO_ENABLED
        else None
    )

    log.info(
        "Browser %s ready (channel=%s)",
        app.state.browser.version,
        BROWSER_CHANNEL or "chromium"
    )

    try:
        yield

    finally:

        if audio_task:
            audio_task.cancel()

        await app.state.browser.close()

        await pw.stop()


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


# --------------------------------------------------------------------------- #
# Audio broadcaster
# --------------------------------------------------------------------------- #

async def audio_broadcaster(
    app: FastAPI
) -> None:

    """Capture Chrome audio and fan it out to listeners."""

    quick_fails = 0

    while quick_fails < 5:

        started = asyncio.get_event_loop().time()

        proc = None

        try:

            proc = await asyncio.create_subprocess_exec(
                "parec",
                "-d",
                "pbsink.monitor",
                "--format=s16le",
                f"--rate={AUDIO_RATE}",
                "--channels=2",
                "--latency-msec=30",

                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            app.state.audio_on = True

            while True:

                chunk = await proc.stdout.read(6400)

                if not chunk:
                    break

                if chunk.count(0) == len(chunk):
                    continue

                for q in list(
                    app.state.audio_clients
                ):

                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass

                    try:
                        q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        pass

        except FileNotFoundError:

            log.warning(
                "parec not found: audio disabled"
            )

            break

        except asyncio.CancelledError:

            if proc and proc.returncode is None:
                proc.kill()

            raise

        except Exception:

            log.exception(
                "audio capture error"
            )

        app.state.audio_on = False

        elapsed = (
            asyncio.get_event_loop().time()
            - started
        )

        if elapsed < 5:
            quick_fails += 1
        else:
            quick_fails = 0

        await asyncio.sleep(2)

    log.warning(
        "Audio capture unavailable"
    )


# --------------------------------------------------------------------------- #
# Browser helper
# --------------------------------------------------------------------------- #

_launch_lock = asyncio.Lock()


async def ensure_browser() -> Browser:

    """Return a live browser."""

    async with _launch_lock:

        if not app.state.browser.is_connected():

            log.warning(
                "Browser process died: relaunching"
            )

            app.state.browser = (
                await app.state.pw.chromium.launch(
                    **app.state.launch_kw
                )
            )

        return app.state.browser


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #

def normalize_url(
    raw: str
) -> str | None:

    raw = raw.strip()

    if not raw:
        return None

    if raw == "about:blank":
        return raw

    if re.match(
        r"^[a-zA-Z][a-zA-Z0-9+.-]*://",
        raw
    ):

        if raw.lower().startswith(
            ("http://", "https://")
        ):
            return raw

        return None

    if (
        " " in raw
        or (
            "." not in raw
            and "localhost" not in raw
        )
    ):

        return (
            "https://duckduckgo.com/?q="
            + quote_plus(raw)
        )

    return "https://" + raw


def clamp(
    value: str | None,
    lo: int,
    hi: int,
    default: int
) -> int:

    try:

        return max(
            lo,
            min(
                hi,
                int(float(value))
            )
        )

    except (
        TypeError,
        ValueError
    ):

        return default


BUTTONS = {
    0: "left",
    1: "middle",
    2: "right",
}


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #

class Session:

    """One browser context + page streamed to one WebSocket client."""

    def __init__(
        self,
        ws: WebSocket,
        browser: Browser,
        w: int,
        h: int
    ):

        self.ws = ws

        self.browser = browser

        self.w = w
        self.h = h

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

        self.last_input = time.monotonic()

        self.level = DEFAULT_QUALITY

        self.auto = True

        self.drops = 0

        self._last_frame = time.monotonic()

        self._recovering = False

        self.last_url = ""

    # --------------------------------------------------------------------- #
    # Outgoing JSON
    # --------------------------------------------------------------------- #

    async def send_json(
        self,
        obj: dict
    ) -> None:

        async with self._lock:

            try:

                await self.ws.send_text(
                    json.dumps(
                        obj,
                        ensure_ascii=False
                    )
                )

            except Exception:
                pass

    # --------------------------------------------------------------------- #
    # CDP frame callback
    # --------------------------------------------------------------------- #

    async def _on_frame(
        self,
        params: dict
    ) -> None:

        try:

            data = base64.b64decode(
                params["data"]
            )

            if self._latest is not None:
                self.drops += 1

            self._latest = data

            self._last_frame = time.monotonic()

            self._frame_evt.set()

            await self.cdp.send(
                "Page.screencastFrameAck",
                {
                    "sessionId":
                        params["sessionId"]
                }
            )

        except Exception as exc:

            log.debug(
                "frame handling failed: %s",
                exc
            )

    # --------------------------------------------------------------------- #
    # Frame sender
    # --------------------------------------------------------------------- #

    async def _send_frames(
        self
    ) -> None:

        while True:

            await self._frame_evt.wait()

            self._frame_evt.clear()

            data = self._latest

            self._latest = None

            if data is None:
                continue

            try:

                async with self._lock:

                    await asyncio.wait_for(
                        self.ws.send_bytes(data),
                        10
                    )

            except Exception:

                try:
                    await self.ws.close(
                        code=1011
                    )
                except Exception:
                    pass

                return

    # --------------------------------------------------------------------- #
    # Evict session
    # --------------------------------------------------------------------- #

    async def evict(
        self
    ) -> None:

        await self.send_json({
            "type": "error",
            "message":
                "Session taken over by a new connection"
        })

        try:

            await self.ws.close(
                code=4000
            )

        except Exception:
            pass

    # --------------------------------------------------------------------- #
    # Quality
    # --------------------------------------------------------------------- #

    async def set_quality(
        self,
        level: str
    ) -> None:

        if level not in PRESETS:
            return

        self.level = level

        await self._stop_screencast()

        await self._start_screencast()

        await self.send_json({
            "type": "quality",
            "level": level,
            "auto": self.auto,
        })

    async def _restart_screencast(
        self
    ) -> None:

        await self._stop_screencast(
            detach=True
        )

        await self._start_screencast()

    # --------------------------------------------------------------------- #
    # Adaptive quality
    # --------------------------------------------------------------------- #

    async def _adapt(
        self
    ) -> None:

        while True:

            await asyncio.sleep(4)

            dropped = self.drops

            self.drops = 0

            i = LEVELS.index(
                self.level
            )

            if (
                self.auto
                and dropped >= 12
                and i > 0
                and not self._recovering
            ):

                try:

                    await asyncio.wait_for(
                        self.set_quality(
                            LEVELS[i - 1]
                        ),
                        10
                    )

                except Exception:

                    await self._restart_screencast()

                await asyncio.sleep(15)

    # --------------------------------------------------------------------- #
    # Watchdog
    # --------------------------------------------------------------------- #

    async def _watchdog(
        self
    ) -> None:

        bad = 0

        while True:

            await asyncio.sleep(5)

            if self._recovering:
                continue

            try:

                playing = await asyncio.wait_for(
                    self.page.evaluate(
                        """
                        [...document.querySelectorAll('video')]
                        .some(v =>
                            !v.paused &&
                            !v.ended &&
                            v.readyState > 2
                        )
                        """
                    ),
                    6
                )

                bad = 0

            except Exception:

                bad += 1

                if bad >= 2:

                    bad = 0

                    await self._recover(
                        "page unresponsive"
                    )

                continue

            if (
                playing
                and time.monotonic()
                - self._last_frame > 6
            ):

                log.warning(
                    "video playing but no frames for 6s"
                )

                try:

                    await asyncio.wait_for(
                        self._restart_screencast(),
                        10
                    )

                except Exception:

                    await self._recover(
                        "stream stalled"
                    )

    # --------------------------------------------------------------------- #
    # Recovery
    # --------------------------------------------------------------------- #

    async def _recover(
        self,
        reason: str
    ) -> None:

        if self._recovering:
            return

        self._recovering = True

        log.warning(
            "recovering session (%s)",
            reason
        )

        url = self.last_url

        try:

            await self.send_json({
                "type": "error",
                "message":
                    "The page stopped responding. Restarting it…"
            })

            self.cdp = None

            try:

                await asyncio.wait_for(
                    self.ctx.close(),
                    8
                )

            except Exception:
                pass

            self.browser = (
                await ensure_browser()
            )

            await self._open_context()

            await self._start_screencast()

            await self.send_json({
                "type": "viewport",
                "w": self.w,
                "h": self.h,
            })

            await self.goto(
                url
                if url and url != "about:blank"
                else HOME_URL
            )

        except Exception:

            log.exception(
                "recovery failed"
            )

        finally:

            self._recovering = False

    # --------------------------------------------------------------------- #
    # Input queue
    # --------------------------------------------------------------------- #

    def push(
        self,
        m: dict
    ) -> None:

        self._inbox.append(m)

        self._inbox_evt.set()

    async def _input_worker(
        self
    ) -> None:

        def kind(x):

            if x.get("type") == "mouse":
                return (
                    x.get("type"),
                    x.get("action")
                )

            return (
                x.get("type"),
                None
            )

        while True:

            await self._inbox_evt.wait()

            while self._inbox:

                m = self._inbox.popleft()

                k = kind(m)

                # Merge stale mouse moves
                if (
                    k == ("mouse", "move")
                    and self._inbox
                    and kind(
                        self._inbox[0]
                    ) == k
                ):
                    continue

                # Merge wheel events
                if k == ("mouse", "wheel"):

                    while (
                        self._inbox
                        and kind(
                            self._inbox[0]
                        ) == k
                    ):

                        n = self._inbox.popleft()

                        m["dx"] = (
                            float(
                                m.get(
                                    "dx",
                                    0
                                )
                            )
                            +
                            float(
                                n.get(
                                    "dx",
                                    0
                                )
                            )
                        )

                        m["dy"] = (
                            float(
                                m.get(
                                    "dy",
                                    0
                                )
                            )
                            +
                            float(
                                n.get(
                                    "dy",
                                    0
                                )
                            )
                        )

                        m["x"] = n["x"]
                        m["y"] = n["y"]

                await self.handle(m)

            self._inbox_evt.clear()

    # --------------------------------------------------------------------- #
    # Navigation
    # --------------------------------------------------------------------- #

    async def _nav_changed(
        self,
        loading: bool = False
    ) -> None:

        try:
            title = await self.page.title()
        except Exception:
            title = ""

        self.last_url = self.page.url

        await self.send_json({
            "type": "nav",
            "url": self.page.url,
            "title": title,
            "loading": loading,
        })

    # --------------------------------------------------------------------- #
    # Context
    # --------------------------------------------------------------------- #

    async def _open_context(
        self
    ) -> None:

        major = (
            self.browser.version
            .split(".")[0]
        )

        self.ctx = await self.browser.new_context(

            viewport={
                "width": self.w,
                "height": self.h
            },

            accept_downloads=False,

            user_agent=(
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 "
                "Safari/537.36"
            ),

            locale="en-US",
        )

        # Cookies
        if COOKIES_JSON:

            try:

                await self.ctx.add_cookies(
                    json.loads(
                        COOKIES_JSON
                    )
                )

            except Exception as exc:

                log.warning(
                    "COOKIES_JSON ignored: %s",
                    exc
                )

        # Hide webdriver
        await self.ctx.add_init_script(
            """
            Object.defineProperty(
                navigator,
                'webdriver',
                {
                    get: () => undefined
                }
            );
            """
        )

        self.ctx.on(
            "page",
            lambda p:
                asyncio.create_task(
                    self._on_new_page(p)
                )
        )

        self.page = await self.ctx.new_page()

        self.page.on(
            "framenavigated",
            lambda f:
                asyncio.create_task(
                    self._nav_changed(True)
                )
                if f == self.page.main_frame
                else None
        )

        self.page.on(
            "load",
            lambda _:
                asyncio.create_task(
                    self._nav_changed(False)
                )
        )

        self.page.on(
            "dialog",
            lambda d:
                asyncio.create_task(
                    d.dismiss()
                )
        )

        self.page.on(
            "crash",
            lambda _:
                asyncio.create_task(
                    self._recover(
                        "page crashed"
                    )
                )
        )

    # --------------------------------------------------------------------- #
    # Start
    # --------------------------------------------------------------------- #

    async def start(
        self
    ) -> None:

        await self._open_context()

        self._tasks = [
            asyncio.create_task(
                self._send_frames()
            ),

            asyncio.create_task(
                self._input_worker()
            ),

            asyncio.create_task(
                self._adapt()
            ),

            asyncio.create_task(
                self._watchdog()
            ),
        ]

        await self._start_screencast()

        await self.goto(
            HOME_URL
        )

    # --------------------------------------------------------------------- #
    # Screencast
    # --------------------------------------------------------------------- #

    async def _start_screencast(
        self
    ) -> None:

        scale, quality, nth = PRESETS[
            self.level
        ]

        if self.cdp is None:

            self.cdp = (
                await self.ctx.new_cdp_session(
                    self.page
                )
            )

            self.cdp.on(
                "Page.screencastFrame",
                self._on_frame
            )

        await self.cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",

                "quality": quality,

                "maxWidth": max(
                    160,
                    int(
                        self.w * scale
                    )
                ),

                "maxHeight": max(
                    120,
                    int(
                        self.h * scale
                    )
                ),

                "everyNthFrame": nth,
            }
        )

    async def _stop_screencast(
        self,
        detach: bool = False
    ) -> None:

        if not self.cdp:
            return

        try:

            await asyncio.wait_for(
                self.cdp.send(
                    "Page.stopScreencast"
                ),
                5
            )

        except Exception:
            pass

        if detach:

            try:

                await asyncio.wait_for(
                    self.cdp.detach(),
                    3
                )

            except Exception:
                pass

            self.cdp = None

    # --------------------------------------------------------------------- #
    # Popup
    # --------------------------------------------------------------------- #

    async def _on_new_page(
        self,
        popup: Page
    ) -> None:

        try:

            if await popup.opener() is None:
                return

            await popup.wait_for_load_state(
                "domcontentloaded",
                timeout=10000
            )

            url = popup.url

            await popup.close()

            if (
                url
                and url != "about:blank"
            ):
                await self.goto(url)

        except Exception:
            pass

    # --------------------------------------------------------------------- #
    # Close
    # --------------------------------------------------------------------- #

    async def close(
        self
    ) -> None:

        for task in self._tasks:
            task.cancel()

        await self._stop_screencast(
            detach=True
        )

        try:
            await self.ctx.close()
        except Exception:
            pass

    # --------------------------------------------------------------------- #
    # Navigation
    # --------------------------------------------------------------------- #

    async def goto(
        self,
        raw: str
    ) -> None:

        url = normalize_url(raw)

        if not url:

            await self.send_json({
                "type": "error",
                "message":
                    "Only http(s) URLs are allowed"
            })

            return

        try:

            await self.page.goto(
                url,
                wait_until="commit",
                timeout=30000
            )

        except Exception as exc:

            await self.send_json({
                "type": "error",
                "message":
                    str(exc)
                    .splitlines()[0][:200]
            })

            await self._nav_changed(False)

    # --------------------------------------------------------------------- #
    # Handle events
    # --------------------------------------------------------------------- #

    async def handle(
        self,
        m: dict
    ) -> None:

        t = m.get("type")

        p = self.page

        try:

            # ------------------------------------------------------------- #
            # Navigation
            # ------------------------------------------------------------- #

            if t == "goto":

                await self.goto(
                    str(
                        m.get(
                            "url",
                            ""
                        )
                    )
                )

            # ------------------------------------------------------------- #
            # Quality
            # ------------------------------------------------------------- #

            elif t == "quality":

                lvl = str(
                    m.get(
                        "level",
                        ""
                    )
                )

                if lvl == "auto":

                    self.auto = True

                    await self.send_json({
                        "type": "quality",
                        "level":
                            self.level,
                        "auto": True,
                    })

                elif lvl in PRESETS:

                    self.auto = False

                    await self.set_quality(
                        lvl
                    )

            # ------------------------------------------------------------- #
            # Home
            # ------------------------------------------------------------- #

            elif t == "home":

                await self.goto(
                    HOME_URL
                )

            # ------------------------------------------------------------- #
            # Back
            # ------------------------------------------------------------- #

            elif t == "back":

                await p.go_back(
                    wait_until="commit"
                )

            # ------------------------------------------------------------- #
            # Forward
            # ------------------------------------------------------------- #

            elif t == "forward":

                await p.go_forward(
                    wait_until="commit"
                )

            # ------------------------------------------------------------- #
            # Reload
            # ------------------------------------------------------------- #

            elif t == "reload":

                await p.reload(
                    wait_until="commit"
                )

            # ------------------------------------------------------------- #
            # Mouse
            # ------------------------------------------------------------- #

            elif t == "mouse":

                x = float(
                    m["x"]
                )

                y = float(
                    m["y"]
                )

                action = m.get(
                    "action"
                )

                if (
                    x,
                    y
                ) != self._pos:

                    await p.mouse.move(
                        x,
                        y
                    )

                    self._pos = (
                        x,
                        y
                    )

                if action == "down":

                    await p.mouse.down(
                        button=BUTTONS.get(
                            m.get(
                                "button",
                                0
                            ),
                            "left"
                        ),

                        click_count=int(
                            m.get(
                                "clicks",
                                1
                            )
                        )
                    )

                elif action == "up":

                    await p.mouse.up(
                        button=BUTTONS.get(
                            m.get(
                                "button",
                                0
                            ),
                            "left"
                        ),

                        click_count=int(
                            m.get(
                                "clicks",
                                1
                            )
                        )
                    )

                elif action == "wheel":

                    await p.mouse.wheel(
                        float(
                            m.get(
                                "dx",
                                0
                            )
                        ),

                        float(
                            m.get(
                                "dy",
                                0
                            )
                        )
                    )

            # ------------------------------------------------------------- #
            # Keyboard
            # ------------------------------------------------------------- #

            elif t == "key":

                key = m.get(
                    "key"
                )

                if (
                    isinstance(
                        key,
                        str
                    )
                    and key
                    and key not in (
                        "Dead",
                        "Unidentified",
                        "AltGraph"
                    )
                ):

                    if m.get(
                        "action"
                    ) == "down":

                        await p.keyboard.down(
                            key
                        )

                    else:

                        await p.keyboard.up(
                            key
                        )

            # ------------------------------------------------------------- #
            # Text
            # ------------------------------------------------------------- #

            elif t == "text":

                await p.keyboard.insert_text(
                    str(
                        m.get(
                            "text",
                            ""
                        )
                    )[:100_000]
                )

            # ------------------------------------------------------------- #
            # Resize
            # ------------------------------------------------------------- #

            elif t == "resize":

                self.w = clamp(
                    m.get("w"),
                    320,
                    MAX_W,
                    self.w
                )

                self.h = clamp(
                    m.get("h"),
                    240,
                    MAX_H,
                    self.h
                )

                await self._stop_screencast()

                await p.set_viewport_size({
                    "width": self.w,
                    "height": self.h
                })

                await self._start_screencast()

                await self.send_json({
                    "type": "viewport",
                    "w": self.w,
                    "h": self.h
                })

        except Exception as exc:

            log.debug(
                "event %s failed: %s",
                t,
                exc
            )


# --------------------------------------------------------------------------- #
# Allow iframe embedding
# --------------------------------------------------------------------------- #

@app.middleware("http")
async def allow_embedding(
    request,
    call_next
):

    resp = await call_next(
        request
    )

    for h in (
        "x-frame-options",
        "content-security-policy"
    ):

        if h in resp.headers:
            del resp.headers[h]

    return resp


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

@app.get(
    "/healthz"
)
async def healthz():

    browser: Browser = (
        app.state.browser
    )

    return JSONResponse({
        "ok":
            browser.is_connected(),

        "sessions":
            len(
                app.state.sessions
            ),
    })


# --------------------------------------------------------------------------- #
# Main HTML
# --------------------------------------------------------------------------- #

@app.get(
    "/",
    response_class=HTMLResponse
)
async def index():

    return INDEX_HTML


# --------------------------------------------------------------------------- #
# Audio WebSocket
# --------------------------------------------------------------------------- #

@app.websocket(
    "/ws/audio"
)
async def audio_ws(
    ws: WebSocket
):

    await ws.accept()

    if not app.state.audio_on:

        await ws.send_text(
            json.dumps({
                "type": "noaudio"
            })
        )

        await ws.close()

        return

    q: asyncio.Queue = (
        asyncio.Queue(
            maxsize=24
        )
    )

    app.state.audio_clients.add(
        q
    )

    await ws.send_text(
        json.dumps({
            "type": "format",
            "rate": AUDIO_RATE,
            "channels": 2,
        })
    )

    async def pump():

        while True:

            data = await q.get()

            await ws.send_bytes(
                data
            )

    pump_task = asyncio.create_task(
        pump()
    )

    try:

        while True:

            await ws.receive_text()

    except Exception:
        pass

    finally:

        pump_task.cancel()

        app.state.audio_clients.discard(
            q
        )


# --------------------------------------------------------------------------- #
# Browser WebSocket
# --------------------------------------------------------------------------- #

@app.websocket(
    "/ws"
)
async def ws_endpoint(
    ws: WebSocket
):

    log.info(
        "WebSocket connection: origin=%s user-agent=%s",
        ws.headers.get("origin"),
        ws.headers.get(
            "user-agent",
            ""
        )[:200]
    )

    await ws.accept()

    sessions = app.state.sessions

    # ------------------------------------------------------------- #
    # Free session slot
    # ------------------------------------------------------------- #

    while len(sessions) >= MAX_SESSIONS:

        victim = min(
            sessions,
            key=lambda x:
                x.last_input
        )

        sessions.remove(
            victim
        )

        await victim.evict()

    # ------------------------------------------------------------- #
    # Initial viewport
    # ------------------------------------------------------------- #

    w = clamp(
        ws.query_params.get("w"),
        320,
        MAX_W,
        1280
    )

    h = clamp(
        ws.query_params.get("h"),
        240,
        MAX_H,
        720
    )

    # ------------------------------------------------------------- #
    # Session
    # ------------------------------------------------------------- #

    sess = Session(
        ws,
        await ensure_browser(),
        w,
        h
    )

    sessions.append(
        sess
    )

    try:

        await sess.start()

        await sess.send_json({
            "type": "viewport",
            "w": sess.w,
            "h": sess.h,
        })

        # --------------------------------------------------------- #
        # Receive client messages
        # --------------------------------------------------------- #

        while True:

            try:

                raw = await asyncio.wait_for(
                    ws.receive_text(),
                    HEARTBEAT_TIMEOUT
                )

            except asyncio.TimeoutError:

                log.info(
                    "WebSocket heartbeat timeout"
                )

                break

            try:

                msg = json.loads(
                    raw
                )

            except json.JSONDecodeError:

                continue

            if not isinstance(
                msg,
                dict
            ):

                continue

            # ----------------------------------------------------- #
            # Ping
            # ----------------------------------------------------- #

            if msg.get(
                "type"
            ) == "ping":

                await sess.send_json({
                    "type": "pong"
                })

                if (
                    IDLE_TIMEOUT
                    and
                    time.monotonic()
                    - sess.last_input
                    > IDLE_TIMEOUT
                ):

                    await sess.send_json({
                        "type": "error",
                        "message":
                            "Closed after inactivity"
                    })

                    break

                continue

            # ----------------------------------------------------- #
            # Normal event
            # ----------------------------------------------------- #

            sess.last_input = (
                time.monotonic()
            )

            sess.push(
                msg
            )

    except WebSocketDisconnect:

        log.info(
            "WebSocket disconnected"
        )

    except RuntimeError as exc:

        log.debug(
            "websocket closed: %s",
            exc
        )

    except Exception:

        log.exception(
            "session crashed"
        )

    finally:

        if sess in sessions:
            sessions.remove(
                sess
            )

        await sess.close()

        try:
            await ws.close()
        except Exception:
            pass


# ========================================================================= #
# FRONTEND
# ========================================================================= #

INDEX_HTML = r"""<!doctype html>

<html
    lang="en"
    data-theme="dark"
>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"
>

<title>PyBrowser</title>

<link
    rel="preconnect"
    href="https://fonts.googleapis.com"
>

<link
    rel="preconnect"
    href="https://fonts.gstatic.com"
    crossorigin
>

<link
    href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Newsreader:ital,opsz,wght@0,6..72,400..600;1,6..72,400..500&display=swap"
    rel="stylesheet"
>

<link
    rel="icon"
    href="data:image/svg+xml,%3Csvg%20viewBox%3D%220%200%2096%2096%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Cdefs%3E%3ClinearGradient%20id%3D%22lgf%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%221%22%20y2%3D%221%22%3E%3Cstop%20offset%3D%220%22%20stop-color%3D%22%23f7e39a%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%23b8860b%22%2F%3E%3C%2FlinearGradient%3E%3C/defs%3E%3Crect%20width%3D%2296%22%20height%3D%2296%22%20rx%3D%2226%22%20fill%3D%22url%28%23lgf%29%22/%3E%3C/svg%3E"
>

<style>

:root{
    --bg:#0a0907;
    --bar:#14110ce6;
    --chip:#1d1912;
    --chip2:#2c2518;

    --fg:#f4ecd8;
    --mut:#9a8f76;

    --acc:#d4af37;
    --acc2:#f3d98b;

    --ok:#4ade80;
    --bad:#f87171;
    --warn:#fbbf24;

    --sh:0 10px 34px #000a;
}

:root[data-theme=light]{
    --bg:#f6f0e1;
    --bar:#fffaf0e6;
    --chip:#ede4cd;
    --chip2:#e0d4b5;

    --fg:#1d1710;
    --mut:#7a6d52;

    --sh:0 10px 34px #0002;
}

*{
    box-sizing:border-box;
}

html,
body{
    width:100%;
    height:100%;
    margin:0;

    background:var(--bg);
    color:var(--fg);

    font:
        14px/1.45
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    overflow:hidden;

    overscroll-behavior:none;
}

body{
    touch-action:none;
}

/* ---------------------------------------------------------- */
/* Toolbar */
/* ---------------------------------------------------------- */

#bar{
    position:absolute;

    top:0;
    left:0;
    right:0;

    height:56px;

    display:flex;
    align-items:center;

    gap:6px;

    padding:0 10px;

    background:var(--bar);

    backdrop-filter:blur(14px);
    -webkit-backdrop-filter:blur(14px);

    border-bottom:
        1px solid
        #d4af3730;

    z-index:5;

    animation:
        down
        .5s
        cubic-bezier(.2,.9,.3,1)
        both;
}

@keyframes down{

    from{
        transform:translateY(-100%);
        opacity:0;
    }

}

/* ---------------------------------------------------------- */
/* Buttons */
/* ---------------------------------------------------------- */

.ib{

    width:36px;
    height:36px;

    border:0;

    border-radius:50%;

    background:transparent;

    color:var(--fg);

    display:grid;
    place-items:center;

    cursor:pointer;

    position:relative;

    overflow:hidden;

    transition:
        background .2s,
        transform .15s;

    -webkit-tap-highlight-color:transparent;

    touch-action:manipulation;
}

.ib:hover{
    background:var(--chip2);
}

.ib:active{
    transform:scale(.88);
}

.ib svg{

    width:19px;
    height:19px;

    fill:none;

    stroke:currentColor;

    stroke-width:2;

    stroke-linecap:round;
    stroke-linejoin:round;
}

.spin svg{

    animation:
        rot
        .8s
        linear
        infinite;

    color:var(--acc);
}

@keyframes rot{

    to{
        transform:rotate(360deg);
    }

}

/* ---------------------------------------------------------- */
/* Address */
/* ---------------------------------------------------------- */

#pill{

    flex:1;

    min-width:0;

    height:38px;

    display:flex;

    align-items:center;

    gap:8px;

    padding:
        0
        6px
        0
        12px;

    border-radius:19px;

    background:var(--chip);

    border:
        1.5px solid
        transparent;

    transition:
        border-color .25s,
        box-shadow .25s,
        background .25s;
}

#pill:focus-within{

    border-color:var(--acc);

    box-shadow:
        0 0 0 4px
        #d4af3730;

    background:var(--bg);
}

#lock{

    width:16px;
    height:16px;

    flex:none;

    color:var(--acc);
}

#lock[data-s="0"]{
    color:var(--bad);
}

#lock svg{

    width:16px;
    height:16px;

    fill:none;

    stroke:currentColor;

    stroke-width:2;

    stroke-linecap:round;
    stroke-linejoin:round;
}

#url{

    flex:1;

    min-width:0;

    height:100%;

    border:0;

    outline:0;

    background:transparent;

    color:var(--fg);

    font:inherit;

    font-size:14px;
}

#url::placeholder{
    color:var(--mut);
}

/* ---------------------------------------------------------- */
/* Connection */
/* ---------------------------------------------------------- */

#dot{

    width:9px;
    height:9px;

    border-radius:50%;

    margin:
        0
        6px;

    background:var(--warn);

    flex:none;

    transition:
        background .3s;
}

#dot.on{

    background:var(--ok);

    animation:
        pulse
        2.4s
        infinite;
}

#dot.off{
    background:var(--bad);
}

@keyframes pulse{

    0%{
        box-shadow:
            0 0 0 0
            #4ade8088;
    }

    70%,
    100%{
        box-shadow:
            0 0 0 8px
            #4ade8000;
    }

}

/* ---------------------------------------------------------- */
/* Progress */
/* ---------------------------------------------------------- */

#prog{

    position:absolute;

    left:0;

    bottom:-1px;

    height:3px;

    width:0;

    opacity:0;

    background:
        linear-gradient(
            90deg,
            var(--acc),
            var(--acc2)
        );

    border-radius:
        0
        3px
        3px
        0;

    box-shadow:
        0 0 10px
        var(--acc);
}

/* ---------------------------------------------------------- */
/* Stage */
/* ---------------------------------------------------------- */

#stage{

    position:absolute;

    top:56px;
    left:0;
    right:0;
    bottom:0;

    background:#000;

    overflow:hidden;

    touch-action:none;

    user-select:none;
    -webkit-user-select:none;
}

#cv{

    width:100%;
    height:100%;

    display:block;

    outline:none;

    opacity:0;

    transition:
        opacity .35s;

    /*
     * Critical for mobile browsers.
     */
    touch-action:none;

    user-select:none;
    -webkit-user-select:none;

    -webkit-touch-callout:none;

    image-rendering:auto;

    background:#000;
}

#cv.show{
    opacity:1;
}

.loading #cv{
    filter:brightness(.96);
}

/* ---------------------------------------------------------- */
/* Ripple */
/* ---------------------------------------------------------- */

.rip{

    position:absolute;

    width:16px;
    height:16px;

    margin:
        -8px
        0
        0
        -8px;

    border-radius:50%;

    border:
        2px solid
        var(--acc);

    background:
        #d4af3740;

    pointer-events:none;

    animation:
        rip
        .55s
        ease-out
        forwards;
}

@keyframes rip{

    to{
        transform:scale(4);
        opacity:0;
    }

}

/* ---------------------------------------------------------- */
/* Splash */
/* ---------------------------------------------------------- */

#splash,
#over{

    position:absolute;

    inset:
        56px
        0
        0
        0;

    display:grid;

    place-items:center;

    text-align:center;

    z-index:4;

    background:var(--bg);

    transition:
        opacity .5s,
        visibility .5s;

    overflow:hidden;
}

#splash.hide,
#over.hide{

    opacity:0;

    visibility:hidden;
}

.blob{

    position:absolute;

    width:380px;
    height:380px;

    border-radius:50%;

    filter:blur(70px);

    opacity:.2;

    animation:
        float
        9s
        ease-in-out
        infinite
        alternate;
}

.b1{

    background:var(--acc);

    left:12%;
    top:8%;
}

.b2{

    background:var(--acc2);

    right:10%;
    bottom:6%;

    animation-delay:-4s;
}

@keyframes float{

    to{
        transform:
            translate(
                60px,
                -40px
            )
            scale(1.2);
    }

}

/* ---------------------------------------------------------- */
/* Splash Card */
/* ---------------------------------------------------------- */

.card{

    position:relative;

    animation:
        rise
        .7s
        cubic-bezier(.2,.9,.3,1)
        both;
}

@keyframes rise{

    from{
        transform:translateY(24px);
        opacity:0;
    }

}

.logo{

    display:block;

    width:84px;
    height:84px;

    margin:
        0
        auto
        18px;

    filter:
        drop-shadow(
            0 10px 22px
            #d4af3755
        );

    animation:
        bob
        2.2s
        ease-in-out
        infinite;
}

@keyframes bob{

    50%{
        transform:translateY(-8px);
    }

}

.ring{

    width:26px;
    height:26px;

    margin:
        16px
        auto
        0;

    border-radius:50%;

    border:
        3px solid
        var(--chip2);

    border-top-color:
        var(--acc);

    animation:
        rot
        .8s
        linear
        infinite;
}

.card h1{

    margin:
        0
        0
        6px;

    font:
        500
        30px/1.15
        Newsreader,
        Georgia,
        serif;
}

.card p{

    margin:0;

    color:var(--mut);

    font:
        italic
        400
        19px/1.4
        Newsreader,
        Georgia,
        serif;
}

.btn{

    margin-top:18px;

    border:0;

    border-radius:22px;

    padding:
        10px
        22px;

    font:inherit;

    font-weight:600;

    color:#14110a;

    cursor:pointer;

    background:
        linear-gradient(
            135deg,
            var(--acc2),
            var(--acc)
        );
}

/* ---------------------------------------------------------- */
/* Toast */
/* ---------------------------------------------------------- */

#toasts{

    position:absolute;

    right:14px;
    bottom:14px;

    display:flex;

    flex-direction:column;

    gap:8px;

    z-index:9;

    pointer-events:none;
}

.toast{

    background:var(--bar);

    backdrop-filter:blur(12px);
    -webkit-backdrop-filter:blur(12px);

    border:
        1px solid
        #ffffff1a;

    border-left:
        4px solid
        var(--bad);

    color:var(--fg);

    padding:
        10px
        14px;

    border-radius:10px;

    box-shadow:var(--sh);

    max-width:340px;

    animation:
        toastIn
        .35s
        cubic-bezier(.2,.9,.3,1)
        both;
}

.toast.out{

    animation:
        toastOut
        .3s
        forwards;
}

@keyframes toastIn{

    from{
        transform:translateX(120%);
        opacity:0;
    }

}

@keyframes toastOut{

    to{
        transform:translateX(120%);
        opacity:0;
    }

}

/* ---------------------------------------------------------- */
/* Sound */
/* ---------------------------------------------------------- */

#snd .w{
    opacity:.55;
}

#snd.live .w{

    opacity:1;

    animation:
        wave
        .9s
        ease-in-out
        infinite;
}

#snd.muted .w{
    display:none;
}

#snd.off{
    opacity:.4;
}

@keyframes wave{

    50%{
        opacity:.3;
    }

}

/* ---------------------------------------------------------- */
/* Quality */
/* ---------------------------------------------------------- */

#qual{

    width:auto;

    min-width:46px;

    padding:
        0
        10px;

    border-radius:18px;

    font:
        600
        12px/1
        Inter,
        system-ui,
        sans-serif;

    color:var(--acc);
}

/* ---------------------------------------------------------- */
/* Mobile */
/* ---------------------------------------------------------- */

@media (max-width:560px){

    #bar{
        height:52px;
        padding:0 6px;
        gap:3px;
    }

    #stage{
        top:52px;
    }

    #splash,
    #over{
        inset:52px 0 0 0;
    }

    #bar .opt{
        display:none;
    }

    #pill{
        height:36px;
    }

    #url{
        font-size:13px;
    }

}

/* ---------------------------------------------------------- */
/* Reduced motion */
/* ---------------------------------------------------------- */

@media (
    prefers-reduced-motion:reduce
){

    *{

        animation-duration:
            .01s !important;

        transition-duration:
            .01s !important;
    }

}

</style>

</head>

<body>

<!-- ======================================================== -->
<!-- TOOLBAR -->
<!-- ======================================================== -->

<div id="bar">

    <button
        class="ib"
        id="back"
        title="Back"
    >
        <svg viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7"/>
        </svg>
    </button>

    <button
        class="ib opt"
        id="fwd"
        title="Forward"
    >
        <svg viewBox="0 0 24 24">
            <path d="M5 12h14M12 5l7 7-7 7"/>
        </svg>
    </button>

    <button
        class="ib"
        id="rel"
        title="Reload"
    >
        <svg viewBox="0 0 24 24">
            <path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/>
        </svg>
    </button>

    <button
        class="ib opt"
        id="home"
        title="Home"
    >
        <svg viewBox="0 0 24 24">
            <path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/>
        </svg>
    </button>

    <div id="pill">

        <span
            id="lock"
            data-s="1"
        >
            <svg viewBox="0 0 24 24">
                <rect
                    x="4"
                    y="11"
                    width="16"
                    height="10"
                    rx="2"
                />
                <path
                    d="M8 11V7a4 4 0 0 1 8 0v4"
                />
            </svg>
        </span>

        <input
            id="url"
            placeholder="Search or enter address"
            autocomplete="off"
            spellcheck="false"
        >

    </div>

    <span
        id="dot"
        title="Connection"
    ></span>

    <button
        class="ib"
        id="snd"
        title="Sound"
    >
        <svg viewBox="0 0 24 24">
            <path
                d="M11 5L6 9H2v6h4l5 4V5z"
            />
            <path
                class="w"
                d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"
            />
        </svg>
    </button>

    <button
        class="ib"
        id="qual"
        title="Quality"
    >
        Auto
    </button>

    <button
        class="ib opt"
        id="theme"
        title="Toggle theme"
    >
        <svg viewBox="0 0 24 24">
            <path
                d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"
            />
        </svg>
    </button>

    <button
        class="ib opt"
        id="full"
        title="Fullscreen"
    >
        <svg viewBox="0 0 24 24">
            <path
                d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"
            />
        </svg>
    </button>

    <div id="prog"></div>

</div>


<!-- ======================================================== -->
<!-- BROWSER CANVAS -->
<!-- ======================================================== -->

<div id="stage">

    <canvas
        id="cv"
        tabindex="0"
    ></canvas>

</div>


<!-- ======================================================== -->
<!-- SPLASH -->
<!-- ======================================================== -->

<div id="splash">

    <div class="blob b1"></div>

    <div class="blob b2"></div>

    <div class="card">

        <svg
            class="logo"
            role="img"
            aria-label="PyBrowser"
            viewBox="0 0 96 96"
            xmlns="http://www.w3.org/2000/svg"
        >

            <defs>

                <linearGradient
                    id="lg1"
                    x1="0"
                    y1="0"
                    x2="1"
                    y2="1"
                >

                    <stop
                        offset="0"
                        stop-color="#f7e39a"
                    />

                    <stop
                        offset="1"
                        stop-color="#b8860b"
                    />

                </linearGradient>

            </defs>

            <rect
                width="96"
                height="96"
                rx="26"
                fill="url(#lg1)"
            />

            <circle
                cx="48"
                cy="48"
                r="26"
                fill="none"
                stroke="#14110a"
                stroke-width="4"
            />

            <ellipse
                cx="48"
                cy="48"
                rx="11"
                ry="26"
                fill="none"
                stroke="#14110a"
                stroke-width="4"
                opacity=".9"
            />

            <path
                d="M22 48h52M27 34h42M27 62h42"
                stroke="#14110a"
                stroke-width="3.5"
                stroke-linecap="round"
                fill="none"
                opacity=".9"
            />

            <path
                d="M58 56l22 8-9 3-3 9z"
                fill="#14110a"
                stroke="#f7e39a"
                stroke-width="2.5"
                stroke-linejoin="round"
            />

        </svg>

        <p id="splashTxt">
            Starting your private browser…
        </p>

        <div class="ring"></div>

    </div>

</div>


<!-- ======================================================== -->
<!-- DISCONNECTED -->
<!-- ======================================================== -->

<div
    id="over"
    class="hide"
>

    <div class="blob b1"></div>

    <div class="blob b2"></div>

    <div class="card">

        <svg
            class="logo"
            role="img"
            aria-label="PyBrowser"
            viewBox="0 0 96 96"
            xmlns="http://www.w3.org/2000/svg"
        >

            <defs>

                <linearGradient
                    id="lg2"
                    x1="0"
                    y1="0"
                    x2="1"
                    y2="1"
                >

                    <stop
                        offset="0"
                        stop-color="#f7e39a"
                    />

                    <stop
                        offset="1"
                        stop-color="#b8860b"
                    />

                </linearGradient>

            </defs>

            <rect
                width="96"
                height="96"
                rx="26"
                fill="url(#lg2)"
            />

            <circle
                cx="48"
                cy="48"
                r="26"
                fill="none"
                stroke="#14110a"
                stroke-width="4"
            />

            <ellipse
                cx="48"
                cy="48"
                rx="11"
                ry="26"
                fill="none"
                stroke="#14110a"
                stroke-width="4"
                opacity=".9"
            />

            <path
                d="M22 48h52M27 34h42M27 62h42"
                stroke="#14110a"
                stroke-width="3.5"
                stroke-linecap="round"
                fill="none"
                opacity=".9"
            />

            <path
                d="M58 56l22 8-9 3-3 9z"
                fill="#14110a"
                stroke="#f7e39a"
                stroke-width="2.5"
                stroke-linejoin="round"
            />

        </svg>

        <h1 id="overH">
            Disconnected
        </h1>

        <p id="overP">
            The session ended.
        </p>

        <button
            class="btn"
            id="reco"
        >
            Reconnect
        </button>

    </div>

</div>


<div id="toasts"></div>


<script>

/* ========================================================= */
/* BASIC HELPERS */
/* ========================================================= */

const $ = id =>
    document.getElementById(id);


const stage = $('stage');

const cv = $('cv');

const ctx = cv.getContext(
    '2d',
    {
        alpha:false,
        desynchronized:true
    }
);

const url = $('url');

const prog = $('prog');

const dot = $('dot');

const lock = $('lock');

const rel = $('rel');

const splash = $('splash');

const over = $('over');

const root = document.documentElement;


/* ========================================================= */
/* STATE */
/* ========================================================= */

let pingAt = 0;

let vw = 1280;

let vh = 720;

let hb = null;

let tries = 0;

let ws = null;

let last = 0;

let lastErr = '';

let loadT = null;

let opened = false;


/* ========================================================= */
/* SIZE */
/* ========================================================= */

const size = () => {

    return {

        w: Math.max(
            320,
            Math.min(
                1920,
                stage.clientWidth | 0
            )
        ),

        h: Math.max(
            240,
            Math.min(
                1080,
                stage.clientHeight | 0
            )
        )

    };

};


/* ========================================================= */
/* SEND */
/* ========================================================= */

const send = obj => {

    if (
        ws &&
        ws.readyState === WebSocket.OPEN
    ){

        try{
            ws.send(
                JSON.stringify(obj)
            );
        }catch(e){
            console.warn(
                'WebSocket send failed',
                e
            );
        }

    }

};


/* ========================================================= */
/* TOAST */
/* ========================================================= */

function toast(t){

    const d =
        document.createElement(
            'div'
        );

    d.className='toast';

    d.textContent=t;

    $('toasts').append(d);

    setTimeout(
        () => {

            d.classList.add(
                'out'
            );

            setTimeout(
                () => d.remove(),
                300
            );

        },
        4200
    );

}


/* ========================================================= */
/* LOADING */
/* ========================================================= */

function setLoading(on){

    clearTimeout(
        loadT
    );

    root.classList.toggle(
        'loading',
        on
    );

    rel.classList.toggle(
        'spin',
        on
    );

    if(on){

        prog.style.transition =
            'none';

        prog.style.opacity =
            1;

        prog.style.width =
            '0%';

        void prog.offsetWidth;

        prog.style.transition =
            'width 8s cubic-bezier(.1,.8,.2,1)';

        prog.style.width =
            '86%';

        loadT =
            setTimeout(
                () => setLoading(false),
                30000
            );

    }else{

        prog.style.transition =
            'width .25s';

        prog.style.width =
            '100%';

        setTimeout(
            () => {

                prog.style.transition =
                    'opacity .35s';

                prog.style.opacity =
                    0;

            },
            260
        );

    }

}


/* ========================================================= */
/* IMAGE STREAMING */
/* ========================================================= */

/*
 * IMPORTANT:
 *
 * Mobile compatible pipeline:
 *
 * WebSocket binary
 *       ↓
 * Blob
 *       ↓
 * Object URL
 *       ↓
 * HTMLImageElement
 *       ↓
 * Canvas
 *
 * This avoids relying on createImageBitmap()
 * which can behave differently between mobile
 * browsers and desktop browsers.
 */

let pend = null;

let drawing = false;


function drawLatest(){

    if(
        drawing ||
        !pend
    ){
        return;
    }

    drawing = true;

    const blob = pend;

    pend = null;


    /*
     * Make sure the WebSocket frame
     * is actually a Blob.
     */

    let frameBlob;

    if(
        blob instanceof Blob
    ){

        frameBlob = blob;

    }else if(
        blob instanceof ArrayBuffer
    ){

        frameBlob = new Blob(
            [blob],
            {
                type:'image/jpeg'
            }
        );

    }else{

        console.warn(
            'Unknown binary frame type:',
            typeof blob
        );

        drawing = false;

        return;
    }


    const objectUrl =
        URL.createObjectURL(
            frameBlob
        );


    const img =
        new Image();


    img.onload = () => {

        try{

            /*
             * Keep canvas equal to the
             * actual JPEG dimensions.
             */

            if(
                cv.width !==
                    img.naturalWidth
                ||
                cv.height !==
                    img.naturalHeight
            ){

                cv.width =
                    img.naturalWidth;

                cv.height =
                    img.naturalHeight;

            }


            ctx.drawImage(
                img,
                0,
                0,
                img.naturalWidth,
                img.naturalHeight
            );


            /*
             * First successfully decoded
             * frame = browser ready.
             */

            if(
                !cv.classList.contains(
                    'show'
                )
            ){

                cv.classList.add(
                    'show'
                );

                splash.classList.add(
                    'hide'
                );

            }

        }catch(err){

            console.warn(
                'Canvas rendering error:',
                err
            );

        }finally{

            /*
             * Very important on mobile.
             *
             * Prevent Blob URL memory leak.
             */

            URL.revokeObjectURL(
                objectUrl
            );

            drawing = false;


            /*
             * If another newer frame arrived
             * while decoding, render it.
             */

            if(pend){

                requestAnimationFrame(
                    drawLatest
                );

            }

        }

    };


    img.onerror = () => {

        URL.revokeObjectURL(
            objectUrl
        );

        console.warn(
            'JPEG frame could not be decoded'
        );

        drawing = false;

        if(pend){

            requestAnimationFrame(
                drawLatest
            );

        }

    };


    img.src = objectUrl;

}


/* ========================================================= */
/* CONNECT WEBSOCKET */
/* ========================================================= */

function connect(){

    lastErr='';

    opened=false;

    dot.className='';

    over.classList.add(
        'hide'
    );

    splash.classList.remove(
        'hide'
    );

    $('splashTxt').textContent =
        'Starting your private browser…';


    /*
     * Clear old pending frame.
     */

    pend = null;

    drawing = false;


    const {
        w,
        h
    } = size();

    vw=w;
    vh=h;


    /*
     * HTTPS page → WSS
     *
     * HTTP page → WS
     */

    const protocol =
        location.protocol === 'https:'
            ? 'wss:'
            : 'ws:';


    const wsUrl =
        protocol +
        '//' +
        location.host +
        '/ws?w=' +
        encodeURIComponent(w) +
        '&h=' +
        encodeURIComponent(h);


    console.log(
        'Connecting WebSocket:',
        wsUrl
    );


    ws =
        new WebSocket(
            wsUrl
        );


    /*
     * CRITICAL FOR BINARY JPEG
     * STREAMING ON MOBILE.
     */

    ws.binaryType =
        'blob';


    /* ------------------------------------------------------- */
    /* OPEN */
    /* ------------------------------------------------------- */

    ws.onopen = () => {

        console.log(
            'WebSocket connected'
        );

        opened=true;

        tries=0;

        dot.className='on';

        cv.focus();

        clearInterval(
            hb
        );

        pingAt=0;


        hb =
            setInterval(
                () => {

                    if(
                        !ws ||
                        ws.readyState !==
                            WebSocket.OPEN
                    ){
                        return;
                    }


                    /*
                     * Server stopped
                     * answering.
                     */

                    if(
                        pingAt &&
                        performance.now()
                        -
                        pingAt
                        >
                        25000
                    ){

                        console.warn(
                            'WebSocket heartbeat timeout'
                        );

                        ws.close();

                        return;
                    }


                    if(!pingAt){

                        pingAt =
                            performance.now();

                        send({
                            type:'ping'
                        });

                    }

                },
                10000
            );

    };


    /* ------------------------------------------------------- */
    /* MESSAGE */
    /* ------------------------------------------------------- */

    ws.onmessage = e => {

        /*
         * TEXT MESSAGE
         */

        if(
            typeof e.data ===
            'string'
        ){

            let m;

            try{

                m =
                    JSON.parse(
                        e.data
                    );

            }catch(err){

                console.warn(
                    'Invalid WebSocket JSON:',
                    err
                );

                return;
            }


            /* NAVIGATION */

            if(
                m.type ===
                'nav'
            ){

                if(
                    document.activeElement
                    !== url
                ){

                    url.value =
                        m.url ===
                        'about:blank'
                            ? ''
                            : m.url;

                }


                lock.dataset.s =
                    m.url.startsWith(
                        'https:'
                    )
                        ? '1'
                        : '0';


                document.title =
                    m.title
                        ? m.title +
                          ' – PyBrowser'
                        : 'PyBrowser';


                setLoading(
                    !!m.loading
                );

            }


            /* VIEWPORT */

            else if(
                m.type ===
                'viewport'
            ){

                vw =
                    Number(
                        m.w
                    ) || vw;

                vh =
                    Number(
                        m.h
                    ) || vh;

            }


            /* PONG */

            else if(
                m.type ===
                'pong'
            ){

                pingAt=0;

            }


            /* QUALITY */

            else if(
                m.type ===
                'quality'
            ){

                $('qual').textContent =
                    m.auto
                        ? 'Auto'
                        : (
                            QN[m.level]
                            || m.level
                        );

                $('qual').title =
                    'Quality: ' +
                    (
                        m.auto
                            ? 'Auto (now ' +
                              (
                                QN[m.level]
                                ||
                                m.level
                              ) +
                              ')'
                            :
                              (
                                QN[m.level]
                                ||
                                m.level
                              )
                    );

            }


            /* ERROR */

            else if(
                m.type ===
                'error'
            ){

                lastErr =
                    m.message || '';

                toast(
                    lastErr
                );

            }


            return;
        }


        /*
         * BINARY JPEG FRAME
         */

        if(
            e.data instanceof Blob
        ){

            pend =
                e.data;

        }else if(
            e.data instanceof ArrayBuffer
        ){

            pend =
                new Blob(
                    [e.data],
                    {
                        type:
                            'image/jpeg'
                    }
                );

        }else{

            console.warn(
                'Unknown WebSocket frame:',
                e.data
            );

            return;
        }


        /*
         * Render latest frame.
         */

        drawLatest();

    };


    /* ------------------------------------------------------- */
    /* ERROR */
    /* ------------------------------------------------------- */

    ws.onerror = e => {

        console.warn(
            'PyBrowser WebSocket error',
            e
        );

        lastErr =
            'WebSocket connection error';

    };


    /* ------------------------------------------------------- */
    /* CLOSE */
    /* ------------------------------------------------------- */

    ws.onclose = e => {

        console.warn(
            'WebSocket closed:',
            e.code,
            e.reason
        );

        clearInterval(
            hb
        );

        dot.className='off';

        cv.classList.remove(
            'show'
        );

        setLoading(false);


        if(
            /taken over/i.test(
                lastErr
            )
        ){

            $('overH').textContent =
                'Opened in another tab';

        }else if(
            /busy/i.test(
                lastErr
            )
        ){

            $('overH').textContent =
                'Server is busy';

        }else{

            $('overH').textContent =
                'Disconnected';

        }


        $('overP').textContent =
            lastErr ||
            (
                e.reason ||
                'The session ended.'
            );


        splash.classList.add(
            'hide'
        );

        over.classList.remove(
            'hide'
        );


        /*
         * Automatic reconnect.
         */

        if(
            !/taken over/i.test(
                lastErr
            )
            &&
            tries < 4
        ){

            tries++;


            setTimeout(
                () => {

                    if(
                        !ws ||
                        ws.readyState >
                            WebSocket.CLOSING
                    ){

                        connect();

                    }

                },
                1500 * tries
            );

        }

    };

}


/* ========================================================= */
/* RECONNECT */
/* ========================================================= */

$('reco').onclick =
    connect;


/* ========================================================= */
/* NAVIGATION BUTTONS */
/* ========================================================= */

$('back').onclick =
    () => send({
        type:'back'
    });


$('fwd').onclick =
    () => send({
        type:'forward'
    });


rel.onclick =
    () => send({
        type:'reload'
    });


$('home').onclick =
    () => send({
        type:'home'
    });


/* ========================================================= */
/* FULLSCREEN */
/* ========================================================= */

$('full').onclick =
    () => {

        if(
            document.fullscreenElement
        ){

            document.exitFullscreen();

        }else{

            root.requestFullscreen()
                .catch(
                    () => {}
                );

        }

    };


/* ========================================================= */
/* THEME */
/* ========================================================= */

$('theme').onclick =
    () => {

        const t =
            root.dataset.theme ===
            'dark'
                ? 'light'
                : 'dark';

        root.dataset.theme =
            t;

        try{

            localStorage.setItem(
                'pb-theme',
                t
            );

        }catch(e){}

    };


try{

    const t =
        localStorage.getItem(
            'pb-theme'
        );

    if(t){

        root.dataset.theme =
            t;

    }

}catch(e){}


/* ========================================================= */
/* ADDRESS BAR */
/* ========================================================= */

url.addEventListener(
    'keydown',
    e => {

        if(
            e.key ===
            'Enter'
        ){

            send({
                type:'goto',
                url:url.value
            });

            url.blur();

            cv.focus();

        }

        else if(
            e.key ===
            'Escape'
        ){

            url.blur();

            cv.focus();

        }

    }
);


url.addEventListener(
    'focus',
    () =>
        setTimeout(
            () =>
                url.select(),
            0
        )
);


/* ========================================================= */
/* POINTER → REMOTE BROWSER COORDINATES */
/* ========================================================= */

const pt = e => {

    const r =
        cv.getBoundingClientRect();

    return {

        x:
            (
                e.clientX -
                r.left
            )
            *
            vw
            /
            r.width,

        y:
            (
                e.clientY -
                r.top
            )
            *
            vh
            /
            r.height

    };

};


/* ========================================================= */
/* MOUSE MOVE */
/* ========================================================= */

cv.addEventListener(
    'mousemove',
    e => {

        const n =
            performance.now();

        if(
            n-last < 16
        ){
            return;
        }

        last=n;

        send({
            type:'mouse',
            action:'move',
            ...pt(e)
        });

    }
);


/* ========================================================= */
/* POINTER DOWN */
/* ========================================================= */

cv.addEventListener(
    'mousedown',
    e => {

        cv.focus();

        send({
            type:'mouse',
            action:'down',
            button:e.button,
            clicks:e.detail || 1,
            ...pt(e)
        });


        const r =
            stage.getBoundingClientRect();


        const d =
            document.createElement(
                'div'
            );

        d.className='rip';

        d.style.left =
            (
                e.clientX -
                r.left
            ) +
            'px';

        d.style.top =
            (
                e.clientY -
                r.top
            ) +
            'px';

        stage.append(d);

        d.onanimationend =
            () => d.remove();

    }
);


/* ========================================================= */
/* MOUSE UP */
/* ========================================================= */

cv.addEventListener(
    'mouseup',
    e =>
        send({
            type:'mouse',
            action:'up',
            button:e.button,
            clicks:e.detail || 1,
            ...pt(e)
        })
);


/* ========================================================= */
/* WHEEL */
/* ========================================================= */

cv.addEventListener(
    'wheel',
    e => {

        e.preventDefault();

        send({
            type:'mouse',
            action:'wheel',
            dx:e.deltaX,
            dy:e.deltaY,
            ...pt(e)
        });

    },
    {
        passive:false
    }
);


/* ========================================================= */
/* CONTEXT MENU */
/* ========================================================= */

cv.addEventListener(
    'contextmenu',
    e =>
        e.preventDefault()
);


/* ========================================================= */
/* TOUCH → MOUSE */
/* ========================================================= */

/*
 * Mobile browsers don't generate normal
 * mouse events consistently for every gesture.
 *
 * Convert touch into remote mouse input.
 */

let touchDown = false;

let touchStartX = 0;

let touchStartY = 0;


cv.addEventListener(
    'touchstart',
    e => {

        e.preventDefault();

        if(
            !e.touches.length
        ){
            return;
        }

        const t =
            e.touches[0];


        touchDown=true;

        touchStartX =
            t.clientX;

        touchStartY =
            t.clientY;


        send({
            type:'mouse',
            action:'move',
            ...pt(t)
        });


        send({
            type:'mouse',
            action:'down',
            button:0,
            clicks:1,
            ...pt(t)
        });

    },
    {
        passive:false
    }
);


cv.addEventListener(
    'touchmove',
    e => {

        e.preventDefault();

        if(
            !touchDown ||
            !e.touches.length
        ){
            return;
        }

        const t =
            e.touches[0];


        send({
            type:'mouse',
            action:'move',
            ...pt(t)
        });


        const dx =
            touchStartX -
            t.clientX;

        const dy =
            touchStartY -
            t.clientY;


        /*
         * If finger moves significantly,
         * emulate scrolling.
         */

        if(
            Math.abs(dx) > 3 ||
            Math.abs(dy) > 3
        ){

            send({
                type:'mouse',
                action:'wheel',
                dx:-dx * .8,
                dy:-dy * .8,
                ...pt(t)
            });

            touchStartX =
                t.clientX;

            touchStartY =
                t.clientY;

        }

    },
    {
        passive:false
    }
);


cv.addEventListener(
    'touchend',
    e => {

        e.preventDefault();

        if(!touchDown){
            return;
        }

        touchDown=false;

        const t =
            e.changedTouches[0];

        if(t){

            send({
                type:'mouse',
                action:'up',
                button:0,
                clicks:1,
                ...pt(t)
            });

        }

    },
    {
        passive:false
    }
);


/* ========================================================= */
/* KEYBOARD */
/* ========================================================= */

const mod =
    e =>
        e.ctrlKey ||
        e.metaKey;


cv.addEventListener(
    'keydown',
    e => {

        const k =
            e.key.toLowerCase();


        if(
            mod(e) &&
            k === 'v'
        ){
            return;
        }


        if(
            (
                mod(e) &&
                k === 'l'
            )
            ||
            e.key === 'F6'
        ){

            e.preventDefault();

            url.focus();

            return;
        }


        if(
            (
                mod(e) &&
                k === 'r'
            )
            ||
            e.key === 'F5'
        ){

            e.preventDefault();

            send({
                type:'reload'
            });

            return;
        }


        if(
            e.altKey &&
            e.key === 'ArrowLeft'
        ){

            e.preventDefault();

            send({
                type:'back'
            });

            return;
        }


        if(
            e.altKey &&
            e.key === 'ArrowRight'
        ){

            e.preventDefault();

            send({
                type:'forward'
            });

            return;
        }


        e.preventDefault();


        send({
            type:'key',
            action:'down',
            key:e.key
        });

    }
);


cv.addEventListener(
    'keyup',
    e => {

        if(
            mod(e) &&
            e.key.toLowerCase() ===
                'v'
        ){
            return;
        }

        e.preventDefault();

        send({
            type:'key',
            action:'up',
            key:e.key
        });

    }
);


/* ========================================================= */
/* PASTE */
/* ========================================================= */

cv.addEventListener(
    'paste',
    e => {

        e.preventDefault();

        const t =
            e.clipboardData
                .getData(
                    'text'
                );

        if(t){

            send({
                type:'text',
                text:t
            });

        }

    }
);


/* ========================================================= */
/* RESIZE */
/* ========================================================= */

let rt = null;

addEventListener(
    'resize',
    () => {

        clearTimeout(
            rt
        );

        rt =
            setTimeout(
                () => {

                    const {
                        w,
                        h
                    } = size();

                    vw=w;
                    vh=h;

                    send({
                        type:'resize',
                        w,
                        h
                    });

                },
                250
            );

    }
);


/* ========================================================= */
/* QUALITY */
/* ========================================================= */

const QL = [
    'auto',
    'potato',
    'smooth',
    'balanced',
    'sharp'
];


const QN = {

    potato:'Low',

    smooth:'Med',

    balanced:'High',

    sharp:'Max'

};


let qi=0;


$('qual').onclick =
    () => {

        qi =
            (
                qi+1
            )
            %
            QL.length;


        const level =
            QL[qi];


        send({
            type:'quality',
            level
        });


        if(
            level !==
            'auto'
        ){

            $('qual').textContent =
                QN[level];

        }else{

            $('qual').textContent =
                'Auto';

        }

    };


/* ========================================================= */
/* AUDIO */
/* ========================================================= */

let actx = null;

let gain = null;

let nextT = 0;

let aws = null;

let rem =
    new Uint8Array(0);

let arate = 32000;

let muted = false;

let aT = null;

const snd =
    $('snd');


function initAudio(){

    if(actx){

        if(
            actx.state ===
            'suspended'
        ){

            actx.resume()
                .catch(
                    () => {}
                );

        }

        return;

    }


    try{

        actx =
            new (
                window.AudioContext ||
                window.webkitAudioContext
            )({
                latencyHint:
                    'interactive'
            });

    }catch(e){

        return;

    }


    gain =
        actx.createGain();

    gain.connect(
        actx.destination
    );


    connectAudio();

}


function connectAudio(){

    const protocol =
        location.protocol ===
        'https:'
            ? 'wss:'
            : 'ws:';


    aws =
        new WebSocket(
            protocol +
            '//' +
            location.host +
            '/ws/audio'
        );


    aws.binaryType =
        'arraybuffer';


    aws.onmessage =
        e => {

            if(
                typeof e.data ===
                'string'
            ){

                let m;

                try{

                    m =
                        JSON.parse(
                            e.data
                        );

                }catch(err){

                    return;
                }


                if(
                    m.type ===
                    'format'
                ){

                    arate =
                        Number(
                            m.rate
                        ) ||
                        32000;

                }


                if(
                    m.type ===
                    'noaudio'
                ){

                    snd.classList.add(
                        'off'
                    );

                    snd.title =
                        'Sound unavailable on this server';

                    return;
                }

            }


            else{

                playPcm(
                    new Uint8Array(
                        e.data
                    )
                );

            }

        };


    aws.onclose =
        () => {

            setTimeout(
                () => {

                    if(actx){
                        connectAudio();
                    }

                },
                2000
            );

        };

}


function playPcm(
    u8
){

    if(
        muted ||
        !actx
    ){
        return;
    }


    if(
        rem.length
    ){

        const t =
            new Uint8Array(
                rem.length +
                u8.length
            );

        t.set(rem);

        t.set(
            u8,
            rem.length
        );

        u8=t;

    }


    const n =
        Math.floor(
            u8.length / 4
        );


    rem =
        u8.slice(
            n * 4
        );


    if(!n){
        return;
    }


    const pcm =
        new Int16Array(
            u8.buffer,
            u8.byteOffset,
            n * 2
        );


    const buf =
        actx.createBuffer(
            2,
            n,
            arate
        );


    const L =
        buf.getChannelData(0);

    const R =
        buf.getChannelData(1);


    for(
        let i=0;
        i<n;
        i++
    ){

        L[i] =
            pcm[
                2*i
            ] / 32768;

        R[i] =
            pcm[
                2*i+1
            ] / 32768;

    }


    const src =
        actx.createBufferSource();

    src.buffer =
        buf;

    src.connect(
        gain
    );


    const now =
        actx.currentTime;


    if(
        nextT <
        now + .02
    ){

        nextT =
            now + .09;

    }

    else if(
        nextT-now >
        .45
    ){

        return;

    }


    src.start(
        nextT
    );

    nextT +=
        buf.duration;


    snd.classList.add(
        'live'
    );

    clearTimeout(
        aT
    );

    aT =
        setTimeout(
            () =>
                snd.classList.remove(
                    'live'
                ),
            400
        );

}


/* ========================================================= */
/* SOUND BUTTON */
/* ========================================================= */

snd.onclick =
    () => {

        if(!actx){

            initAudio();

            return;

        }


        muted =
            !muted;


        gain.gain.value =
            muted
                ? 0
                : 1;


        snd.classList.toggle(
            'muted',
            muted
        );


        if(
            !muted &&
            actx.state ===
            'suspended'
        ){

            actx.resume()
                .catch(
                    () => {}
                );

        }

    };


/* ========================================================= */
/* AUTO AUDIO INIT */
/* ========================================================= */

addEventListener(
    'pointerdown',
    e => {

        if(
            !e.target.closest(
                '#snd'
            )
        ){

            initAudio();

        }

    }
);


addEventListener(
    'keydown',
    () =>
        initAudio()
);


/* ========================================================= */
/* VISIBILITY */
/* ========================================================= */

document.addEventListener(
    'visibilitychange',
    () => {

        if(
            !document.hidden &&
            ws &&
            ws.readyState >
                WebSocket.CLOSING &&
            !/taken over/i.test(
                lastErr
            )
        ){

            connect();

        }

    }
);


/* ========================================================= */
/* START */
/* ========================================================= */

connect();

</script>

</body>

</html>
"""
