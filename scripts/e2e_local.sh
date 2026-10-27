#!/usr/bin/env bash
# End-to-end test of the whole product on this machine, on an iPhone 14 profile:
#   PWA (next start) -> API (uvicorn) -> jobs queue -> fake worker -> local Supabase Postgres -> Realtime -> PWA
#
# 1. starts local Supabase if it is down (scripts/dev_supabase.sh) and builds the PWA against it
# 2. `supabase db reset` (every migration, a clean database), then dev_supabase.sh again for the ask_reader login
# 3. starts the API (ASK_FAKE=1) and the worker (WORKER_FAKE=1, CATEGORIZE=0) with .env.server.local, and the PWA
# 4. runs Playwright (e2e/), then stops everything it started, including Supabase if it started it
#
# Usage: bash scripts/e2e_local.sh [playwright args, e.g. -g "updates live"]
# Env:   E2E_KEEP_SUPABASE=1 leaves a Supabase this script started running.
# Ports: API 8010, PWA 3110, one host name throughout: localhost. Docker must be running.
# Logs:  e2e/.run/logs/ (never secrets). Prints one summary line at the end.
set -euo pipefail
set -m # background jobs get their own process group, so each can be stopped with its children

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
SUPABASE="npx --yes supabase@2.122.0"
HOST="localhost"
API_PORT=8010
WEB_PORT=3110
API_URL="http://$HOST:$API_PORT"
WEB_URL="http://$HOST:$WEB_PORT"
RUN_DIR="$ROOT/e2e/.run"
LOGS="$RUN_DIR/logs"

PHASE="setup"
STARTED_SUPABASE=0
PIDS=()
RESULT=""

rm -rf "$LOGS"
mkdir -p "$LOGS" "$RUN_DIR/profiles"

log() { printf '[e2e] %s\n' "$*" >&2; }

# ---- stopping what we started ---------------------------------------------------------------------

stop_pid() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 0
  if [ -r "/proc/$pid/winpid" ]; then
    # Git Bash on Windows: the job is a native process; end it and its children by Windows PID.
    taskkill //F //T //PID "$(cat "/proc/$pid/winpid")" >/dev/null 2>&1 || true
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 50); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  local pid
  for pid in "${PIDS[@]+"${PIDS[@]}"}"; do stop_pid "$pid"; done
  if [ "$STARTED_SUPABASE" = 1 ] && [ "${E2E_KEEP_SUPABASE:-0}" != 1 ]; then
    log "stopping the Supabase this run started"
    $SUPABASE stop >>"$LOGS/supabase.log" 2>&1 || true
  fi
  local took="$((SECONDS / 60))m$((SECONDS % 60))s"
  if [ "$status" = 0 ]; then
    echo "E2E OK: ${RESULT:-passed} in $took"
  elif [ "$PHASE" = "playwright" ]; then
    echo "E2E FAILED: ${RESULT:-playwright exited $status} in $took (report: e2e/playwright-report, logs: e2e/.run/logs)"
  else
    echo "E2E FAILED during $PHASE after $took (logs: e2e/.run/logs)"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

fail() {
  log "$*"
  exit 1
}

show_log() { # the end of a log, for a failure that needs it (logs never hold secrets)
  [ -f "$1" ] && tail -n 30 "$1" >&2 || true
}

# ---- checks ---------------------------------------------------------------------------------------

PHASE="checking tools"
PY=""
for candidate in python python3; do
  # "python3" on Windows can be the Microsoft Store stub, which exits non-zero: only accept one that runs.
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || fail "need Python 3.11+ as python or python3"
command -v node >/dev/null 2>&1 || fail "need Node 22"
command -v pnpm >/dev/null 2>&1 || fail "need pnpm"
command -v curl >/dev/null 2>&1 || fail "need curl"
docker info >/dev/null 2>&1 || fail "Docker is not running"
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1 NEXT_TELEMETRY_DISABLED=1

listening() { curl -s -o /dev/null --max-time 2 "http://$HOST:$1/" 2>/dev/null; }
for port in "$API_PORT" "$WEB_PORT"; do
  if listening "$port"; then fail "port $port is already in use; stop whatever is on it"; fi
done

# wait_for <what> <seconds> <pid or ""> <log> <command...>: poll until the command succeeds.
wait_for() {
  local what="$1" limit="$2" pid="$3" logfile="$4"
  shift 4
  local start=$SECONDS
  until "$@" >/dev/null 2>&1; do
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      show_log "$logfile"
      fail "$what exited before it was ready"
    fi
    if [ $((SECONDS - start)) -ge "$limit" ]; then
      show_log "$logfile"
      fail "$what not ready after ${limit}s"
    fi
    sleep 1
  done
  log "$what ready ($((SECONDS - start))s)"
}

http_ok() { curl -sf -o /dev/null --max-time 5 "$@"; }

# ---- Supabase and the PWA build -----------------------------------------------------------------------

PHASE="starting Supabase"
if ! $SUPABASE status >/dev/null 2>&1; then
  STARTED_SUPABASE=1
  log "starting local Supabase"
fi
# Starts it if needed and writes web/.env.local and .env.server.local. Its stdout names the anon key: not printed.
bash scripts/dev_supabase.sh >/dev/null 2>>"$LOGS/supabase.log" || { show_log "$LOGS/supabase.log"; fail "scripts/dev_supabase.sh failed"; }

