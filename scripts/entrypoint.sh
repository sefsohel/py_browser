#!/usr/bin/env bash
set -Eeuo pipefail

DISPLAY="${DISPLAY:-:99}"
PORT="${PORT:-8080}"
APP_PORT="${APP_PORT:-8000}"
DATA_DIR="${BROWSER_DATA_DIR:-/data/chromium}"
DOWNLOAD_DIR="${BROWSER_DOWNLOAD_DIR:-/data/downloads}"
VNC_PASSWORD="${VNC_PASSWORD:-}"

mkdir -p /data "$DATA_DIR" "$DOWNLOAD_DIR" /tmp/.X11-unix
chown -R browser:browser /data
chmod 1777 /tmp/.X11-unix

# x11vnc's classic VNC authentication stores up to 8 password characters.
if [[ -z "$VNC_PASSWORD" ]]; then
    VNC_PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 8)"
    echo "[PyBrowser] VNC_PASSWORD was not supplied; generated password: $VNC_PASSWORD"
else
    VNC_PASSWORD="${VNC_PASSWORD:0:8}"
    echo "[PyBrowser] Using VNC_PASSWORD (first 8 characters are used)."
fi

VNC_PASS_FILE=/data/.vnc-password

x11vnc \
    -storepasswd "$VNC_PASSWORD" "$VNC_PASS_FILE" >/dev/null

chmod 600 "$VNC_PASS_FILE"

# Create nginx config from the template using Railway's injected PORT.
export PORT APP_PORT

envsubst '${PORT} ${APP_PORT}' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/nginx.conf

cleanup() {
    echo "[PyBrowser] Shutting down..."
    jobs -pr | xargs -r kill 2>/dev/null || true
}

trap cleanup EXIT INT TERM

# ---------------------------------------------------------
# Virtual display
# ---------------------------------------------------------

Xvfb "$DISPLAY" \
    -screen 0 1920x1080x24 \
    -ac \
    -nolisten tcp \
    +extension GLX \
    +extension RANDR \
    -noreset \
    >/tmp/xvfb.log 2>&1 &

sleep 1

# ---------------------------------------------------------
# Window manager
# ---------------------------------------------------------

su -s /bin/bash browser -c "
    export DISPLAY='$DISPLAY'
    export HOME=/home/browser
    openbox-session >/tmp/openbox.log 2>&1
" &

# ---------------------------------------------------------
# Chromium
# ---------------------------------------------------------

start_chromium() {
    echo "[PyBrowser] Starting Chromium..."

    su -s /bin/bash browser -c "
        export DISPLAY='$DISPLAY'
        export HOME=/home/browser

        mkdir -p '$DATA_DIR' '$DOWNLOAD_DIR'

        exec chromium \
          --start-maximized \
          --window-size=1920,1080 \
          --no-first-run \
          --no-default-browser-check \
          --disable-dev-shm-usage \
          --disable-gpu \
          --disable-session-crashed-bubble \
          --password-store=basic \
          --user-data-dir='$DATA_DIR' \
          --disk-cache-dir='$DATA_DIR/cache' \
          https://www.google.com \
          >/tmp/chromium.log 2>&1
    " &
}

start_chromium

# ---------------------------------------------------------
# VNC server
# ---------------------------------------------------------

x11vnc \
    -display "$DISPLAY" \
    -rfbport 5900 \
    -rfbauth "$VNC_PASS_FILE" \
    -forever \
    -shared \
    -noxdamage \
    -repeat \
    -cursor arrow \
    -wait 5 \
    -defer 5 \
    >/tmp/x11vnc.log 2>&1 &

# ---------------------------------------------------------
# noVNC / WebSocket bridge
# ---------------------------------------------------------

websockify \
    --web=/usr/share/novnc \
    --heartbeat=30 \
    6080 127.0.0.1:5900 \
    >/tmp/websockify.log 2>&1 &

# ---------------------------------------------------------
# Python 3.11 control / health service
# ---------------------------------------------------------

su -s /bin/bash browser -c "
    cd /app

    gunicorn \
      --workers 1 \
      --threads 2 \
      --bind 0.0.0.0:$APP_PORT \
      app:app
" >/tmp/gunicorn.log 2>&1 &

# ---------------------------------------------------------
# Wait for nginx configuration
# ---------------------------------------------------------

until nginx -t >/dev/null 2>&1; do
    sleep 1
done

# ---------------------------------------------------------
# Start nginx
# ---------------------------------------------------------

nginx -g 'daemon off;' &

# ---------------------------------------------------------
# Keep PID 1 alive
# ---------------------------------------------------------

while true; do

    if ! kill -0 "$(pgrep -o Xvfb || echo 0)" 2>/dev/null; then
        echo "[PyBrowser] Xvfb stopped. Exiting for Railway restart."
        exit 1
    fi

    if ! pgrep -x x11vnc >/dev/null; then
        echo "[PyBrowser] x11vnc stopped. Exiting for Railway restart."
        exit 1
    fi

    if ! pgrep -x websockify >/dev/null; then
        echo "[PyBrowser] websockify stopped. Exiting for Railway restart."
        exit 1
    fi

    if ! pgrep -x chromium >/dev/null; then
        echo "[PyBrowser] Chromium stopped; restarting browser..."
        start_chromium
    fi

    if ! pgrep -x nginx >/dev/null; then
        echo "[PyBrowser] nginx stopped. Exiting for Railway restart."
        exit 1
    fi

    sleep 10

done
