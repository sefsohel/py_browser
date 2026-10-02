#!/usr/bin/env bash

set -e

# =========================================================
# PyBrowser Entrypoint
# =========================================================

export DISPLAY="${DISPLAY:-:99}"
export PORT="${PORT:-8080}"
export APP_PORT="${APP_PORT:-8000}"

export BROWSER_DATA_DIR="${BROWSER_DATA_DIR:-/data/chromium}"
export BROWSER_DOWNLOAD_DIR="${BROWSER_DOWNLOAD_DIR:-/data/downloads}"

echo "========================================================="
echo "[PyBrowser] Starting PyBrowser..."
echo "[PyBrowser] DISPLAY=$DISPLAY"
echo "[PyBrowser] Railway PORT=$PORT"
echo "[PyBrowser] APP_PORT=$APP_PORT"
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
# Generate VNC password
# =========================================================

VNC_PASSWORD_FILE="/data/.vnc-password"

if [ ! -f "$VNC_PASSWORD_FILE" ]; then

    echo "[PyBrowser] Creating VNC password..."

    # Generate an 8-character random password
    VNC_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 8)"

    echo "$VNC_PASSWORD" > "$VNC_PASSWORD_FILE"

    chmod 600 "$VNC_PASSWORD_FILE"

else

    echo "[PyBrowser] Existing VNC password found."

fi

VNC_PASSWORD="$(cat "$VNC_PASSWORD_FILE")"


# =========================================================
# Generate nginx configuration
# =========================================================

echo "[PyBrowser] Generating nginx configuration..."

export PORT
export APP_PORT

envsubst '${PORT} ${APP_PORT}' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf


# =========================================================
# Clean old processes / display lock
# =========================================================

echo "[PyBrowser] Cleaning old X11 locks..."

rm -f \
    /tmp/.X99-lock \
    /tmp/.X11-unix/X99


# =========================================================
# Start Xvfb
# =========================================================

echo "[PyBrowser] Starting Xvfb..."

Xvfb "$DISPLAY" \
    -screen 0 1920x1080x24 \
    -ac \
    +extension GLX \
    +render \
    -noreset \
    > /tmp/xvfb.log 2>&1 &

XVFB_PID=$!

echo "[PyBrowser] Xvfb PID=$XVFB_PID"


# =========================================================
# Wait for X server
# =========================================================

echo "[PyBrowser] Waiting for X server..."

for i in $(seq 1 30); do

    if [ -S "/tmp/.X11-unix/X99" ]; then
        echo "[PyBrowser] X server is ready."
        break
    fi

    sleep 1

done


if [ ! -S "/tmp/.X11-unix/X99" ]; then
    echo "[PyBrowser] ERROR: X server failed to start."
    cat /tmp/xvfb.log || true
    exit 1
fi


# =========================================================
# Start Openbox
# =========================================================

echo "[PyBrowser] Starting Openbox..."

su -s /bin/bash browser -c "
    export DISPLAY=$DISPLAY
    openbox-session
" > /tmp/openbox.log 2>&1 &

OPENBOX_PID=$!

echo "[PyBrowser] Openbox PID=$OPENBOX_PID"


# =========================================================
# Start Chromium
# =========================================================

echo "[PyBrowser] Starting Chromium..."

su -s /bin/bash browser -c "
    export DISPLAY=$DISPLAY

    chromium \
        --display=$DISPLAY \
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
" > /tmp/chromium.log 2>&1 &

CHROMIUM_PID=$!

echo "[PyBrowser] Chromium PID=$CHROMIUM_PID"


# =========================================================
# Start x11vnc
# =========================================================

echo "[PyBrowser] Starting x11vnc..."

x11vnc \
    -display "$DISPLAY" \
    -forever \
    -shared \
    -rfbport 5900 \
    -rfbauth "$VNC_PASSWORD_FILE" \
    -noxdamage \
    -repeat \
    -xkb \
    > /tmp/x11vnc.log 2>&1 &

X11VNC_PID=$!

echo "[PyBrowser] x11vnc PID=$X11VNC_PID"


# =========================================================
# Start websockify / noVNC backend
# =========================================================

echo "[PyBrowser] Starting websockify..."

websockify \
    --web=/usr/share/novnc \
    6080 \
    127.0.0.1:5900 \
    > /tmp/websockify.log 2>&1 &

