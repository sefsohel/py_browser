"""
PyBrowser: a Chromium-based remote browser written in Python 3.11.

Chromium runs headless on the server via Playwright.
The browser screen is streamed to the client using CDP screencast.
Mouse and keyboard events from the client are replayed into Chromium.
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
from playwright.async_api import (
    Browser,
    Page,
    async_playwright,
)


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("pybrowser")


# =============================================================================
# CONFIGURATION
# =============================================================================

MAX_SESSIONS = int(
    os.getenv("MAX_SESSIONS", "2")
)

HOME_URL = os.getenv(
    "HOME_URL",
    "https://duckduckgo.com",
)

JPEG_QUALITY = max(
    30,
    min(
        90,
        int(
            os.getenv(
                "JPEG_QUALITY",
                "60",
            )
        ),
    ),
)

IDLE_TIMEOUT = int(
    os.getenv(
        "IDLE_TIMEOUT",
        "900",
    )
)

MAX_W = 1920
MAX_H = 1080


# =============================================================================
# SERVER / RENDER
# =============================================================================

HOST = os.getenv(
    "HOST",
    "0.0.0.0",
)

PORT = int(
    os.getenv(
        "PORT",
        "10000",
    )
)


# =============================================================================
# AUTHENTICATION
# =============================================================================

AUTH_USER = os.getenv(
    "BROWSER_USER",
    "admin",
)

AUTH_PASSWORD = os.getenv(
    "BROWSER_PASSWORD",
    "pybrowser",
)

SECRET_KEY = (
    os.getenv("SECRET_KEY")
    or secrets.token_hex(32)
)

SESSION_TTL = int(
    os.getenv(
        "SESSION_TTL",
        "86400",
    )
)

COOKIE = "pb_session"


# =============================================================================
# CHROMIUM
# =============================================================================

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--mute-audio",
]


# =============================================================================
# APPLICATION LIFECYCLE
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    pw = None
    browser = None

    app.state.browser = None
    app.state.active = 0
    app.state.browser_error = None

    log.info(
        "Starting PyBrowser..."
    )

    log.info(
        "Host: %s",
        HOST,
    )

    log.info(
        "Port: %s",
        PORT,
    )

    log.info(
        "HOME_URL: %s",
        HOME_URL,
    )

    try:

        log.info(
            "Starting Playwright..."
        )

        pw = await async_playwright().start()

        log.info(
            "Launching Chromium..."
        )

        browser = await pw.chromium.launch(
            headless=True,
            args=CHROMIUM_ARGS,
        )

        app.state.browser = browser

        log.info(
            "Chromium %s ready",
            browser.version,
        )

        if AUTH_PASSWORD == "pybrowser":

            log.warning(
                "Using the DEFAULT password. "
                "Set BROWSER_PASSWORD in Render Environment Variables!"
            )

        yield

    except Exception as exc:

        app.state.browser_error = str(
            exc
        )

        log.exception(
            "FATAL: Playwright / Chromium startup failed"
        )

        raise

    finally:

        log.info(
            "Shutting down PyBrowser..."
        )

        if browser is not None:

            try:
                await browser.close()
            except Exception:
                log.exception(
                    "Browser close failed"
                )

        if pw is not None:

            try:
                await pw.stop()
            except Exception:
                log.exception(
                    "Playwright stop failed"
                )

        app.state.browser = None


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


# =============================================================================
# HELPERS
# =============================================================================

def normalize_url(
    raw: str,
) -> str | None:

    raw = raw.strip()

    if not raw:
        return None

    if raw == "about:blank":
        return raw

    if re.match(
        r"^[a-zA-Z][a-zA-Z0-9+.-]*://",
        raw,
    ):

        if raw.lower().startswith(
            (
                "http://",
                "https://",
            )
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

    return (
        "https://"
        + raw
    )


def clamp(
    value,
    lo: int,
    hi: int,
    default: int,
) -> int:

    try:

        return max(
            lo,
            min(
                hi,
                int(
                    float(value)
                ),
            ),
        )

    except (
        TypeError,
        ValueError,
    ):

        return default


BUTTONS = {
    0: "left",
    1: "middle",
    2: "right",
}


# =============================================================================
# BROWSER SESSION
# =============================================================================

class Session:
    """
    One browser context + one page for one connected client.
    """

    def __init__(
        self,
        ws: WebSocket,
        browser: Browser,
        w: int,
        h: int,
    ):

        self.ws = ws
        self.browser = browser

        self.w = w
        self.h = h

        self.ctx = None
        self.page: Page | None = None
        self.cdp = None

        self.closed = False

        self._lock = asyncio.Lock()

        self._frame_count = 0
        self._last_frame_at = 0.0

        self._watchdog_task: asyncio.Task | None = None

    # -------------------------------------------------------------------------
    # SAFE JSON SEND
    # -------------------------------------------------------------------------

    async def send_json(
        self,
        obj: dict,
    ) -> bool:

        if self.closed:
            return False

        async with self._lock:

            if self.closed:
                return False

            try:

                await self.ws.send_text(
                    json.dumps(
                        obj,
                        ensure_ascii=False,
                    )
                )

                return True

            except Exception:

                self.closed = True

                return False

    # -------------------------------------------------------------------------
    # SAFE BINARY SEND
    # -------------------------------------------------------------------------

    async def send_bytes(
        self,
        data: bytes,
    ) -> bool:

        if self.closed:
            return False

        async with self._lock:

            if self.closed:
                return False

            try:

                await self.ws.send_bytes(
                    data
                )

                return True

            except Exception:

                self.closed = True

                return False

    # -------------------------------------------------------------------------
    # CDP FRAME
    # -------------------------------------------------------------------------

    async def _on_frame(
        self,
        params: dict,
    ) -> None:

        if self.closed:
            return

        try:

            frame_data = params.get(
                "data"
            )

            session_id = params.get(
                "sessionId"
            )

            if not frame_data:

                log.warning(
                    "Screencast frame received without data"
                )

                return

            jpeg_data = base64.b64decode(
                frame_data
            )

            self._frame_count += 1
            self._last_frame_at = time.monotonic()

            if self._frame_count <= 5:

                log.info(
                    "Screencast frame #%s received: %s bytes",
                    self._frame_count,
                    len(jpeg_data),
                )

            elif (
                self._frame_count % 100 == 0
            ):

                log.debug(
                    "Screencast frame #%s",
                    self._frame_count,
                )

            sent = await self.send_bytes(
                jpeg_data
            )

            # Always ACK the CDP frame when possible.
            #
            # Without ACK Chrome can stop producing more screencast frames.
            if (
                sent
                and self.cdp is not None
                and not self.closed
                and session_id is not None
            ):

                try:

                    await self.cdp.send(
                        "Page.screencastFrameAck",
                        {
                            "sessionId":
                                session_id
                        },
                    )

                except Exception:

                    log.debug(
                        "Screencast frame ACK failed",
                        exc_info=True,
                    )

        except Exception:

            log.exception(
                "Error while processing screencast frame"
            )

    # -------------------------------------------------------------------------
    # NAVIGATION EVENT
    # -------------------------------------------------------------------------

    async def _nav_changed(
        self,
        loading: bool = False,
    ) -> None:

        if self.closed:
            return

        page = self.page

        if page is None:
            return

        try:

            title = await page.title()

        except Exception:

            title = ""

        try:

            current_url = page.url

            await self.send_json(
                {
                    "type": "nav",
                    "url": current_url,
                    "title": title,
                    "loading": loading,
                }
            )

        except Exception:

            self.closed = True

    # -------------------------------------------------------------------------
    # CREATE SESSION
    # -------------------------------------------------------------------------

    async def start(
        self,
    ) -> None:

        if self.browser is None:

            raise RuntimeError(
                "Chromium browser is not available"
            )

        log.info(
            "Creating browser context %sx%s",
            self.w,
            self.h,
        )

        self.ctx = await self.browser.new_context(
            viewport={
                "width": self.w,
                "height": self.h,
            },
            accept_downloads=False,
        )

        self.ctx.on(
            "page",
            lambda page: asyncio.create_task(
                self._on_new_page(
                    page
                )
            ),
        )

        self.page = await self.ctx.new_page()

        page = self.page

        page.on(
            "framenavigated",
            lambda frame: (
                asyncio.create_task(
                    self._nav_changed(True)
                )
                if self.page is not None
                and frame == self.page.main_frame
                else None
            ),
        )

        page.on(
            "load",
            lambda _: asyncio.create_task(
                self._nav_changed(False)
            ),
        )

        page.on(
            "dialog",
            lambda dialog: asyncio.create_task(
                self._dismiss_dialog(
                    dialog
                )
            ),
        )

        # Start CDP screencast before navigation.
        await self._start_screencast()

        # Start fallback watchdog.
        self._watchdog_task = asyncio.create_task(
            self._screenshot_watchdog()
        )

        # Navigate to home.
        await self.goto(
            HOME_URL
        )

        # Send an initial screenshot immediately.
        #
        # This guarantees that the browser page is visible even if
        # Chromium delays the first screencastFrame event.
        await asyncio.sleep(
            0.5
        )

        await self._send_screenshot(
            reason="initial"
        )

    # -------------------------------------------------------------------------
    # START CDP SCREENCAST
    # -------------------------------------------------------------------------

    async def _start_screencast(
        self,
    ) -> None:

        if self.ctx is None:

            raise RuntimeError(
                "Browser context is not available"
            )

        if self.page is None:

            raise RuntimeError(
                "Page is not available"
            )

        await self._stop_screencast()

        log.info(
            "Creating CDP session..."
        )

        self.cdp = (
            await self.ctx.new_cdp_session(
                self.page
            )
        )

        # IMPORTANT:
        # Enable Page domain before starting the screencast.
        await self.cdp.send(
            "Page.enable"
        )

        log.info(
            "CDP Page domain enabled"
        )

        # Register listener BEFORE startScreencast.
        self.cdp.on(
            "Page.screencastFrame",
            self._on_frame,
        )

        log.info(
            "Starting CDP screencast %sx%s...",
            self.w,
            self.h,
        )

        await self.cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": JPEG_QUALITY,
                "maxWidth": self.w,
                "maxHeight": self.h,
                "everyNthFrame": 1,
            },
        )

        log.info(
            "CDP screencast started successfully"
        )

    # -------------------------------------------------------------------------
    # STOP CDP SCREENCAST
    # -------------------------------------------------------------------------

    async def _stop_screencast(
        self,
    ) -> None:

        cdp = self.cdp

        if cdp is None:
            return

        self.cdp = None

        try:

            await cdp.send(
                "Page.stopScreencast"
            )

        except Exception:
            pass

        try:

            await cdp.detach()

        except Exception:
            pass

    # -------------------------------------------------------------------------
    # FALLBACK SCREENSHOT
    # -------------------------------------------------------------------------

    async def _send_screenshot(
        self,
        reason: str = "fallback",
    ) -> None:

        if self.closed:
            return

        page = self.page

        if page is None:
            return

        try:

            jpeg = await page.screenshot(
                type="jpeg",
                quality=JPEG_QUALITY,
                animations="disabled",
            )

            if not jpeg:
                return

            sent = await self.send_bytes(
                jpeg
            )

            if sent:

                self._last_frame_at = (
                    time.monotonic()
                )

                if reason == "initial":

                    log.info(
                        "Initial screenshot sent: %d bytes",
                        len(jpeg),
                    )

                else:

                    log.debug(
                        "Fallback screenshot sent: %d bytes",
                        len(jpeg),
                    )

        except Exception:

            log.debug(
                "Fallback screenshot failed",
                exc_info=True,
            )

    # -------------------------------------------------------------------------
    # SCREENSHOT WATCHDOG
    # -------------------------------------------------------------------------

    async def _screenshot_watchdog(
        self,
    ) -> None:

        """
        If CDP screencast stops producing frames, use Playwright screenshot
        as a fallback. Normally this does nothing because CDP frames keep
        arriving.
        """

        while not self.closed:

            try:

                await asyncio.sleep(
                    2.0
                )

                if self.closed:
                    break

                elapsed = (
                    time.monotonic()
                    - self._last_frame_at
                )

                # No CDP frame for more than 2 seconds.
                if elapsed > 2.0:

                    log.warning(
                        "No CDP frame for %.1f seconds. "
                        "Using screenshot fallback.",
                        elapsed,
                    )

                    await self._send_screenshot(
                        reason="fallback"
                    )

            except asyncio.CancelledError:

                break

            except Exception:

                log.debug(
                    "Screenshot watchdog error",
                    exc_info=True,
                )

    # -------------------------------------------------------------------------
    # POPUP
    # -------------------------------------------------------------------------

    async def _on_new_page(
        self,
        popup: Page,
    ) -> None:

        if self.closed:
            return

        try:

            opener = await popup.opener()

            if opener is None:
                return

            await popup.wait_for_load_state(
                "domcontentloaded",
                timeout=10000,
            )

            popup_url = popup.url

            log.info(
                "Popup intercepted: %s",
                popup_url,
            )

            await popup.close()

            if (
                popup_url
                and popup_url != "about:blank"
                and not self.closed
            ):

                await self.goto(
                    popup_url
                )

        except Exception:

            log.debug(
                "Popup handling failed",
                exc_info=True,
            )

    # -------------------------------------------------------------------------
    # DIALOG
    # -------------------------------------------------------------------------

    async def _dismiss_dialog(
        self,
        dialog,
    ) -> None:

        try:

            await dialog.dismiss()

        except Exception:

            pass

    # -------------------------------------------------------------------------
    # CLOSE SESSION
    # -------------------------------------------------------------------------

    async def close(
        self,
    ) -> None:

        if self.closed:
            pass

        self.closed = True

        # Stop watchdog.
        if self._watchdog_task is not None:

            self._watchdog_task.cancel()

            try:

                await self._watchdog_task

            except (
                asyncio.CancelledError,
                Exception,
            ):

                pass

            self._watchdog_task = None

        # Stop screencast.
        await self._stop_screencast()

        # Close browser context.
        if self.ctx is not None:

            try:

                await self.ctx.close()

            except Exception:

                log.debug(
                    "Browser context close failed",
                    exc_info=True,
                )

        self.ctx = None
        self.page = None

    # -------------------------------------------------------------------------
    # GOTO
    # -------------------------------------------------------------------------

    async def goto(
        self,
        raw: str,
    ) -> None:

        if self.closed:
            return

        page = self.page

        if page is None:
            return

        url = normalize_url(
            raw
        )

        if not url:

            await self.send_json(
                {
                    "type": "error",
                    "message":
                        "Only http(s) URLs are allowed",
                }
            )

            return

        log.info(
            "Navigating to %s",
            url,
        )

        try:

            await page.goto(
                url,
                wait_until="commit",
                timeout=30000,
            )

        except Exception as exc:

            message = (
                str(exc)
                .splitlines()[0][:300]
            )

            log.warning(
                "Navigation failed: %s",
                message,
            )

            await self.send_json(
                {
                    "type": "error",
                    "message": message,
                }
            )

            await self._nav_changed(
                False
            )

    # -------------------------------------------------------------------------
    # HANDLE CLIENT EVENT
    # -------------------------------------------------------------------------

    async def handle(
        self,
        m: dict,
    ) -> None:

        if self.closed:
            return

        t = m.get(
            "type"
        )

        page = self.page

        if page is None:
            return

        try:

            # =================================================================
            # URL
            # =================================================================

            if t == "goto":

                await self.goto(
                    str(
                        m.get(
                            "url",
                            "",
                        )
                    )
                )

            # =================================================================
            # HOME
            # =================================================================

            elif t == "home":

                await self.goto(
                    HOME_URL
                )

            # =================================================================
            # BACK
            # =================================================================

            elif t == "back":

                try:

                    await page.go_back(
                        wait_until="commit",
                        timeout=30000,
                    )

                except Exception:

                    pass

            # =================================================================
            # FORWARD
            # =================================================================

            elif t == "forward":

                try:

                    await page.go_forward(
                        wait_until="commit",
                        timeout=30000,
                    )

                except Exception:

                    pass

            # =================================================================
            # RELOAD
            # =================================================================

            elif t == "reload":

                try:

                    await page.reload(
                        wait_until="commit",
                        timeout=30000,
                    )

                except Exception:

                    pass

            # =================================================================
            # MOUSE
            # =================================================================

            elif t == "mouse":

                try:

                    x = float(
                        m.get(
                            "x",
                            0,
                        )
                    )

                    y = float(
                        m.get(
                            "y",
                            0,
                        )
                    )

                    action = m.get(
                        "action"
                    )

                    await page.mouse.move(
                        x,
                        y,
                    )

                    button = BUTTONS.get(
                        int(
                            m.get(
                                "button",
                                0,
                            )
                        ),
                        "left",
                    )

                    clicks = max(
                        1,
                        int(
                            m.get(
                                "clicks",
                                1,
                            )
                        ),
                    )

                    if action == "down":

                        await page.mouse.down(
                            button=button,
                            click_count=clicks,
                        )

                    elif action == "up":

                        await page.mouse.up(
                            button=button,
                            click_count=clicks,
                        )

                    elif action == "wheel":

                        await page.mouse.wheel(
                            float(
                                m.get(
                                    "dx",
                                    0,
                                )
                            ),
                            float(
                                m.get(
                                    "dy",
                                    0,
                                )
                            ),
                        )

                except Exception:

                    log.debug(
                        "Mouse event failed",
                        exc_info=True,
                    )

            # =================================================================
            # KEYBOARD
            # =================================================================

            elif t == "key":

                key = m.get(
                    "key"
                )

                if (
                    isinstance(
                        key,
                        str,
                    )
                    and key
                    and key not in (
                        "Dead",
                        "Unidentified",
                        "AltGraph",
                    )
                ):

                    if (
                        m.get(
                            "action"
                        )
                        == "down"
                    ):

                        await page.keyboard.down(
                            key
                        )

                    else:

                        await page.keyboard.up(
                            key
                        )

            # =================================================================
            # TEXT / PASTE
            # =================================================================

            elif t == "text":

                text_value = str(
                    m.get(
                        "text",
                        "",
                    )
                )[:100_000]

                if text_value:

                    await page.keyboard.insert_text(
                        text_value
                    )

            # =================================================================
            # RESIZE
            # =================================================================

            elif t == "resize":

                new_w = clamp(
                    m.get("w"),
                    320,
                    MAX_W,
                    self.w,
                )

                new_h = clamp(
                    m.get("h"),
                    240,
                    MAX_H,
                    self.h,
                )

                if (
                    new_w == self.w
                    and new_h == self.h
                ):

                    return

                self.w = new_w
                self.h = new_h

                log.info(
                    "Resize browser to %sx%s",
                    self.w,
                    self.h,
                )

                await self._stop_screencast()

                await page.set_viewport_size(
                    {
                        "width":
                            self.w,
                        "height":
                            self.h,
                    }
                )

                if not self.closed:

                    await self._start_screencast()

                    await self._send_screenshot(
                        reason="resize"
                    )

        except Exception as exc:

            # Never allow one malformed event to kill the session.
            log.debug(
                "Event %s failed: %s",
                t,
                exc,
            )


# =============================================================================
# AUTHENTICATION
# =============================================================================

_fails: dict[
    str,
    list[float],
] = {}


def make_token() -> str:

    exp = (
        int(
            time.time()
        )
        + SESSION_TTL
    )

    payload = (
        f"{AUTH_USER}:{exp}"
    )

    sig = hmac.new(
        SECRET_KEY.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    return (
        f"{exp}.{sig}"
    )


def valid_token(
    token: str | None,
) -> bool:

    try:

        exp, sig = (
            token or ""
        ).split(
            ".",
            1,
        )

        payload = (
            f"{AUTH_USER}:{exp}"
        )

        good = hmac.new(
            SECRET_KEY.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        return (
            hmac.compare_digest(
                sig,
                good,
            )
            and int(exp)
            > int(time.time())
        )

    except Exception:

        return False


def client_ip(
    request: Request,
) -> str:

    forwarded = request.headers.get(
        "x-forwarded-for",
        "",
    )

    if forwarded:

        first = (
            forwarded
            .split(",")[0]
            .strip()
        )

        if first:
            return first

    if request.client:

        return request.client.host

    return "?"


def is_locked(
    ip: str,
) -> bool:

    now = time.time()

    recent = [
        ts
        for ts in _fails.get(
            ip,
            [],
        )
        if now - ts < 300
    ]

    _fails[ip] = recent

    return len(recent) >= 5


# =============================================================================
# HTTP AUTH MIDDLEWARE
# =============================================================================

@app.middleware("http")
async def auth_middleware(
    request: Request,
    call_next,
):

    path = request.url.path

    public_paths = {
        "/healthz",
        "/login",
        "/logout",
    }

    if (
        path in public_paths
        or valid_token(
            request.cookies.get(
                COOKIE
            )
        )
    ):

        return await call_next(
            request
        )

    if (
        path == "/"
        and request.method == "GET"
    ):

        return HTMLResponse(
            LOGIN_HTML,
            headers={
                "Cache-Control":
                    "no-store"
            },
        )

    return JSONResponse(
        {
            "error":
                "unauthorized"
        },
        status_code=401,
    )


# =============================================================================
# LOGIN
# =============================================================================

@app.post("/login")
async def login(
    request: Request,
):

    ip = client_ip(
        request
    )

    if is_locked(ip):

        return JSONResponse(
            {
                "error":
                    "Too many attempts. "
                    "Try again in a few minutes."
            },
            status_code=429,
        )

    try:

        data = await request.json()

    except Exception:

        data = {}

    user = str(
        data.get(
            "username",
            "",
        )
    )

    password = str(
        data.get(
            "password",
            "",
        )
    )

    user_ok = hmac.compare_digest(
        user.encode(),
        AUTH_USER.encode(),
    )

    password_ok = hmac.compare_digest(
        password.encode(),
        AUTH_PASSWORD.encode(),
    )

    if not (
        user_ok
        and password_ok
    ):

        _fails.setdefault(
            ip,
            [],
        ).append(
            time.time()
        )

        await asyncio.sleep(
            1
        )

        return JSONResponse(
            {
                "error":
                    "Incorrect username or password"
            },
            status_code=401,
        )

    _fails.pop(
        ip,
        None,
    )

    response = JSONResponse(
        {
            "ok": True
        }
    )

    forwarded_proto = (
        request.headers.get(
            "x-forwarded-proto",
            "",
        )
        .split(",")[0]
        .strip()
        .lower()
    )

    secure = (
        forwarded_proto == "https"
    )

    response.set_cookie(
        COOKIE,
        make_token(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite=(
            "none"
            if secure
            else "lax"
        ),
        secure=secure,
        path="/",
    )

    return response


# =============================================================================
# LOGOUT
# =============================================================================

@app.post("/logout")
async def logout():

    response = JSONResponse(
        {
            "ok": True
        }
    )

    response.delete_cookie(
        COOKIE,
        path="/",
    )

    return response


# =============================================================================
# CURRENT USER
# =============================================================================

@app.get("/api/me")
async def me():

    return {
        "user":
            AUTH_USER
    }


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.get("/healthz")
async def healthz():

    browser = getattr(
        app.state,
        "browser",
        None,
    )

    connected = (
        browser is not None
        and browser.is_connected()
    )

    return JSONResponse(
        {
            "ok":
                connected,

            "browser_connected":
                connected,

            "sessions":
                getattr(
                    app.state,
                    "active",
                    0,
                ),

            "browser_error":
                getattr(
                    app.state,
                    "browser_error",
                    None,
                ),
        }
    )


# =============================================================================
# INDEX
# =============================================================================

@app.get(
    "/",
    response_class=HTMLResponse,
)
async def index():

    return INDEX_HTML


# =============================================================================
# WEBSOCKET
# =============================================================================

@app.websocket("/ws")
async def ws_endpoint(
    ws: WebSocket,
):

    # -------------------------------------------------------------------------
    # Auth
    # -------------------------------------------------------------------------

    if not valid_token(
        ws.cookies.get(
            COOKIE
        )
    ):

        try:

            await ws.close(
                code=4401
            )

        except Exception:
            pass

        return

    # -------------------------------------------------------------------------
    # Browser availability
    # -------------------------------------------------------------------------

    browser = getattr(
        app.state,
        "browser",
        None,
    )

    if browser is None:

        try:

            await ws.accept()

            await ws.send_text(
                json.dumps(
                    {
                        "type":
                            "error",
                        "message":
                            (
                                "Chromium is not available. "
                                "Check Render logs."
                            ),
                    }
                )
            )

            await ws.close(
                code=1011
            )

        except Exception:
            pass

        return

    # -------------------------------------------------------------------------
    # Accept
    # -------------------------------------------------------------------------

    try:

        await ws.accept()

    except Exception:

        return

    # -------------------------------------------------------------------------
    # Session limit
    # -------------------------------------------------------------------------

    if app.state.active >= MAX_SESSIONS:

        try:

            await ws.send_text(
                json.dumps(
                    {
                        "type":
                            "error",
                        "message":
                            (
                                "Server busy: "
                                "max sessions reached"
                            ),
                    }
                )
            )

        except Exception:
            pass

        try:

            await ws.close(
                code=1013
            )

        except Exception:
            pass

        return

    # -------------------------------------------------------------------------
    # Count session
    # -------------------------------------------------------------------------

    app.state.active += 1

    counted = True

    w = clamp(
        ws.query_params.get("w"),
        320,
        MAX_W,
        1280,
    )

    h = clamp(
        ws.query_params.get("h"),
        240,
        MAX_H,
        720,
    )

    sess = Session(
        ws,
        browser,
        w,
        h,
    )

    try:

        # ---------------------------------------------------------------------
        # Start browser
        # ---------------------------------------------------------------------

        await sess.start()

        # ---------------------------------------------------------------------
        # Receive loop
        # ---------------------------------------------------------------------

        while True:

            if sess.closed:
                break

            try:

                raw = await asyncio.wait_for(
                    ws.receive_text(),
                    timeout=IDLE_TIMEOUT,
                )

            except asyncio.TimeoutError:

                await sess.send_json(
                    {
                        "type":
                            "error",
                        "message":
                            "Closed after inactivity",
                    }
                )

                try:

                    await ws.close(
                        code=1000
                    )

                except Exception:
                    pass

                break

            except WebSocketDisconnect:

                log.info(
                    "WebSocket client disconnected"
                )

                break

            except RuntimeError as exc:

                text = str(
                    exc
                )

                if (
                    "WebSocket is not connected"
                    in text
                ):

                    log.debug(
                        "WebSocket already disconnected"
                    )

                    break

                raise

            # -----------------------------------------------------------------
            # JSON decode
            # -----------------------------------------------------------------

            try:

                message = json.loads(
                    raw
                )

            except json.JSONDecodeError:

                continue

            if isinstance(
                message,
                dict,
            ):

                await sess.handle(
                    message
                )

    except WebSocketDisconnect:

        log.debug(
            "WebSocket disconnected"
        )

    except RuntimeError as exc:

        if (
            "WebSocket is not connected"
            in str(exc)
        ):

            log.debug(
                "WebSocket already disconnected"
            )

        else:

            log.exception(
                "WebSocket runtime error"
            )

    except Exception:

        log.exception(
            "Session crashed"
        )

    finally:

        # ---------------------------------------------------------------------
        # Active count
        # ---------------------------------------------------------------------

        if counted:

            app.state.active = max(
                0,
                app.state.active - 1,
            )

        # ---------------------------------------------------------------------
        # Cleanup
        # ---------------------------------------------------------------------

        try:

            await sess.close()

        except Exception:

            log.debug(
                "Session cleanup failed",
                exc_info=True,
            )

        # IMPORTANT:
        #
        # Do not call ws.close() here.
        #
        # Client may already be disconnected, which was the source of:
        #
        # RuntimeError: WebSocket is not connected.
        #
        # Session.close() handles browser cleanup.


# =============================================================================
# FRONTEND
# =============================================================================

INDEX_HTML = r"""<!doctype html>

