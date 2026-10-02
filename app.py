from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone

from flask import Flask, jsonify

app = Flask(__name__)


def process_running(name: str) -> bool:
    return shutil.which("pgrep") is not None and subprocess.run(
        ["pgrep", "-x", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


@app.get("/healthz")
def healthz():
    chromium_ok = process_running("chromium") or process_running("chromium-browse")
    x11vnc_ok = process_running("x11vnc")
    websockify_ok = process_running("websockify")

    # Railway health checks need a 2xx once the service is usable.
    healthy = chromium_ok and x11vnc_ok and websockify_ok
    return jsonify(
        {
            "status": "ok" if healthy else "starting",
            "python": "3.11",
            "chromium": chromium_ok,
            "x11vnc": x11vnc_ok,
            "websockify": websockify_ok,
            "time": datetime.now(timezone.utc).isoformat(),
        }
    ), 200 if healthy else 503


@app.get("/api/info")
def info():
    return jsonify(
        {
            "name": "PyBrowser Cloud",
            "engine": "Chromium",
            "transport": "VNC + WebSocket + noVNC",
            "python": "3.11",
            "display": os.getenv("DISPLAY", ":99"),
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", "8000"))
    app.run(host="127.0.0.1", port=port)
