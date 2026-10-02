#!/usr/bin/env bash

set -eu

# =========================================================
# PyBrowser Entrypoint
# =========================================================

export DISPLAY="${DISPLAY:-:99}"
export PORT="${PORT:-8080}"
export APP_PORT="${APP_PORT:-8000}"

export BROWSER_DATA_DIR="${BROWSER_DATA_DIR:-/data/chromium}"
export BROWSER_DOWNLOAD_DIR="${BROWSER_DOWNLOAD_DIR:-/data/downloads}"

# Derive X11 socket/lock paths from $DISPLAY (":99" -> "99")
DISPLAY_NUM="${DISPLAY#:}"
DISPLAY_NUM="${DISPLAY_NUM%%.*}"
X_SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM}"
X_LOCK="/tmp/.X${DISPLAY_NUM}-lock"

VNC_PASSWORD_FILE="/data/.vnc-password"

log() { echo "[PyBrowser] $*"; }

# =========================================================
# Shutdown handling
# =========================================================
# tini (PID 1) forwards signals to this script. On SIGTERM/SIGINT
# or any exit, kill every background service we started.

cleanup() {
    trap - EXIT TERM INT
    log "Shutting down services..."
    # shellcheck disable=SC2046
    kill $(jobs -p) 2>/dev/null || true
}

trap cleanup EXIT
trap 'exit 143' TERM INT

echo "========================================================="
log "Starting PyBrowser..."
log "DISPLAY=$DISPLAY"
log "PORT=$PORT"
log "APP_PORT=$APP_PORT"
echo "========================================================="


# =========================================================
# Prepare directories
# =========================================================

mkdir -p \
    "$BROWSER_DATA_DIR" \
    "$BROWSER_DOWNLOAD_DIR" \
    /data \
    /tmp/.X11-unix

chown -R browser:browser \
    "$BROWSER_DATA_DIR" \
    "$BROWSER_DOWNLOAD_DIR" \
    /home/browser

chmod 1777 /tmp/.X11-unix


# =========================================================
# VNC password
# =========================================================
# x11vnc's -rfbauth needs an obfuscated file made by
# `x11vnc -storepasswd`, not plain text. VNC auth only uses the
# first 8 characters of a password.
#
# Priority:
#   1. VNC_PASSWORD env var (set it in Railway; recreated every boot)
#   2. Existing password file on the /data volume
#   3. Newly generated random password (printed once to the logs)

if [ -n "${VNC_PASSWORD:-}" ]; then

    if [ "${#VNC_PASSWORD}" -gt 8 ]; then
        log "WARNING: VNC_PASSWORD is longer than 8 characters; VNC only uses the first 8."
        VNC_PASSWORD="${VNC_PASSWORD:0:8}"
    fi

    log "Using VNC password from VNC_PASSWORD env var."
    x11vnc -storepasswd "$VNC_PASSWORD" "$VNC_PASSWORD_FILE" >/dev/null 2>&1
    chmod 600 "$VNC_PASSWORD_FILE"

elif [ ! -f "$VNC_PASSWORD_FILE" ]; then

    VNC_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 8 || true)"
    x11vnc -storepasswd "$VNC_PASSWORD" "$VNC_PASSWORD_FILE" >/dev/null 2>&1
    chmod 600 "$VNC_PASSWORD_FILE"

    echo "---------------------------------------------------------"
    log "Generated VNC password: $VNC_PASSWORD"
    log "(Shown once. Set VNC_PASSWORD env var to choose your own.)"
    echo "---------------------------------------------------------"

else

    log "Existing VNC password found."

fi

unset VNC_PASSWORD


# =========================================================
# nginx configuration
# =========================================================

log "Generating nginx configuration..."

envsubst '${PORT} ${APP_PORT}' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf

log "Validating nginx configuration..."

if ! nginx -t; then
    log "ERROR: invalid nginx configuration."
    exit 1
fi


# =========================================================
# Service start functions (reused by the monitor loop)
# =========================================================

start_openbox() {
    su -s /bin/bash browser -c "
        export DISPLAY='$DISPLAY'
        exec openbox-session
    " >> /tmp/openbox.log 2>&1 &
    OPENBOX_PID=$!
    log "Openbox PID=$OPENBOX_PID"
}

