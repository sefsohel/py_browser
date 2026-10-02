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
# Start nginx (background)
# ---------------------------------------------------------

echo "[PyBrowser] Starting nginx on Railway PORT=$PORT..."

nginx -g 'daemon off;' &

# ---------------------------------------------------------
# Background monitoring loop
# ---------------------------------------------------------

(
    while true; do

        if ! pgrep -x Xvfb >/dev/null; then
            echo "[PyBrowser] Xvfb stopped. Exiting."
            exit 1
        fi

        if ! pgrep -x x11vnc >/dev/null; then
            echo "[PyBrowser] x11vnc stopped. Exiting."
            exit 1
        fi

        if ! pgrep -x websockify >/dev/null; then
            echo "[PyBrowser] websockify stopped. Exiting."
            exit 1
        fi

        if ! pgrep -x nginx >/dev/null; then
            echo "[PyBrowser] nginx stopped. Exiting."
            exit 1
        fi

        if ! pgrep -x chromium >/dev/null; then
            echo "[PyBrowser] Chromium stopped, restarting..."
            start_chromium
        fi

        sleep 10

    done
) &

# ---------------------------------------------------------
# Run Gunicorn in foreground (main process)
# ---------------------------------------------------------

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
