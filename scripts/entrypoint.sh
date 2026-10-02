#!/usr/bin/env bash
set -Eeuo pipefail

DISPLAY="${DISPLAY:-:99}"
PORT="${PORT:-8080}"
APP_PORT="${APP_PORT:-8000}"

DATA_DIR="${BROWSER_DATA_DIR:-/data/chromium}"
DOWNLOAD_DIR="${BROWSER_DOWNLOAD_DIR:-/data/downloads}"

VNC_PASSWORD="${VNC_PASSWORD:-}"

echo "[PyBrowser] Starting PyBrowser..."
echo "[PyBrowser] DISPLAY=$DISPLAY"
echo "[PyBrowser] PORT=$PORT"
echo "[PyBrowser] APP_PORT=$APP_PORT"

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
# Generate nginx configuration
# ---------------------------------------------------------

echo "[PyBrowser] Generating nginx configuration..."

export PORT
export APP_PORT

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
# ---------------------------------------------------------

echo "[PyBrowser] Starting Xvfb..."

Xvfb "$DISPLAY" \
    -screen 0 1920x1080x24 \
    -ac \
    -nolisten tcp \
    +extension GLX \
    +extension RANDR \
    -noreset \
    >/tmp/xvfb.log 2>&1 &

sleep 2

if ! pgrep -x Xvfb >/dev/null; then

    echo "[PyBrowser] ERROR: Xvfb failed to start."

    cat /tmp/xvfb.log 2>/dev/null || true

    exit 1

fi

echo "[PyBrowser] Xvfb started."

# ---------------------------------------------------------
# Start Openbox
# ---------------------------------------------------------

echo "[PyBrowser] Starting Openbox..."

su -s /bin/bash browser -c "
    export DISPLAY='$DISPLAY'
    export HOME=/home/browser

    openbox-session
" >/tmp/openbox.log 2>&1 &

sleep 1

# ---------------------------------------------------------
# Start Chromium
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
            https://www.google.com
    " >/tmp/chromium.log 2>&1 &

}

start_chromium

sleep 3

# ---------------------------------------------------------
# Start x11vnc
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

sleep 2

if ! pgrep -x x11vnc >/dev/null; then

    echo "[PyBrowser] ERROR: x11vnc failed to start."

    cat /tmp/x11vnc.log 2>/dev/null || true

    exit 1

fi

echo "[PyBrowser] x11vnc started."

# ---------------------------------------------------------
# Start noVNC / WebSocket bridge
# ---------------------------------------------------------

echo "[PyBrowser] Starting websockify..."

websockify \
    --web=/usr/share/novnc \
    --heartbeat=30 \
    6080 127.0.0.1:5900 \
    >/tmp/websockify.log 2>&1 &

sleep 2

if ! pgrep -x websockify >/dev/null; then

    echo "[PyBrowser] ERROR: websockify failed to start."

    cat /tmp/websockify.log 2>/dev/null || true

    exit 1

fi

echo "[PyBrowser] websockify started."

# ---------------------------------------------------------
# Validate nginx configuration
# ---------------------------------------------------------

echo "[PyBrowser] Testing nginx configuration..."

if ! nginx -t; then

    echo "[PyBrowser] ERROR: nginx configuration is invalid."

    exit 1

fi

echo "[PyBrowser] nginx configuration is valid."

# ---------------------------------------------------------
# Start nginx
# ---------------------------------------------------------

echo "[PyBrowser] Starting nginx..."

nginx -g 'daemon off;' >/tmp/nginx.log 2>&1 &

sleep 2

if ! pgrep -x nginx >/dev/null; then

    echo "[PyBrowser] ERROR: nginx failed to start."

    cat /tmp/nginx.log 2>/dev/null || true

    exit 1

fi

echo "[PyBrowser] nginx started."

# ---------------------------------------------------------
# Wait for Gunicorn
# ---------------------------------------------------------

echo "[PyBrowser] Starting Gunicorn on 0.0.0.0:$APP_PORT..."

GUNICORN_READY=0