<html
    lang="en"
    data-theme="dark"
>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
PyBrowser
</title>

<link
    rel="icon"
    href="data:image/svg+xml,%3Csvg%20viewBox%3D%220%200%2096%2096%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Cdefs%3E%3ClinearGradient%20id%3D%22lgf%22%20x1%3D%220%22%20y1%3D%220%22%20x2%3D%221%22%20y2%3D%221%22%3E%3Cstop%20offset%3D%220%22%20stop-color%3D%22%237c9cff%22%2F%3E%3Cstop%20offset%3D%221%22%20stop-color%3D%22%23b57cff%22%2F%3E%3C%2FlinearGradient%3E%3C%2Fdefs%3E%3Crect%20width%3D%2296%22%20height%3D%2296%22%20rx%3D%2226%22%20fill%3D%22url%28%23lgf%29%22%2F%3E%3Ccircle%20cx%3D%2248%22%20cy%3D%2248%22%20r%3D%2226%22%20fill%3D%22none%22%20stroke%3D%22%23fff%22%20stroke-width%3D%224%22%2F%3E%3Cellipse%20cx%3D%2248%22%20cy%3D%2248%22%20rx%3D%2211%22%20ry%3D%2226%22%20fill%3D%22none%22%20stroke%3D%22%23fff%22%20stroke-width%3D%224%22%20opacity%3D%22.9%22%2F%3E%3Cpath%20d%3D%22M22%2048h52M27%2034h42M27%2062h42%22%20stroke%3D%22%23fff%22%20stroke-width%3D%223.5%22%20stroke-linecap%3D%22round%22%20fill%3D%22none%22%20opacity%3D%22.9%22%2F%3E%3Cpath%20d%3D%22M58%2056l22%208-9%203-3%209z%22%20fill%3D%22%23fff%22%20stroke%3D%22%237c9cff%22%20stroke-width%3D%222.5%22%20stroke-linejoin%3D%22round%22%2F%3E%3C%2Fsvg%3E"
