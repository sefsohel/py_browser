#!/usr/bin/env bash
set -euo pipefail

# Railway injects PORT; default for local runs. Login is handled inside the app
# (see app.py), and every variable has a built-in default.
export PORT="${PORT:-8080}"
export AUTH_BLOCK=""   # legacy placeholder: renders to nothing if an old template is still present

envsubst '${PORT} ${AUTH_BLOCK}' \
  < /etc/nginx/templates/default.conf.template \
  > /etc/nginx/conf.d/default.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t

# --- Virtual sound card so Chrome has somewhere to play audio (captured by app.py) ---
PA=""
if [[ "${AUDIO:-1}" != "0" ]] && command -v pulseaudio >/dev/null 2>&1; then
  mkdir -p /tmp/pa && chown pulsed /tmp/pa
  runuser -u pulsed -- env HOME=/tmp/pa XDG_RUNTIME_DIR=/tmp/pa \
    pulseaudio -n --daemonize=no --exit-idle-time=-1 --disallow-exit --use-pid-file=no --disable-shm=yes --log-target=stderr \
    -L "module-null-sink sink_name=pbsink" \
    -L "module-native-protocol-unix socket=/tmp/pulse-socket auth-anonymous=1" &
  PA=$!
  for _ in $(seq 1 50); do [[ -S /tmp/pulse-socket ]] && break; sleep 0.1; done
  export PULSE_SERVER=unix:/tmp/pulse-socket
fi

python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-access-log &
UV=$!
nginx -g 'daemon off; error_log /dev/stderr warn;' &
NG=$!

trap 'kill -TERM $UV $NG $PA 2>/dev/null || true' TERM INT

# If either process exits, stop the other and exit non-zero so Railway restarts us.
wait -n "$UV" "$NG" || true
kill -TERM "$UV" "$NG" $PA 2>/dev/null || true
wait || true
exit 1
