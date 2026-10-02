#!/usr/bin/env bash
set -euo pipefail

# Railway injects PORT; default for local runs. Login is handled inside the app
# (see app.py), and every variable has a built-in default.
export PORT="${PORT:-8080}"

envsubst '${PORT}' \
  < /etc/nginx/templates/default.conf.template \
  > /etc/nginx/conf.d/default.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t

python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-access-log &
UV=$!
nginx -g 'daemon off; error_log /dev/stderr warn;' &
NG=$!

trap 'kill -TERM $UV $NG 2>/dev/null || true' TERM INT

# If either process exits, stop the other and exit non-zero so Railway restarts us.
wait -n "$UV" "$NG" || true
kill -TERM "$UV" "$NG" 2>/dev/null || true
wait || true
exit 1
