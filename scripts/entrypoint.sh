#!/usr/bin/env bash
set -Eeuo pipefail

DISPLAY="${DISPLAY:-:99}"
PORT="${PORT:-8080}"
APP_PORT="${APP_PORT:-8000}"
DATA_DIR="${BROWSER_DATA_DIR:-/data/chromium}"
DOWNLOAD_DIR="${BROWSER_DOWNLOAD_DIR:-/data/downloads}"
VNC_PASSWORD="${VNC_PASSWORD:-}"

# ---------------------------------------------------------
# Prepare directories
# ---------------------------------------------------------

mkdir -p \
    /data \
    "$DATA_DIR" \
    "$DOWNLOAD_DIR" \
    /tmp/.X11-unix

chown -R browser:browser /data
chmod 1777 /tmp/.X11-unix

# ---------------------------------------------------------
# VNC password
# x11vnc classic authentication supports up to 8 chars.
# ---------------------------------------------------------

if [[ -z "$VNC_PASSWORD" ]]; then
    VNC_PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 8)"
    echo "[PyBrowser] VNC_PASSWORD was not supplied."
    echo "[PyBrowser] Generated VNC password: $VNC_PASSWORD"
else
    VNC_PASSWORD="${VNC_PASSWORD:0:8}"
    echo "[PyBrowser] Using supplied VNC_PASSWORD."
    echo "[PyBrowser] Only the first 8 characters are used."
fi

VNC_PASS_FILE="/data/.vnc-password"

x11vnc \
    -storepasswd "$VNC_PASSWORD" "$VNC_PASS_FILE" \
    >/dev/null

chmod 600 "$VNC_PASS_FILE"

# ---------------------------------------------------------
# Create nginx configuration
# Railway provides PORT automatically.
# ---------------------------------------------------------

export PORT APP_PORT

envsubst '${PORT} ${APP_PORT}' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/nginx.conf

# ---------------------------------------------------------
# Cleanup
# ---------------------------------------------------------

cleanup() {
    echo "[PyBrowser] Shutting down..."

    jobs -pr | xargs -r kill 2>/dev/null || true
}

trap cleanup EXIT INT TERM

# ---------------------------------------------------------
# Start Xvfb
# Virtual graphical display for Chromium.
# ---------------------------------------------------------

echo "[PyBrowser] Starting Xvfb on $DISPLAY..."

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
# Start Openbox
# Lightweight window manager.
# ---------------------------------------------------------

echo "[PyBrowser] Starting Openbox..."

su -s /bin/bash browser -c "
    export DISPLAY='$DISPLAY'
    export HOME=/home/browser

    openbox-session \
        >/tmp/openbox.log 2>&1
" &

# ---------------------------------------------------------
# Chromium
# ---------------------------------------------------------

start_chromium() {

    echo "[PyBrowser] Starting Chromium..."

    su -s /bin/bash browser -c "
        export DISPLAY='$DISPLAY'
        export HOME=/home/browser

        mkdir -p '$DATA_DIR'
        mkdir -p '$DOWNLOAD_DIR'
        mkdir -p '$DATA_DIR/cache'

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
          --no-sandbox \
          https://www.google.com \
          >/tmp/chromium.log 2>&1
    " &
}

start_chromium

# ---------------------------------------------------------
# Start x11vnc
# Exposes the actual Chromium/X11 desktop.
# ---------------------------------------------------------

echo "[PyBrowser] Starting x11vnc..."

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
# Start noVNC / WebSocket bridge
# ---------------------------------------------------------

echo "[PyBrowser] Starting websockify..."

websockify \
    --web=/usr/share/novnc \
    --heartbeat=30 \
    6080 127.0.0.1:5900 \
    >/tmp/websockify.log 2>&1 &

# ---------------------------------------------------------
# Start Python / Gunicorn
# ---------------------------------------------------------

echo "[PyBrowser] Starting Gunicorn on 0.0.0.0:$APP_PORT..."

su -s /bin/bash browser -c "
    cd /app

    exec gunicorn \
      --workers 1 \
      --threads 2 \
      --bind 0.0.0.0:$APP_PORT \
      app:app
" >/tmp/gunicorn.log 2>&1 &

# ---------------------------------------------------------
# Wait for Gunicorn to start and accept connections
#
# Uses Bash /dev/tcp, so netcat is NOT required.
# ---------------------------------------------------------

echo "[PyBrowser] Waiting for Gunicorn..."

GUNICORN_READY=0

for i in {1..30}; do

    if (echo > /dev/tcp/127.0.0.1/"$APP_PORT") \
        >/dev/null 2>&1; then

        echo "[PyBrowser] Gunicorn is ready on port $APP_PORT"

        GUNICORN_READY=1
        break
    fi

    echo "[PyBrowser] Waiting for gunicorn... ($i/30)"

    sleep 1
done

if [ "$GUNICORN_READY" -eq 0 ]; then

    echo "[PyBrowser] Gunicorn failed to start."

    echo "[PyBrowser] Last Gunicorn logs:"
    tail -n 100 /tmp/gunicorn.log 2>/dev/null || true

    exit 1
fi

# ---------------------------------------------------------
# Validate nginx configuration
# ---------------------------------------------------------

echo "[PyBrowser] Validating nginx configuration..."

until nginx -t >/dev/null 2>&1; do

    echo "[PyBrowser] Waiting for valid nginx configuration..."

    sleep 1
done

echo "[PyBrowser] nginx configuration is valid."

# ---------------------------------------------------------
# Start nginx
# ---------------------------------------------------------

echo "[PyBrowser] Starting nginx on Railway PORT=$PORT..."

nginx -g 'daemon off;' &

# ---------------------------------------------------------
# Monitor all important processes
# ---------------------------------------------------------

echo "[PyBrowser] PyBrowser is running."

while true; do

    # Xvfb
    if ! pgrep -x Xvfb >/dev/null; then
        echo "[PyBrowser] Xvfb stopped."
        echo "[PyBrowser] Exiting for Railway restart."
        exit 1
    fi

    # x11vnc
    if ! pgrep -x x11vnc >/dev/null; then
        echo "[PyBrowser] x11vnc stopped."
        echo "[PyBrowser] Exiting for Railway restart."
        exit 1
    fi

    # websockify
    if ! pgrep -x websockify >/dev/null; then
        echo "[PyBrowser] websockify stopped."
        echo "[PyBrowser] Exiting for Railway restart."
        exit 1
    fi

    # Chromium
    if ! pgrep -x chromium >/dev/null; then
        echo "[PyBrowser] Chromium stopped."
        echo "[PyBrowser] Restarting Chromium..."

        start_chromium
    fi

    # nginx
    if ! pgrep -x nginx >/dev/null; then
        echo "[PyBrowser] nginx stopped."
        echo "[PyBrowser] Exiting for Railway restart."
        exit 1
    fi

    # Gunicorn
    if ! pgrep -f "gunicorn.*app:app" >/dev/null 2>&1; then
        echo "[PyBrowser] Gunicorn stopped."
        echo "[PyBrowser] Exiting for Railway restart."
        exit 1
    fi

    sleep 10

done