>

<style>

:root{
    --bg:#0e1015;
    --bar:#161922e6;
    --chip:#232837;
    --chip2:#2f3548;
    --fg:#e9ebf2;
    --mut:#8a92a8;
    --acc:#7c9cff;
    --acc2:#b57cff;
    --ok:#4ade80;
    --bad:#f87171;
    --warn:#fbbf24;
    --sh:0 10px 34px #0007;
}

:root[data-theme=light]{
    --bg:#eceff5;
    --bar:#ffffffe6;
    --chip:#e5e8f0;
    --chip2:#d6dbe8;
    --fg:#1a1e2b;
    --mut:#657090;
    --sh:0 10px 34px #0002;
}

*{
    box-sizing:border-box;
}

html,
body{
    height:100%;
    margin:0;
    background:var(--bg);
    color:var(--fg);
    font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;
    overflow:hidden;
}

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
    border-bottom:1px solid #ffffff12;
    z-index:5;
    animation:down .5s cubic-bezier(.2,.9,.3,1) both;
}

@keyframes down{
    from{
        transform:translateY(-100%);
        opacity:0;
    }
}

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
    animation:rot .8s linear infinite;
    color:var(--acc);
}

@keyframes rot{
    to{
        transform:rotate(360deg);
    }
}

#pill{
    flex:1;
    min-width:0;
    height:38px;
    display:flex;
    align-items:center;
    gap:8px;
    padding:0 6px 0 12px;
    border-radius:19px;
    background:var(--chip);
    border:1.5px solid transparent;
    transition:
        border-color .25s,
        box-shadow .25s,
        background .25s;
}

