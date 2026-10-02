#!/usr/bin/env bash
set -euo pipefail

export PORT="${PORT:-8080}"
USER_NAME="${BROWSER_USER:-admin}"

# A remote browser exposed to the internet must not be open to everyone.
if [[ -n "${BROWSER_PASSWORD:-}" ]]; then
  printf '%s:%s\n' "$USER_NAME" "$(openssl passwd -apr1 "$BROWSER_PASSWORD")" > /etc/nginx/.htpasswd
  export AUTH_BLOCK='auth_basic "PyBrowser"; auth_basic_user_file /etc/nginx/.htpasswd;'
elif [[ "${ALLOW_NO_AUTH:-false}" == "true" ]]; then
  echo "WARNING: running WITHOUT authentication (ALLOW_NO_AUTH=true)." >&2
  export AUTH_BLOCK=""
else
  echo "ERROR: set BROWSER_PASSWORD (or ALLOW_NO_AUTH=true to disable auth)." >&2
  exit 1
fi

envsubst '${PORT} ${AUTH_BLOCK}' \
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