env_value() { sed -n "s/^$2=//p" "$1" | tr -d '\r' | head -n1; }
SUPABASE_URL="$(env_value web/.env.local NEXT_PUBLIC_SUPABASE_URL)"
ANON_KEY="$(env_value web/.env.local NEXT_PUBLIC_SUPABASE_ANON_KEY)"
[ -n "$SUPABASE_URL" ] && [ -n "$ANON_KEY" ] || fail "web/.env.local is missing the Supabase URL or anon key"
# One host everywhere: the browser, the PWA server and the API all say localhost, which is also the host
# of the Supabase auth site_url (supabase/config.toml), so auth redirects to this PWA are allowed.
SUPABASE_URL="${SUPABASE_URL//127.0.0.1/$HOST}"

PHASE="building the PWA"
if [ ! -d web/node_modules ]; then
  log "installing web dependencies"
  pnpm --dir web install --frozen-lockfile >"$LOGS/web-install.log" 2>&1 || { show_log "$LOGS/web-install.log"; fail "pnpm install (web) failed"; }
fi
log "building the PWA for $WEB_URL (API $API_URL)"
NEXT_PUBLIC_SUPABASE_URL="$SUPABASE_URL" NEXT_PUBLIC_SUPABASE_ANON_KEY="$ANON_KEY" NEXT_PUBLIC_API_URL="$API_URL" \
  pnpm --dir web build >"$LOGS/web-build.log" 2>&1 || { show_log "$LOGS/web-build.log"; fail "next build failed"; }

PHASE="preparing Playwright"
if [ ! -d e2e/node_modules ]; then
  pnpm --dir e2e install --frozen-lockfile >"$LOGS/e2e-install.log" 2>&1 || { show_log "$LOGS/e2e-install.log"; fail "pnpm install (e2e) failed"; }
fi
pnpm --dir e2e exec playwright install webkit >"$LOGS/playwright-install.log" 2>&1 \
  || { show_log "$LOGS/playwright-install.log"; fail "playwright install webkit failed (on Linux: pnpm --dir e2e exec playwright install --with-deps webkit)"; }

PHASE="resetting the database"
log "supabase db reset"
$SUPABASE db reset >>"$LOGS/supabase.log" 2>&1 || { show_log "$LOGS/supabase.log"; fail "supabase db reset failed"; }
# The reset recreates ask_reader without a login: give it a fresh password (written to .env.server.local only).
bash scripts/dev_supabase.sh >/dev/null 2>>"$LOGS/supabase.log" || { show_log "$LOGS/supabase.log"; fail "scripts/dev_supabase.sh failed after the reset"; }
wait_for "Supabase auth" 120 "" "$LOGS/supabase.log" http_ok -H "apikey: $ANON_KEY" "$SUPABASE_URL/auth/v1/health"
wait_for "Supabase REST" 120 "" "$LOGS/supabase.log" http_ok -H "apikey: $ANON_KEY" "$SUPABASE_URL/rest/v1/"

# ---- API, worker, PWA ---------------------------------------------------------------------------------

# Each service gets .env.server.local (database URLs, JWKS URL) plus its own e2e switches.
server_env() {
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env.server.local"
  set +a
  export SUPABASE_JWKS_URL="${SUPABASE_JWKS_URL//127.0.0.1/$HOST}"
  unset SUPABASE_JWT_SECRET OPENAI_API_KEY TYPESAFE_API_KEY PROXY_URL_TEMPLATE
}

PHASE="starting the API"
(
  server_env
  export ASK_FAKE=1 CORS_ORIGINS="$WEB_URL"
  exec "$PY" -m uvicorn server.api:app --host "$HOST" --port "$API_PORT"
) >"$LOGS/api.log" 2>&1 &
API_PID=$!
PIDS+=("$API_PID")
wait_for "API" 60 "$API_PID" "$LOGS/api.log" http_ok "$API_URL/healthz"

PHASE="starting the worker"
(
  server_env
  export WORKER_FAKE=1 CATEGORIZE=0 PROFILE_ROOT="$RUN_DIR/profiles" DRAIN_S=5
  exec "$PY" -m server.worker
) >"$LOGS/worker.log" 2>&1 &
WORKER_PID=$!
PIDS+=("$WORKER_PID")
wait_for "worker" 60 "$WORKER_PID" "$LOGS/worker.log" grep -q "worker started" "$LOGS/worker.log"

PHASE="starting the PWA"
(
  cd web
  exec node node_modules/next/dist/bin/next start -H "$HOST" -p "$WEB_PORT"
) >"$LOGS/web.log" 2>&1 &
WEB_PID=$!
PIDS+=("$WEB_PID")
wait_for "PWA" 90 "$WEB_PID" "$LOGS/web.log" http_ok "$WEB_URL/login"

# ---- Playwright ---------------------------------------------------------------------------------------

PHASE="playwright"
log "running Playwright (iPhone 14, WebKit) against $WEB_URL"
set +e
E2E_BASE_URL="$WEB_URL" pnpm --dir e2e exec playwright test "$@" 2>&1 | tee "$LOGS/playwright.log"
PW_STATUS=${PIPESTATUS[0]}
set -e
RESULT="$(grep -E '^[[:space:]]+[0-9]+ (passed|failed|flaky|skipped|did not run)' "$LOGS/playwright.log" \
  | sed -E 's/^[[:space:]]+//; s/ \(.*\)$//' | paste -sd ',' - | sed 's/,/, /g')"
exit "$PW_STATUS"