#pill:focus-within{
    border-color:var(--acc);
    box-shadow:0 0 0 4px #7c9cff29;
    background:var(--bg);
}

#lock{
    width:16px;
    height:16px;
    flex:none;
    color:var(--ok);
    transition:color .3s;
}

#lock[data-s="0"]{
    color:var(--warn);
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

#dot{
    width:9px;
    height:9px;
    border-radius:50%;
    margin:0 6px;
    background:var(--warn);
    flex:none;
    transition:background .3s;
}

#dot.on{
    background:var(--ok);
    animation:pulse 2.4s infinite;
}

#dot.off{
    background:var(--bad);
}

@keyframes pulse{

    0%{
        box-shadow:0 0 0 0 #4ade8088;
    }

    70%,
    100%{
        box-shadow:0 0 0 8px #4ade8000;
    }

}

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
    border-radius:0 3px 3px 0;
    box-shadow:0 0 10px var(--acc);
}

#stage{
    position:absolute;
    top:56px;
    left:0;
    right:0;
    bottom:0;
    background:#fff;
}

#cv{
    width:100%;
    height:100%;
    display:block;
    outline:none;
    opacity:0;
    transition:opacity .5s;
}

#cv.show{
    opacity:1;
}

.loading #cv{
    filter:brightness(.96);
}