start_chromium() {
    # A persistent profile keeps lock files after a crash/redeploy,
    # which makes Chromium refuse to start. Remove them first.
    rm -f "$BROWSER_DATA_DIR"/Singleton{Lock,Socket,Cookie}

    su -s /bin/bash browser -c "
        export DISPLAY='$DISPLAY'

        exec chromium \
            --no-sandbox \
            --disable-setuid-sandbox \
            --start-maximized \
            --window-size=1920,1080 \
            --no-first-run \
            --no-default-browser-check \
            --disable-dev-shm-usage \
            --disable-gpu \
            --disable-session-crashed-bubble \
            --password-store=basic \
            --user-data-dir='$BROWSER_DATA_DIR' \
            --disk-cache-dir='$BROWSER_DATA_DIR/cache' \
            --download-default-directory='$BROWSER_DOWNLOAD_DIR' \
            --disable-features=Translate \
            https://www.google.com
    " >> /tmp/chromium.log 2>&1 &
    CHROMIUM_PID=$!
    log "Chromium PID=$CHROMIUM_PID"
}

start_x11vnc() {
    # -localhost: only websockify (same container) can reach raw VNC
    x11vnc \
        -display "$DISPLAY" \
        -forever \
        -shared \
        -localhost \
        -rfbport 5900 \
        -rfbauth "$VNC_PASSWORD_FILE" \
        -noxdamage \
        -repeat \
        -xkb \
        >> /tmp/x11vnc.log 2>&1 &
    X11VNC_PID=$!
    log "x11vnc PID=$X11VNC_PID"
}

start_websockify() {
    websockify \
        --web=/usr/share/novnc \
        127.0.0.1:6080 \
        127.0.0.1:5900 \
        >> /tmp/websockify.log 2>&1 &
    WEBSOCKIFY_PID=$!
    log "websockify PID=$WEBSOCKIFY_PID"
}

start_nginx() {
    nginx -g 'daemon off;' &
    NGINX_PID=$!
    log "nginx PID=$NGINX_PID"
}

start_gunicorn() {
    # Bound to localhost: only nginx needs to reach it.
    su -s /bin/bash browser -c "
        cd /app

        exec gunicorn \
            --workers 1 \
            --threads 2 \
            --bind 127.0.0.1:$APP_PORT \
            --access-logfile - \
            --error-logfile - \
            app:app
    " &
    GUNICORN_PID=$!
    log "Gunicorn PID=$GUNICORN_PID"
}


# =========================================================
# Start Xvfb
# =========================================================

log "Cleaning old X11 locks..."
rm -f "$X_LOCK" "$X_SOCKET"

log "Starting Xvfb..."

Xvfb "$DISPLAY" \
    -screen 0 1920x1080x24 \
    -ac \
    +extension GLX \
    +render \
    -noreset \
    > /tmp/xvfb.log 2>&1 &

XVFB_PID=$!
log "Xvfb PID=$XVFB_PID"

log "Waiting for X server..."

for _ in $(seq 1 30); do
    [ -S "$X_SOCKET" ] && break
    sleep 1
done

if [ ! -S "$X_SOCKET" ]; then
    log "ERROR: X server failed to start."
    cat /tmp/xvfb.log || true
    exit 1
fi

log "X server is ready."


# =========================================================
# Start the rest
# =========================================================

start_openbox
start_chromium
start_x11vnc
start_websockify
start_nginx
start_gunicorn

echo ""
echo "========================================================="
log "All services started."
log "Public PORT       : $PORT (nginx)"
log "Internal APP_PORT : $APP_PORT (gunicorn, localhost only)"
log "noVNC / VNC       : 6080 / 5900 (localhost only)"
echo "========================================================="
echo ""


# =========================================================
# Supervise (foreground)
# =========================================================
# Critical services (Xvfb, nginx, gunicorn) exiting ends the script,
# which stops the container so Railway restarts it.
# Non-critical services (Openbox, Chromium, x11vnc, websockify)
# are restarted in place.

while true; do

    kill -0 "$XVFB_PID" 2>/dev/null || { log "ERROR: Xvfb stopped."; exit 1; }
    kill -0 "$NGINX_PID" 2>/dev/null || { log "ERROR: nginx stopped."; exit 1; }
    kill -0 "$GUNICORN_PID" 2>/dev/null || { log "ERROR: Gunicorn stopped."; exit 1; }

    if ! kill -0 "$OPENBOX_PID" 2>/dev/null; then
        log "WARNING: Openbox stopped. Restarting..."
        start_openbox
    fi

    if ! kill -0 "$X11VNC_PID" 2>/dev/null; then
        log "WARNING: x11vnc stopped. Restarting..."
        start_x11vnc
    fi

    if ! kill -0 "$WEBSOCKIFY_PID" 2>/dev/null; then
        log "WARNING: websockify stopped. Restarting..."
        start_websockify
    fi

    if ! kill -0 "$CHROMIUM_PID" 2>/dev/null; then
        log "WARNING: Chromium stopped. Restarting..."
        start_chromium
    fi

    # sleep in the background + wait so signals are handled immediately
    sleep 5 &
    wait $! || true

done