WEBSOCKIFY_PID=$!

echo "[PyBrowser] websockify PID=$WEBSOCKIFY_PID"


# =========================================================
# Validate nginx configuration
# =========================================================

echo "[PyBrowser] Validating nginx configuration..."

until nginx -t >/dev/null 2>&1; do
    echo "[PyBrowser] Waiting for valid nginx configuration..."
    sleep 1
done

echo "[PyBrowser] nginx configuration is valid."


# =========================================================
# Start nginx
# =========================================================

echo "[PyBrowser] Starting nginx on Railway PORT=$PORT..."

nginx -g 'daemon off;' &

NGINX_PID=$!

echo "[PyBrowser] nginx PID=$NGINX_PID"


# =========================================================
# Background service monitoring
# =========================================================

(
    while true; do

        # -------------------------------------------------
        # Xvfb
        # -------------------------------------------------

        if ! kill -0 "$XVFB_PID" 2>/dev/null; then

            echo "[PyBrowser] ERROR: Xvfb stopped."

            exit 1

        fi


        # -------------------------------------------------
        # x11vnc
        # -------------------------------------------------

        if ! kill -0 "$X11VNC_PID" 2>/dev/null; then

            echo "[PyBrowser] WARNING: x11vnc stopped."
            echo "[PyBrowser] Restarting x11vnc..."

            x11vnc \
                -display "$DISPLAY" \
                -forever \
                -shared \
                -rfbport 5900 \
                -rfbauth "$VNC_PASSWORD_FILE" \
                -noxdamage \
                -repeat \
                -xkb \
                > /tmp/x11vnc.log 2>&1 &

            X11VNC_PID=$!

            echo "[PyBrowser] New x11vnc PID=$X11VNC_PID"

        fi


        # -------------------------------------------------
        # websockify
        # -------------------------------------------------

        if ! kill -0 "$WEBSOCKIFY_PID" 2>/dev/null; then

            echo "[PyBrowser] WARNING: websockify stopped."
            echo "[PyBrowser] Restarting websockify..."

            websockify \
                --web=/usr/share/novnc \
                6080 \
                127.0.0.1:5900 \
                > /tmp/websockify.log 2>&1 &

            WEBSOCKIFY_PID=$!

            echo "[PyBrowser] New websockify PID=$WEBSOCKIFY_PID"

        fi


        # -------------------------------------------------
        # nginx
        # -------------------------------------------------

        if ! kill -0 "$NGINX_PID" 2>/dev/null; then

            echo "[PyBrowser] ERROR: nginx stopped."

            exit 1

        fi


        # -------------------------------------------------
        # Chromium
        # -------------------------------------------------

        if ! kill -0 "$CHROMIUM_PID" 2>/dev/null; then

            echo "[PyBrowser] WARNING: Chromium stopped."
            echo "[PyBrowser] Restarting Chromium..."

            su -s /bin/bash browser -c "
                export DISPLAY=$DISPLAY

                chromium \
                    --display=$DISPLAY \
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
            " > /tmp/chromium.log 2>&1 &

            CHROMIUM_PID=$!

            echo "[PyBrowser] New Chromium PID=$CHROMIUM_PID"

        fi


        sleep 5

    done

) &

MONITOR_PID=$!

echo "[PyBrowser] Monitor PID=$MONITOR_PID"


# =========================================================
# Final startup information
# =========================================================

echo ""
echo "========================================================="
echo "[PyBrowser] All background services started."
echo "[PyBrowser] Railway PORT      : $PORT"
echo "[PyBrowser] Internal APP_PORT: $APP_PORT"
echo "[PyBrowser] VNC port         : 5900"
echo "[PyBrowser] noVNC port       : 6080"
echo "[PyBrowser] Chromium display : $DISPLAY"
echo "========================================================="
echo ""


# =========================================================
# Start Python / Gunicorn
# =========================================================

echo "[PyBrowser] All services started. Running Gunicorn in foreground..."

exec su -s /bin/bash browser -c "
    cd /app

    exec gunicorn \
        --workers 1 \
        --threads 2 \
        --bind 0.0.0.0:$APP_PORT \
        --access-logfile - \
        --error-logfile - \
        app:app
"