.rip{
    position:absolute;
    width:16px;
    height:16px;
    margin:-8px 0 0 -8px;
    border-radius:50%;
    border:2px solid var(--acc);
    background:#7c9cff33;
    pointer-events:none;
    animation:rip .55s ease-out forwards;
}

@keyframes rip{

    to{
        transform:scale(4);
        opacity:0;
    }

}

#splash,
#over{
    position:absolute;
    inset:56px 0 0 0;
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
    opacity:.35;
    animation:float 9s ease-in-out infinite alternate;
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
            translate(60px,-40px)
            scale(1.2);
    }

}

.card{
    position:relative;
    animation:
        rise .7s
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
    margin:0 auto 18px;
    filter:
        drop-shadow(
            0 10px 22px #7c9cff55
        );
    animation:
        bob 2.2s
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
    margin:16px auto 0;
    border-radius:50%;
    border:3px solid var(--chip2);
    border-top-color:var(--acc);
    animation:
        rot .8s
        linear
        infinite;
}

.card h1{
    margin:0 0 4px;
    font-size:22px;
}

.card p{
    margin:0;
    color:var(--mut);
}

.btn{
    margin-top:18px;
    border:0;
    border-radius:22px;
    padding:10px 22px;
    font:inherit;
    font-weight:600;
    color:#fff;
    cursor:pointer;
    background:
        linear-gradient(
            135deg,
            var(--acc),
            var(--acc2)
        );
    transition:
        transform .15s,
        box-shadow .2s;
}

.btn:hover{
    transform:translateY(-2px);
    box-shadow:0 8px 20px #7c9cff55;
}

.btn:active{
    transform:scale(.95);
}

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
    border:1px solid #ffffff1a;
    border-left:4px solid var(--bad);
    color:var(--fg);
    padding:10px 14px;
    border-radius:10px;
    box-shadow:var(--sh);
    max-width:340px;
    animation:
        in .35s
        cubic-bezier(.2,.9,.3,1)
        both;
}

.toast.out{
    animation:
        out .3s
        forwards;
}

@keyframes in{

    from{
        transform:translateX(120%);
        opacity:0;
    }

}

@keyframes out{

    to{
        transform:translateX(120%);
        opacity:0;
    }

}

@media (max-width:560px){

    #bar .opt{
        display:none;
    }

}

@media (prefers-reduced-motion:reduce){

    *{
        animation-duration:.01s!important;
        transition-duration:.01s!important;
    }

}

</style>

</head>

<body>

<div id="bar">

<button
    class="ib"
    id="back"
    title="Back (Alt+←)"
>
<svg viewBox="0 0 24 24">
<path d="M19 12H5M12 19l-7-7 7-7"/>
</svg>
</button>

<button
    class="ib opt"
    id="fwd"
    title="Forward (Alt+→)"
>
<svg viewBox="0 0 24 24">
<path d="M5 12h14M12 5l7 7-7 7"/>
</svg>
</button>

<button
    class="ib"
    id="rel"
    title="Reload (Ctrl+R)"
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

<path d="M8 11V7a4 4 0 0 1 8 0v4"/>

</svg>

</span>

<input
    id="url"
    placeholder="Search or enter address (Ctrl+L)"
    autocomplete="off"
    spellcheck="false"
/>

</div>

<span
    id="dot"
    title="Connection"
></span>

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

<button
    class="ib opt"
    id="logout"
    title="Sign out"
>

<svg viewBox="0 0 24 24">

<path
    d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"
/>

</svg>

</button>

<div id="prog"></div>

</div>


<div id="stage">

<canvas
    id="cv"
    tabindex="0"
></canvas>

</div>


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
    stop-color="#7c9cff"
/>

<stop
    offset="1"
    stop-color="#b57cff"
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
    stroke="#fff"
    stroke-width="4"
/>

<ellipse
    cx="48"
    cy="48"
    rx="11"
    ry="26"
    fill="none"
    stroke="#fff"
    stroke-width="4"
    opacity=".9"
/>

<path
    d="M22 48h52M27 34h42M27 62h42"
    stroke="#fff"
    stroke-width="3.5"
    stroke-linecap="round"
    fill="none"
    opacity=".9"
/>

<path
    d="M58 56l22 8-9 3-3 9z"
    fill="#fff"
    stroke="#7c9cff"
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
    id="lg3"
    x1="0"
    y1="0"
    x2="1"
    y2="1"
>

<stop
    offset="0"
    stop-color="#7c9cff"
/>

<stop
    offset="1"
    stop-color="#b57cff"
/>

</linearGradient>

</defs>

<rect
    width="96"
    height="96"
    rx="26"
    fill="url(#lg3)"
/>

<circle
    cx="48"
    cy="48"
    r="26"
    fill="none"
    stroke="#fff"
    stroke-width="4"
/>

<ellipse
    cx="48"
    cy="48"
    rx="11"
    ry="26"
    fill="none"
    stroke="#fff"
    stroke-width="4"
    opacity=".9"
/>