# Start Gunicorn in background temporarily so we can verify it.
su -s /bin/bash browser -c "
    cd /app

    exec gunicorn \
        --workers 1 \
        --threads 2 \
        --bind 0.0.0.0:$APP_PORT \
        --access-logfile - \
        --error-logfile - \
        app:app
" >/tmp/gunicorn.log 2>&1 &

GUNICORN_PID=$!

echo "[PyBrowser] Gunicorn PID: $GUNICORN_PID"

# ---------------------------------------------------------
# Gunicorn readiness check
# ---------------------------------------------------------

for i in {1..30}; do

    if (echo > /dev/tcp/127.0.0.1/"$APP_PORT") \
        >/dev/null 2>&1; then

        echo "[PyBrowser] Gunicorn is ready on port $APP_PORT"

        GUNICORN_READY=1

        break

    fi

    if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then

        echo "[PyBrowser] Gunicorn process exited."

        echo "[PyBrowser] Gunicorn logs:"

        cat /tmp/gunicorn.log 2>/dev/null || true

        exit 1

    fi

    echo "[PyBrowser] Waiting for Gunicorn... ($i/30)"

    sleep 1

done

if [ "$GUNICORN_READY" -eq 0 ]; then

    echo "[PyBrowser] Gunicorn failed to become ready."

    echo "[PyBrowser] Last Gunicorn logs:"

    tail -n 100 /tmp/gunicorn.log 2>/dev/null || true

    exit 1

fi

# ---------------------------------------------------------
# Background monitoring
# ---------------------------------------------------------

monitor_services() {

    while true; do

        # ---------------------------------------------
        # Xvfb
        # ---------------------------------------------

        if ! pgrep -x Xvfb >/dev/null; then

            echo "[PyBrowser] Xvfb stopped."

            exit 1

        fi

        # ---------------------------------------------
        # x11vnc
        # ---------------------------------------------

        if ! pgrep -x x11vnc >/dev/null; then

            echo "[PyBrowser] x11vnc stopped."

            exit 1

        fi

        # ---------------------------------------------
        # websockify
        # ---------------------------------------------

        if ! pgrep -x websockify >/dev/null; then

            echo "[PyBrowser] websockify stopped."

            exit 1

        fi

        # ---------------------------------------------
        # nginx
        # ---------------------------------------------

        if ! pgrep -x nginx >/dev/null; then

            echo "[PyBrowser] nginx stopped."

            exit 1

        fi

        # ---------------------------------------------
        # Chromium
        # ---------------------------------------------

        if ! pgrep -x chromium >/dev/null; then

            echo "[PyBrowser] Chromium stopped."

            echo "[PyBrowser] Restarting Chromium..."

            start_chromium

        fi

        # ---------------------------------------------
        # Gunicorn
        # ---------------------------------------------

        if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then

            echo "[PyBrowser] Gunicorn stopped."

            echo "[PyBrowser] Gunicorn logs:"

            tail -n 100 /tmp/gunicorn.log 2>/dev/null || true

            exit 1

        fi

        sleep 10

    done

}

monitor_services &

MONITOR_PID=$!

# ---------------------------------------------------------
# Keep entrypoint alive
#
# Gunicorn remains the primary application process.
# ---------------------------------------------------------

echo "[PyBrowser] ========================================"
echo "[PyBrowser] PyBrowser is READY"
echo "[PyBrowser] Railway PORT: $PORT"
echo "[PyBrowser] Gunicorn: 127.0.0.1:$APP_PORT"
echo "[PyBrowser] noVNC: 127.0.0.1:6080"
echo "[PyBrowser] VNC: 127.0.0.1:5900"
echo "[PyBrowser] ========================================"

# Wait for Gunicorn.
# If Gunicorn exits, the container exits and Railway can restart it.

wait "$GUNICORN_PID"

GUNICORN_EXIT_CODE=$?

echo "[PyBrowser] Gunicorn exited with code $GUNICORN_EXIT_CODE"

exit "$GUNICORN_EXIT_CODE"
