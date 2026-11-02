#!/usr/bin/env bash
# Run the whole product locally against your REAL Zepto / Blinkit / Swiggy / Zomato accounts, for a hands-on demo.
#
#   bash scripts/demo_local.sh you@example.com        # first run: sets up everything, makes you the admin
#   bash scripts/demo_local.sh you@example.com --lan  # also reachable from your phone on the same Wi-Fi
#   (later runs keep your data; add --reset to wipe it. Over plain http a phone browser cannot install the app to the
#   home screen or get push notifications; those need https, so they wait for the real domain.)
#
# What it starts: local Supabase, Mailpit (invite and alert emails: http://localhost:8025), the API (port 8000),
# the REAL worker (a real Chrome opens on this machine for each login), the scheduler and the PWA (port 3000).
# Open http://localhost:3000 and sign up with the email you passed. Ctrl-C stops everything.
#
# Nothing here is used by the tests (scripts/e2e_local.sh has its own ports and wipes its database).
# Your saved app logins go to .demo/vault, encrypted with a random key in .demo/master.key (both gitignored).
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
ADMIN_EMAIL=""; RESET=0; LAN=0
for a in "$@"; do
  case "$a" in
    --reset) RESET=1 ;;
    --lan) LAN=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) ADMIN_EMAIL="$a" ;;
  esac
done
[ -n "$ADMIN_EMAIL" ] || { echo "usage: bash scripts/demo_local.sh you@example.com [--lan] [--reset]" >&2; exit 2; }

PY="python"; command -v "$PY" >/dev/null 2>&1 || PY="python3"
HOST=127.0.0.1
API_PORT=8000
WEB_PORT=3000
SHOWN_HOST=localhost
if [ "$LAN" = 1 ]; then
  # Phone mode: listen on every interface and use this machine's Wi-Fi address, so a phone on the same Wi-Fi can open it.
  HOST=0.0.0.0
  SHOWN_HOST="$("$PY" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0])" 2>/dev/null || true)"
  [ -n "$SHOWN_HOST" ] || { echo "could not find this machine's Wi-Fi address" >&2; exit 1; }
fi
WEB_URL="http://$SHOWN_HOST:$WEB_PORT"
API_URL="http://$SHOWN_HOST:$API_PORT"
DEMO="$ROOT/.demo"
LOGS="$DEMO/logs"
SUPABASE="npx --yes supabase@2.122.0"
MAILPIT_NAME="ea-demo-mailpit"
PIDS=()
mkdir -p "$LOGS" "$DEMO/vault" "$DEMO/profiles"

log() { printf '\n== %s\n' "$*"; }
fail() { printf '\nDEMO FAILED: %s\n' "$*" >&2; exit 1; }
http_ok() { curl -fsS -o /dev/null --max-time 3 "$@"; }
wait_for() { # name seconds url
  local i; for i in $(seq 1 "$2"); do http_ok "$3" 2>/dev/null && return 0; sleep 1; done
  fail "$1 did not come up ($3); see $LOGS"
}

cleanup() {
  trap - EXIT INT TERM
  log "stopping"
  for p in "${PIDS[@]:-}"; do
    [ -n "$p" ] || continue
    if command -v taskkill >/dev/null 2>&1 && [ -r "/proc/$p/winpid" ]; then taskkill //PID "$(cat /proc/$p/winpid)" //T //F >/dev/null 2>&1 || true
    else kill "$p" 2>/dev/null || true; fi
  done
  docker rm -f "$MAILPIT_NAME" >/dev/null 2>&1 || true
  echo "Supabase keeps running (stop it with: $SUPABASE stop). Your data is kept."
}
trap cleanup EXIT INT TERM

docker info >/dev/null 2>&1 || fail "Docker Desktop is not running"

# ---- master key for your saved app logins (random, kept only in .demo/master.key) ------------------------------
if [ ! -s "$DEMO/master.key" ]; then
  "$PY" -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())" >"$DEMO/master.key"
fi
PROFILE_MASTER_KEY="$(tr -d '\r\n' <"$DEMO/master.key")"

# ---- Supabase ---------------------------------------------------------------------------------------------------
log "starting local Supabase (the first run downloads images; be patient)"
bash scripts/dev_supabase.sh >"$LOGS/supabase.log" 2>&1 || { tail -20 "$LOGS/supabase.log"; fail "Supabase did not start"; }

env_value() { sed -n "s/^$2=//p" "$1" | tr -d '\r' | head -n1; }
DATABASE_URL="$(env_value .env.server.local DATABASE_URL)"
[ -n "$DATABASE_URL" ] || fail ".env.server.local has no DATABASE_URL"

need_reset="$RESET"
if [ "$need_reset" = 0 ]; then
  has="$("$PY" -c "import psycopg,sys