<path
    d="M22 48h52M27 34h42M27 62h42"
    stroke="#fff"
    stroke-width="3.5"
    stroke-linecap="round"
    fill="none"
    opacity=".9"
/>

<path
    d="M58 56l22 8-9 3-3 9z"
    fill="#fff"
    stroke="#7c9cff"
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

const $ =
    id => document.getElementById(id);


const stage =
    $('stage');

const cv =
    $('cv');

const ctx =
    cv.getContext('2d');

const url =
    $('url');

const prog =
    $('prog');

const dot =
    $('dot');

const lock =
    $('lock');

const rel =
    $('rel');

const splash =
    $('splash');

const over =
    $('over');

const root =
    document.documentElement;


let ws = null;

let last = 0;

let lastErr = '';

let q =
    Promise.resolve();

let loadT = null;

let opened = false;


/* ========================================================================= */
/* SIZE                                                                      */
/* ========================================================================= */

const size = () => ({

    w:
        Math.max(
            320,
            Math.min(
                1920,
                stage.clientWidth | 0
            )
        ),

    h:
        Math.max(
            240,
            Math.min(
                1080,
                stage.clientHeight | 0
            )
        )

});


/* ========================================================================= */
/* SEND                                                                      */
/* ========================================================================= */

const send = object => {

    if (
        ws
        &&
        ws.readyState ===
            WebSocket.OPEN
    ) {

        try {

            ws.send(
                JSON.stringify(
                    object
                )
            );

            return true;

        } catch (e) {

            return false;
        }
    }

    return false;
};


/* ========================================================================= */
/* TOAST                                                                     */
/* ========================================================================= */

function toast(
    message
) {

    const d =
        document.createElement(
            'div'
        );

    d.className =
        'toast';

    d.textContent =
        message;

    $('toasts').append(
        d
    );

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


/* ========================================================================= */
/* LOADING                                                                   */
/* ========================================================================= */

function setLoading(
    on
) {

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

    if (on) {

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
                () =>
                    setLoading(false),
                30000
            );

    } else {

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


/* ========================================================================= */
/* CONNECT                                                                   */
/* ========================================================================= */

function connect() {

    lastErr =
        '';

    opened =
        false;

    q =
        Promise.resolve();

    dot.className =
        '';

    over.classList.add(
        'hide'
    );

    splash.classList.remove(
        'hide'
    );

    $('splashTxt').textContent =
        'Starting your private browser…';


    /*
     * Close an old socket before creating a new one.
     */

    if (ws) {

        try {

            ws.onopen =
                null;

            ws.onmessage =
                null;

            ws.onerror =
                null;

            ws.onclose =
                null;

            if (
                ws.readyState ===
                    WebSocket.OPEN
                ||
                ws.readyState ===
                    WebSocket.CONNECTING
            ) {

                ws.close();
            }

        } catch (e) {}

        ws =
            null;
    }


    const {
        w,
        h
    } = size();


    const protocol =
        location.protocol ===
            'https:'
            ? 'wss'
            : 'ws';


    const socket =
        new WebSocket(
            protocol
            + '://'
            + location.host
            + '/ws?w='
            + w
            + '&h='
            + h
        );


    ws =
        socket;


    /* ===================================================================== */
    /* OPEN                                                                  */
    /* ===================================================================== */

    socket.onopen = () => {

        if (
            socket !== ws
        )
            return;

        opened =
            true;

        dot.className =
            'on';

        try {

            cv.focus();

        } catch (e) {}

    };


    /* ===================================================================== */
    /* MESSAGE                                                               */
    /* ===================================================================== */

    socket.onmessage =
        event => {

            if (
                socket !== ws
            )
                return;


            /*
             * JSON message
             */

            if (
                typeof event.data ===
                'string'
            ) {

                let message;

                try {

                    message =
                        JSON.parse(
                            event.data
                        );

                } catch (e) {

                    return;
                }


                /*
                 * Navigation update
                 */

                if (
                    message.type ===
                    'nav'
                ) {

                    const currentUrl =
                        message.url ||
                        '';


                    if (
                        document.activeElement
                        !== url
                    ) {

                        url.value =
                            currentUrl ===
                            'about:blank'
                                ? ''
                                : currentUrl;
                    }


                    lock.dataset.s =
                        currentUrl.startsWith(
                            'https:'
                        )
                            ? '1'
                            : '0';


                    document.title =
                        message.title
                            ? (
                                message.title
                                + ' – PyBrowser'
                            )
                            : 'PyBrowser';


                    setLoading(
                        !!message.loading
                    );

                    return;
                }


                /*
                 * Server error
                 */

                if (
                    message.type ===
                    'error'
                ) {

                    lastErr =
                        message.message
                        ||
                        'Unknown error';

                    toast(
                        lastErr
                    );

                    return;
                }

                return;
            }


            /*
             * Binary JPEG frame
             */

            q =
                q.then(
                    async () => {

                        if (
                            socket !==
                            ws
                        )
                            return;

                        let bitmap =
                            null;

                        try {

                            bitmap =
                                await createImageBitmap(
                                    event.data
                                );

                        } catch (e) {

                            return;
                        }


                        if (
                            socket !==
                            ws
                        ) {

                            bitmap.close();

                            return;
                        }


                        if (
                            cv.width !==
                                bitmap.width
                            ||
                            cv.height !==
                                bitmap.height
                        ) {

                            cv.width =
                                bitmap.width;

                            cv.height =
                                bitmap.height;
                        }


                        ctx.drawImage(
                            bitmap,
                            0,
                            0
                        );


                        bitmap.close();


                        if (
                            !cv.classList.contains(
                                'show'
                            )
                        ) {

                            cv.classList.add(
                                'show'
                            );

                            splash.classList.add(
                                'hide'
                            );
                        }

                    }
                )
                .catch(
                    () => {}
                );
        };


    /* ===================================================================== */
    /* ERROR                                                                 */
    /* ===================================================================== */

    socket.onerror =
        () => {

            if (
                socket !==
                ws
            )
                return;

            lastErr =
                'WebSocket connection error';

            dot.className =
                'off';
        };


    /* ===================================================================== */
    /* CLOSE                                                                 */
    /* ===================================================================== */

    socket.onclose =
        event => {

            if (
                socket !==
                ws
            )
                return;


            const wasOpened =
                opened;


            opened =
                false;


            dot.className =
                'off';


            cv.classList.remove(
                'show'
            );


            setLoading(
                false
            );


            /*
             * Authentication failed.
             */

            if (
                event.code ===
                4401
                ||
                !wasOpened
            ) {

                fetch(
                    '/api/me',
                    {
                        cache:
                            'no-store'
                    }
                )
                    .then(
                        response => {

                            if (
                                response.status ===
                                401
                            ) {

                                location.reload();
                            }
                        }
                    )
                    .catch(
                        () => {}
                    );
            }


            const busy =
                event.code ===
                    1013
                ||
                /busy/i.test(
                    lastErr
                );


            $('overH').textContent =
                busy
                    ? 'Server is busy'
                    : 'Disconnected';


            $('overP').textContent =
                lastErr
                ||
                'The session ended.';


            splash.classList.add(
                'hide'
            );

            over.classList.remove(
                'hide'
            );
        };
}


/* ========================================================================= */
/* RECONNECT                                                                 */
/* ========================================================================= */

$('reco').onclick =
    () => {

        connect();

    };


/* ========================================================================= */
/* NAVIGATION                                                                */
/* ========================================================================= */

$('back').onclick =
    () => {

        send({
            type:
                'back'
        });

    };


$('fwd').onclick =
    () => {

        send({
            type:
                'forward'
        });

    };


rel.onclick =
    () => {

        send({
            type:
                'reload'
        });

    };


$('home').onclick =
    () => {

        send({
            type:
                'home'
        });

    };


/* ========================================================================= */
/* LOGOUT                                                                    */
/* ========================================================================= */

$('logout').onclick =
    async () => {

        try {

            if (
                ws
                &&
                ws.readyState ===
                    WebSocket.OPEN
            ) {

                ws.close();
            }

        } catch (e) {}


        try {

            await fetch(
                '/logout',
                {
                    method:
                        'POST'
                }
            );

        } catch (e) {}


        location.reload();
    };


/* ========================================================================= */
/* FULLSCREEN                                                                */
/* ========================================================================= */

$('full').onclick =
    () => {

        if (
            document.fullscreenElement
        ) {

            document.exitFullscreen();

        } else {

            root.requestFullscreen()
                .catch(
                    () => {}
                );
        }

    };


/* ========================================================================= */
/* THEME                                                                     */
/* ========================================================================= */

$('theme').onclick =
    () => {

        const theme =
            root.dataset.theme ===
                'dark'
                ? 'light'
                : 'dark';

        root.dataset.theme =
            theme;

        try {

            localStorage.setItem(
                'pb-theme',
                theme
            );

        } catch (e) {}

    };


try {

    const savedTheme =
        localStorage.getItem(
            'pb-theme'
        );

    if (
        savedTheme
    ) {

        root.dataset.theme =
            savedTheme;
    }

} catch (e) {}


/* ========================================================================= */
/* ADDRESS BAR                                                               */
/* ========================================================================= */

url.addEventListener(
    'keydown',
    e => {

        if (
            e.key ===
            'Enter'
        ) {

            send({
                type:
                    'goto',
                url:
                    url.value
            });

            url.blur();

            cv.focus();

        } else if (
            e.key ===
            'Escape'
        ) {

            url.blur();

            cv.focus();
        }

    }
);


url.addEventListener(
    'focus',
    () => {

        setTimeout(
            () =>
                url.select(),
            0
        );

    }
);


/* ========================================================================= */
/* COORDINATES                                                               */
/* ========================================================================= */

const pt =
    e => {

        const r =
            cv.getBoundingClientRect();

        return {

            x:
                (e.clientX - r.left)
                *
                cv.width
                /
                r.width,

            y:
                (e.clientY - r.top)
                *
                cv.height
                /
                r.height

        };

    };


/* ========================================================================= */
/* MOUSE MOVE                                                                */
/* ========================================================================= */

cv.addEventListener(
    'mousemove',
    e => {

        const now =
            performance.now();

        if (
            now - last < 30
        )
            return;

        last =
            now;

        send({
            type:
                'mouse',
            action:
                'move',
            ...pt(e)
        });

    }
);


/* ========================================================================= */
/* MOUSE DOWN                                                                */
/* ========================================================================= */

cv.addEventListener(
    'mousedown',
    e => {

        cv.focus();

        send({
            type:
                'mouse',
            action:
                'down',
            button:
                e.button,
            clicks:
                e.detail || 1,
            ...pt(e)
        });


        const r =
            stage.getBoundingClientRect();


        const d =
            document.createElement(
                'div'
            );


        d.className =
            'rip';


        d.style.left =
            (
                e.clientX -
                r.left
            )
            + 'px';


        d.style.top =
            (
                e.clientY -
                r.top
            )
            + 'px';


        stage.append(
            d
        );


        d.onanimationend =
            () =>
                d.remove();

    }
);


/* ========================================================================= */
/* MOUSE UP                                                                  */
/* ========================================================================= */

cv.addEventListener(
    'mouseup',
    e => {

        send({
            type:
                'mouse',
            action:
                'up',
            button:
                e.button,
            clicks:
                e.detail || 1,
            ...pt(e)
        });

    }
);


/* ========================================================================= */
/* WHEEL                                                                     */
/* ========================================================================= */

cv.addEventListener(
    'wheel',
    e => {

        e.preventDefault();

        send({
            type:
                'mouse',
            action:
                'wheel',
            dx:
                e.deltaX,
            dy:
                e.deltaY,
            ...pt(e)
        });

    },
    {
        passive:
            false
    }
);


/* ========================================================================= */
/* CONTEXT MENU                                                              */
/* ========================================================================= */

cv.addEventListener(
    'contextmenu',
    e => {

        e.preventDefault();

    }
);


/* ========================================================================= */
/* KEYBOARD                                                                  */
/* ========================================================================= */

const mod =
    e =>
        e.ctrlKey ||
        e.metaKey;


cv.addEventListener(
    'keydown',
    e => {

        const key =
            e.key.toLowerCase();


        /*
         * Ctrl/Cmd + V
         * Let paste event handle it.
         */

        if (
            mod(e)
            &&
            key === 'v'
        ) {

            return;
        }


        /*
         * Ctrl/Cmd + L
         * F6
         */

        if (
            (
                mod(e)
                &&
                key === 'l'
            )
            ||
            e.key === 'F6'
        ) {

            e.preventDefault();

            url.focus();

            return;
        }


        /*
         * Ctrl/Cmd + R
         * F5
         */

        if (
            (
                mod(e)
                &&
                key === 'r'
            )
            ||
            e.key === 'F5'
        ) {

            e.preventDefault();

            send({
                type:
                    'reload'
            });

            return;
        }


        /*
         * Alt + Left
         */

        if (
            e.altKey
            &&
            e.key ===
                'ArrowLeft'
        ) {

            e.preventDefault();

            send({
                type:
                    'back'
            });

            return;
        }


        /*
         * Alt + Right
         */

        if (
            e.altKey
            &&
            e.key ===
                'ArrowRight'
        ) {

            e.preventDefault();

            send({
                type:
                    'forward'
            });

            return;
        }


        e.preventDefault();

        send({
            type:
                'key',
            action:
                'down',
            key:
                e.key
        });

    }
);


/* ========================================================================= */
/* KEYBOARD UP                                                               */
/* ========================================================================= */

cv.addEventListener(
    'keyup',
    e => {

        if (
            mod(e)
            &&
            e.key.toLowerCase()
                === 'v'
        ) {

            return;
        }

        e.preventDefault();

        send({
            type:
                'key',
            action:
                'up',
            key:
                e.key
        });

    }
);


/* ========================================================================= */
/* PASTE                                                                     */
/* ========================================================================= */

cv.addEventListener(
    'paste',
    e => {

        e.preventDefault();

        const text =
            e.clipboardData.getData(
                'text'
            );

        if (text) {

            send({
                type:
                    'text',
                text:
                    text
            });
        }

    }
);


/* ========================================================================= */
/* RESIZE                                                                    */
/* ========================================================================= */

let resizeTimer =
    null;


addEventListener(
    'resize',
    () => {

        clearTimeout(
            resizeTimer
        );

        resizeTimer =
            setTimeout(
                () => {

                    const {
                        w,
                        h
                    } = size();


                    send({
                        type:
                            'resize',
                        w,
                        h
                    });

                },
                250
            );

    }
);


/* ========================================================================= */
/* START                                                                     */
/* ========================================================================= */

connect();

</script>

</body>

</html>
"""


# =============================================================================
# LOGIN HTML
# =============================================================================

LOGIN_HTML = r"""<!doctype html>

<html
    lang="en"
    data-theme="dark"
>

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>
Sign in – PyBrowser
</title>

<style>

:root{
    --bg:#0e1015;
    --bar:#161922e6;
    --chip:#232837;
    --chip2:#2f3548;
    --fg:#e9ebf2;
    --mut:#8a92a8;
    --acc:#7c9cff;
    --acc2:#b57cff;
    --ok:#4ade80;
    --bad:#f87171;
    --sh:0 10px 34px #0007;
}

:root[data-theme=light]{
    --bg:#eceff5;
    --bar:#ffffffe6;
    --chip:#e5e8f0;
    --chip2:#d6dbe8;
    --fg:#1a1e2b;
    --mut:#657090;
    --sh:0 10px 34px #0002;
}

*{
    box-sizing:border-box;
}

html,
body{
    height:100%;
    margin:0;
    background:var(--bg);
    color:var(--fg);
    font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;
    overflow:hidden;
}

body{
    display:grid;
    place-items:center;
}

.blob{
    position:absolute;
    width:420px;
    height:420px;
    border-radius:50%;
    filter:blur(80px);
    opacity:.35;
    animation:
        float 9s
        ease-in-out
        infinite alternate;
}

.b1{
    background:var(--acc);
    left:8%;
    top:6%;
}

.b2{
    background:var(--acc2);
    right:8%;
    bottom:4%;
    animation-delay:-4s;
}

@keyframes float{

    to{
        transform:
            translate(60px,-40px)
            scale(1.2);
    }

}

#card{
    position:relative;
    width:min(380px,92vw);
    padding:34px 28px 28px;
    border-radius:22px;
    background:var(--bar);
    backdrop-filter:blur(16px);
    border:1px solid #ffffff1a;
    box-shadow:var(--sh);
    text-align:center;
    animation:
        rise .7s
        cubic-bezier(.2,.9,.3,1)
        both;
}

@keyframes rise{

    from{
        transform:translateY(26px);
        opacity:0;
    }

}

#card.shake{
    animation:
        shake .45s;
}