c=psycopg.connect(sys.argv[1])
print(c.execute(\"select to_regclass('public.cart_fills') is not null and to_regclass('public.app_addresses') is not null and to_regclass('public.usage_erased') is not null\").fetchone()[0])" "$DATABASE_URL" 2>/dev/null || echo False)"
  [ "$has" = "True" ] || need_reset=1
fi
if [ "$need_reset" = 1 ]; then
  log "applying every migration (db reset)"
  ok=0
  for i in 1 2 3; do
    if $SUPABASE db reset >"$LOGS/reset.log" 2>&1; then ok=1; break; fi
    echo "reset attempt $i failed; retrying"; sleep 5
  done
  [ "$ok" = 1 ] || { tail -20 "$LOGS/reset.log"; fail "supabase db reset failed"; }
  bash scripts/dev_supabase.sh >"$LOGS/supabase.log" 2>&1 || fail "Supabase did not come back after the reset"
  DATABASE_URL="$(env_value .env.server.local DATABASE_URL)"
fi
export DATABASE_URL

if [ "$need_reset" = 0 ]; then
  # Newer code may ship newer migrations: add them without touching your data.
  log "applying any new migrations (your data is kept)"
  $SUPABASE migration up --local >"$LOGS/migrate.log" 2>&1 || { tail -20 "$LOGS/migrate.log"; fail "supabase migration up failed"; }
fi

# ---- Mailpit ------------------------------------------------------------------------------------------------------
docker rm -f "$MAILPIT_NAME" >/dev/null 2>&1 || true
docker run -d --rm --name "$MAILPIT_NAME" -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 axllent/mailpit:v1.21 >"$LOGS/mailpit.log" 2>&1 \
  || fail "could not start Mailpit (are ports 1025 and 8025 free?)"

# ---- you, as admin ---------------------------------------------------------------------------------------------------
log "making $ADMIN_EMAIL an invited admin"
"$PY" cli.py add-admin "$ADMIN_EMAIL" >"$LOGS/admin.log" 2>&1 || { cat "$LOGS/admin.log"; fail "add-admin failed"; }

# ---- API, worker, scheduler, PWA ----------------------------------------------------------------------------
server_env() {
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env.server.local"
  set +a
  unset SUPABASE_JWT_SECRET
  export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
  export SMTP_HOST=127.0.0.1 SMTP_PORT=1025 APP_URL="$WEB_URL" CORS_ORIGINS="$WEB_URL"
  export PROFILE_MASTER_KEY PROFILE_STORE_URL="file://$DEMO/vault" PROFILE_ROOT="$DEMO/profiles"
}

log "starting the API on $API_URL"
( server_env; exec "$PY" -m uvicorn server.api:app --host "$HOST" --port "$API_PORT" --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" ) >"$LOGS/api.log" 2>&1 &
PIDS+=($!)
wait_for "API" 60 "$API_URL/healthz"

log "starting the worker (a real Chrome will open when you connect an app)"
( server_env; exec "$PY" -m server.worker ) >"$LOGS/worker.log" 2>&1 &
PIDS+=($!)

log "starting the scheduler"
( server_env; exec "$PY" -m server.scheduler ) >"$LOGS/scheduler.log" 2>&1 &
PIDS+=($!)

log "starting the PWA on $WEB_URL (the first page load compiles; give it a few seconds)"
SUPABASE_PUBLIC_URL="$(env_value web/.env.local NEXT_PUBLIC_SUPABASE_URL)"
[ "$LAN" = 1 ] && SUPABASE_PUBLIC_URL="$(printf '%s' "$SUPABASE_PUBLIC_URL" | sed "s#//[^:/]*#//$SHOWN_HOST#")"
[ -d web/node_modules ] || pnpm --dir web install --frozen-lockfile >"$LOGS/web-install.log" 2>&1
(
  cd web
  SB_URL="$SUPABASE_PUBLIC_URL"
  NEXT_PUBLIC_API_URL="$API_URL" NEXT_PUBLIC_SUPABASE_URL="$SB_URL" NEXT_TELEMETRY_DISABLED=1 exec node node_modules/next/dist/bin/next dev -H "$HOST" -p "$WEB_PORT"
) >"$LOGS/web.log" 2>&1 &
PIDS+=($!)
wait_for "PWA" 120 "$WEB_URL/login"

cat <<EOF

================================================================================
 Hisaab is running.

   App:      $WEB_URL
   Emails:   http://localhost:8025   (invites and alerts land here; this laptop only)
   Logs:     $LOGS

 1. Open the app and SIGN UP with $ADMIN_EMAIL (you are already invited and an admin).
 2. Accounts -> connect Zepto / Blinkit / Swiggy / Zomato with your own phone number and the real OTP.
 3. Admin tab: learn each app's recipes once (orders, grocery prices, cart, addresses) and approve them.
 4. Then Cart: compare, fill, pay in the app yourself.

 Hisaab never orders or pays. Ctrl-C here stops everything.
================================================================================
EOF
wait