@keyframes shake{

    20%,
    60%{
        transform:translateX(-9px);
    }

    40%,
    80%{
        transform:translateX(9px);
    }

}

.logo{
    display:block;
    width:84px;
    height:84px;
    margin:0 auto 18px;
}

p.s{
    margin:
        0 0 22px;
    color:var(--mut);
}

.f{
    position:relative;
    margin-bottom:12px;
    text-align:left;
}

.f input{
    width:100%;
    height:46px;
    padding:
        0 44px 0 14px;
    border-radius:12px;
    border:1.5px solid transparent;
    background:var(--chip);
    color:var(--fg);
    font:inherit;
    outline:0;
    transition:
        border-color .25s,
        box-shadow .25s,
        background .25s;
}

.f input:focus{
    border-color:var(--acc);
    box-shadow:
        0 0 0 4px #7c9cff29;
    background:var(--bg);
}

.f input::placeholder{
    color:var(--mut);
}

#eye{
    position:absolute;
    right:6px;
    top:5px;
    width:36px;
    height:36px;
    border:0;
    border-radius:50%;
    background:transparent;
    color:var(--mut);
    cursor:pointer;
}

#eye:hover{
    color:var(--fg);
    background:var(--chip2);
}

#eye svg{
    width:19px;
    height:19px;
    fill:none;
    stroke:currentColor;
    stroke-width:2;
    stroke-linecap:round;
    stroke-linejoin:round;
}

#go{
    width:100%;
    height:46px;
    margin-top:6px;
    border:0;
    border-radius:12px;
    font:inherit;
    font-weight:600;
    color:#fff;
    cursor:pointer;
    position:relative;
    background:
        linear-gradient(
            135deg,
            var(--acc),
            var(--acc2)
        );
}

#go:disabled{
    cursor:default;
}

#go.busy span{
    opacity:0;
}

#go.busy::after{
    content:"";
    position:absolute;
    left:50%;
    top:50%;
    width:20px;
    height:20px;
    margin:-10px;
    border-radius:50%;
    border:3px solid #fff5;
    border-top-color:#fff;
    animation:
        rot .7s
        linear
        infinite;
}

#go.ok{
    background:var(--ok);
}

@keyframes rot{

    to{
        transform:rotate(360deg);
    }

}

#err{
    min-height:20px;
    margin-top:12px;
    color:var(--bad);
    font-size:13px;
}

</style>

</head>

<body>

<div class="blob b1"></div>
<div class="blob b2"></div>

<form
    id="card"
    autocomplete="on"
>

<svg
    class="logo"
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
    stop-color="#7c9cff"
/>

<stop
    offset="1"
    stop-color="#b57cff"
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
    stroke="#fff"
    stroke-width="4"
/>

<ellipse
    cx="48"
    cy="48"
    rx="11"
    ry="26"
    fill="none"
    stroke="#fff"
    stroke-width="4"
/>

<path
    d="M22 48h52M27 34h42M27 62h42"
    stroke="#fff"
    stroke-width="3.5"
    stroke-linecap="round"
    fill="none"
/>

<path
    d="M58 56l22 8-9 3-3 9z"
    fill="#fff"
    stroke="#7c9cff"
    stroke-width="2.5"
    stroke-linejoin="round"
/>

</svg>

<p class="s">
Sign in to start browsing
</p>

<div class="f">

<input
    id="u"
    name="username"
    placeholder="Username"
    autocomplete="username"
    autofocus
    required
>

</div>

<div class="f">

<input
    id="p"
    name="password"
    type="password"
    placeholder="Password"
    autocomplete="current-password"
    required
>

<button
    type="button"
    id="eye"
    title="Show password"
>

<svg viewBox="0 0 24 24">

<path
    d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"
/>

<circle
    cx="12"
    cy="12"
    r="3"
/>

</svg>

</button>

</div>

<button
    id="go"
    type="submit"
>

<span>
Sign in
</span>

</button>

<div
    id="err"
    role="alert"
></div>

</form>

<script>

const $ =
    id =>
        document.getElementById(id);

const card =
    $('card');

const u =
    $('u');

const p =
    $('p');

const go =
    $('go');

const err =
    $('err');


try {

    const theme =
        localStorage.getItem(
            'pb-theme'
        );

    if (theme) {

        document.documentElement
            .dataset.theme =
            theme;
    }

} catch (e) {}


$('eye').onclick =
    () => {

        p.type =
            p.type === 'password'
                ? 'text'
                : 'password';

        p.focus();
    };


function fail(
    message
) {

    err.textContent =
        message;

    card.classList.remove(
        'shake'
    );

    void card.offsetWidth;

    card.classList.add(
        'shake'
    );

    go.classList.remove(
        'busy'
    );

    go.disabled =
        false;

    p.select();
}


card.onsubmit =
    async event => {

        event.preventDefault();

        err.textContent =
            '';

        go.classList.add(
            'busy'
        );

        go.disabled =
            true;


        try {

            const response =
                await fetch(
                    '/login',
                    {
                        method:
                            'POST',

                        headers: {
                            'Content-Type':
                                'application/json'
                        },

                        body:
                            JSON.stringify(
                                {
                                    username:
                                        u.value,

                                    password:
                                        p.value
                                }
                            )
                    }
                );


            if (
                response.ok
            ) {

                go.classList.remove(
                    'busy'
                );

                go.classList.add(
                    'ok'
                );

                go.firstChild.textContent =
                    'Welcome ✓';


                setTimeout(
                    () =>
                        location.reload(),
                    400
                );

                return;
            }


            const data =
                await response
                    .json()
                    .catch(
                        () => ({})
                    );


            fail(
                data.error
                ||
                'Sign-in failed'
            );

        } catch (e) {

            fail(
                'Network error. Please try again.'
            );
        }
    };

</script>

</body>

</html>
"""


# =============================================================================
# LOCAL / RENDER START
# =============================================================================

if __name__ == "__main__":

    import uvicorn

    log.info(
        "Launching Uvicorn on %s:%s",
        HOST,
        PORT,
    )

    uvicorn.run(
        "app:app",
        host=HOST,
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
