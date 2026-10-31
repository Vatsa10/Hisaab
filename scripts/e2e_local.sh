#!/usr/bin/env bash
# End-to-end test of the whole product on this machine, on an iPhone 14 profile:
#   PWA (next start) -> API (uvicorn) -> jobs queue -> fake worker -> local Supabase Postgres -> Realtime -> PWA
#
# 1. starts local Supabase if it is down (scripts/dev_supabase.sh) and builds the PWA against it
# 2. `supabase db reset` (every migration, a clean database), then dev_supabase.sh again for the ask_reader login
# 3. starts the API (ASK_FAKE=1) and the worker (WORKER_FAKE=1, CATEGORIZE=0, FAKE_PAGE_DELAY_S=0.4) with
#    .env.server.local, and the PWA
# 4. starts Mailpit (every email of the run lands there), makes admin@e2e.test an admin, and sends the API and the
#    worker's mail and links to it (SMTP_HOST, SMTP_PORT, APP_URL)
# 5. runs Playwright (e2e/: product.spec.ts, plan4.spec.ts, plan5.spec.ts and plan6.spec.ts), then stops everything it started, including Mailpit
#    and Supabase if it started it
#
# Usage: bash scripts/e2e_local.sh [playwright args, e.g. -g "updates live"]
# Env:   E2E_KEEP_SUPABASE=1 leaves a Supabase this script started running.
# Ports: API 8010 and PWA 3110 on localhost (the host Supabase Auth allows redirects to); Supabase itself is
#        reached at 127.0.0.1 as `supabase status` reports it. Docker must be running.
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

MAILPIT_NAME="ea-e2e-mailpit"
ADMIN_EMAIL="admin@e2e.test"
PHASE="setup"
STARTED_SUPABASE=0
STARTED_MAILPIT=0
PIDS=()
RESULT=""
FAKE_STATE_DIR=""

rm -rf "$LOGS"
mkdir -p "$LOGS" "$RUN_DIR/profiles"

log() { printf '[e2e] %s\n' "$*" >&2; }

# Supabase's CLI prints its keys (anon, service role, JWT secret, S3) while starting: every line that reaches a
# log or the terminal goes through this first.
redact() {
  sed -E \
    -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/<redacted-jwt>/g' \
    -e 's/("?[A-Za-z0-9_]*(KEY|SECRET|TOKEN|PASSWORD)[A-Za-z0-9_]*"?[[:space:]]*[:=][[:space:]]*)"?[^",[:space:]}]+"?/\1<redacted>/g' \
    -e 's#(postgres(ql)?://[^:/@[:space:]]+:)[^@[:space:]]+@#\1<redacted>@#g'
}

# supabase <args...>: the pinned CLI, its output redacted into supabase.log.
supabase_logged() {
  $SUPABASE "$@" 2>&1 | redact >>"$LOGS/supabase.log"
}

# dev_supabase.sh: its stdout names the anon key (dropped); its stderr (supabase start) is redacted into the log.
dev_supabase() {
  { bash scripts/dev_supabase.sh 2>&1 >/dev/null; } | redact >>"$LOGS/supabase.log"
}

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
  # A worker a spec restarted (restartWorker) is not one of ours: stop it by the PID the spec wrote.
  if [ -r "$RUN_DIR/worker.pid" ]; then
    if command -v taskkill >/dev/null 2>&1; then taskkill //F //T //PID "$(cat "$RUN_DIR/worker.pid")" >/dev/null 2>&1 || true
    else kill -TERM -- "-$(cat "$RUN_DIR/worker.pid")" 2>/dev/null || true; fi
  fi
  if [ -n "$FAKE_STATE_DIR" ]; then rm -rf "$FAKE_STATE_DIR" 2>/dev/null || true; fi
  if [ "$STARTED_MAILPIT" = 1 ]; then docker rm -f "$MAILPIT_NAME" >/dev/null 2>&1 || true; fi
  if [ "$STARTED_SUPABASE" = 1 ] && [ "${E2E_KEEP_SUPABASE:-0}" != 1 ]; then
    log "stopping the Supabase this run started"
    supabase_logged stop || true
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

show_log() { # the end of a log, for a failure that needs it
  [ -f "$1" ] && tail -n 30 "$1" | redact >&2 || true
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

PHASE="starting Mailpit"
docker rm -f "$MAILPIT_NAME" >/dev/null 2>&1 || true   # a leftover from an interrupted run
docker run -d --rm --name "$MAILPIT_NAME" -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 axllent/mailpit:v1.21   >"$LOGS/mailpit.log" 2>&1 || { show_log "$LOGS/mailpit.log"; fail "could not start Mailpit (are ports 1025 and 8025 free?)"; }
STARTED_MAILPIT=1
# Every service and the CLI send mail here, and the links in it point at the PWA of this run.
export SMTP_HOST=127.0.0.1 SMTP_PORT=1025 APP_URL="$WEB_URL"
# Plan 7 tunables, exported so the API, worker, scheduler and the specs all see the same ones: the error budget
# judges a site after 5 finished syncs (not 10), and a throttled site cools down for a minute at first.
export ERROR_BUDGET_MIN_JOBS=5 ERROR_BUDGET_MAX_FAILURE_PCT=30 ERROR_BUDGET_WINDOW_H=24
export BACKOFF_BASE_S=60 BACKOFF_MAX_S=3600 BACKOFF_RESET_H=24

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
wait_for "Mailpit" 60 "" "$LOGS/mailpit.log" http_ok "http://127.0.0.1:8025/api/v1/info"

# ---- Supabase and the PWA build -----------------------------------------------------------------------

PHASE="starting Supabase"
if ! $SUPABASE status >/dev/null 2>&1; then
  STARTED_SUPABASE=1
  log "starting local Supabase"
fi
# Starts it if needed and writes web/.env.local and .env.server.local.
dev_supabase || { show_log "$LOGS/supabase.log"; fail "scripts/dev_supabase.sh failed"; }

env_value() { sed -n "s/^$2=//p" "$1" | tr -d '\r' | head -n1; }
SUPABASE_URL="$(env_value web/.env.local NEXT_PUBLIC_SUPABASE_URL)"
ANON_KEY="$(env_value web/.env.local NEXT_PUBLIC_SUPABASE_ANON_KEY)"
[ -n "$SUPABASE_URL" ] && [ -n "$ANON_KEY" ] || fail "web/.env.local is missing the Supabase URL or anon key"
# The PWA and the API are served on localhost: that is the host of the Supabase Auth site_url
# (supabase/config.toml), and Auth only redirects back to that host. Supabase itself stays at 127.0.0.1, as
# `supabase status` gives it. Do not rewrite it to localhost: on Windows with WSL, wslrelay.exe also listens
# on [::1] for every Docker-published port and sometimes hangs a connection, so a "localhost" Supabase URL
# (which browsers and Node try over ::1 first) stalls sign-ups, server renders and Realtime at random.

PHASE="building the PWA"
if [ ! -d web/node_modules ]; then
  log "installing web dependencies"
  pnpm --dir web install --frozen-lockfile >"$LOGS/web-install.log" 2>&1 || { show_log "$LOGS/web-install.log"; fail "pnpm install (web) failed"; }
fi
if [ "${E2E_SKIP_BUILD:-0}" = 1 ] && [ -f web/.next/BUILD_ID ]; then
  log "reusing the PWA build in web/.next (E2E_SKIP_BUILD=1); it must come from this script, with the same URLs"
else
  log "building the PWA for $WEB_URL (API $API_URL)"
  NEXT_PUBLIC_SUPABASE_URL="$SUPABASE_URL" NEXT_PUBLIC_SUPABASE_ANON_KEY="$ANON_KEY" NEXT_PUBLIC_API_URL="$API_URL" \
    pnpm --dir web build >"$LOGS/web-build.log" 2>&1 || { show_log "$LOGS/web-build.log"; fail "next build failed"; }
fi

PHASE="preparing Playwright"
if [ ! -d e2e/node_modules ]; then
  pnpm --dir e2e install --frozen-lockfile >"$LOGS/e2e-install.log" 2>&1 || { show_log "$LOGS/e2e-install.log"; fail "pnpm install (e2e) failed"; }
fi
pnpm --dir e2e exec playwright install webkit >"$LOGS/playwright-install.log" 2>&1 \
  || { show_log "$LOGS/playwright-install.log"; fail "playwright install webkit failed (on Linux: pnpm --dir e2e exec playwright install --with-deps webkit)"; }

PHASE="resetting the database"
log "supabase db reset"
# The CLI's reset now and then fails while "Initialising schema" (the realtime container races its own
# schema_migrations insert: DbSetupError, exit 1); the next reset goes through. Up to three tries.
for attempt in 1 2 3; do
  if supabase_logged db reset; then break; fi
  [ "$attempt" = 3 ] && { show_log "$LOGS/supabase.log"; fail "supabase db reset failed three times"; }
  log "supabase db reset failed (attempt $attempt), retrying"
done
# The reset recreates ask_reader without a login: give it a fresh password (written to .env.server.local only).
dev_supabase || { show_log "$LOGS/supabase.log"; fail "scripts/dev_supabase.sh failed after the reset"; }
# Safety net: app_addresses must exist after the reset (it lives at the end of 0008_cheapest_cart.sql). If it is missing, apply 0008 again and restart Realtime.
if [ "$("$PY" -c "import os,psycopg,sys
print(psycopg.connect(sys.argv[1]).execute(\"select to_regclass('public.app_addresses') is not null\").fetchone()[0])"   "$(env_value .env.server.local DATABASE_URL)")" = False ]; then
  log "WARNING: app_addresses is missing after the reset (0008 did not apply fully); applying it by hand"
  "$PY" -c "import psycopg,sys
c=psycopg.connect(sys.argv[1],autocommit=True)
c.execute(open('supabase/migrations/0008_cheapest_cart.sql',encoding='utf-8').read())
c.execute(\"notify pgrst, 'reload schema'\")" "$(env_value .env.server.local DATABASE_URL)"     || fail "could not apply 0008_cheapest_cart.sql"
  # Realtime does not see a table added to its publication after it started: restart it, and wait until it is healthy.
  docker restart supabase_realtime_expense-agent >/dev/null 2>&1 || fail "could not restart Realtime"
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' supabase_realtime_expense-agent 2>/dev/null)" = healthy ] && break
    sleep 1
  done
fi
wait_for "Supabase auth" 120 "" "$LOGS/supabase.log" http_ok -H "apikey: $ANON_KEY" "$SUPABASE_URL/auth/v1/health"
wait_for "Supabase REST" 120 "" "$LOGS/supabase.log" http_ok -H "apikey: $ANON_KEY" "$SUPABASE_URL/rest/v1/"

# ---- API, worker, PWA ---------------------------------------------------------------------------------

# Each service gets .env.server.local (database URLs, JWKS URL) plus its own e2e switches.
server_env() {
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env.server.local"
  set +a
  unset SUPABASE_JWT_SECRET ANTHROPIC_API_KEY TYPESAFE_API_KEY PROXY_URL_TEMPLATE
}

PHASE="making the e2e admin"
# Sign-up is invite-only: the admin account of the Plan 4 spec is invited and made an admin before anyone signs up.
DATABASE_URL="$(env_value .env.server.local DATABASE_URL)"
[ -n "$DATABASE_URL" ] || fail ".env.server.local has no DATABASE_URL"
export DATABASE_URL
"$PY" cli.py add-admin "$ADMIN_EMAIL" >"$LOGS/add-admin.log" 2>&1 || { show_log "$LOGS/add-admin.log"; fail "cli.py add-admin failed"; }

PHASE="starting the API"
(
  server_env
  # WORKER_FAKE=1 here only lets the cart-recipe check accept the fake apps' hosts (fake.<site>.test) when the
  # admin approves the learned cart recipe (recipe_admin.decide re-runs check_cart_recipe).
  export ASK_FAKE=1 CORS_ORIGINS="$WEB_URL" WORKER_FAKE=1
  exec "$PY" -m uvicorn server.api:app --host "$HOST" --port "$API_PORT"
) >"$LOGS/api.log" 2>&1 &
API_PID=$!
PIDS+=("$API_PID")
wait_for "API" 60 "$API_PID" "$LOGS/api.log" http_ok "$API_URL/healthz"

PHASE="starting the worker"
# Where the fake apps keep each user's cart (Plan 6): the worker writes it, the specs read it and set the app's
# location. A native path (cygpath -m) because the worker and Playwright are Windows processes under Git Bash.
FAKE_STATE_DIR="$(mktemp -d)"
if command -v cygpath >/dev/null 2>&1; then FAKE_STATE_DIR="$(cygpath -m "$FAKE_STATE_DIR")"; fi
export FAKE_STATE_DIR
# One launcher for the worker, so a spec can stop it and start it again with other switches (restartWorker in
# e2e/helpers/stack.ts, which reads worker.pid and runs this file). FAKE_PAGE_DELAY_S: the fake fetch pauses between
# its pages, so the live "Found so far" count is on screen. MATCH_JUDGE=0: no small-model call for product matching.
cat >"$RUN_DIR/start-worker.sh" <<LAUNCH
#!/usr/bin/env bash
cd "$ROOT"
set -a
. "$ROOT/.env.server.local"
set +a
unset SUPABASE_JWT_SECRET ANTHROPIC_API_KEY TYPESAFE_API_KEY PROXY_URL_TEMPLATE
export WORKER_FAKE=1 CATEGORIZE=0 FAKE_PAGE_DELAY_S=0.4 PROFILE_ROOT="$RUN_DIR/profiles" DRAIN_S=5 MATCH_JUDGE=0
export FAKE_STATE_DIR="$FAKE_STATE_DIR"
exec "$PY" -m server.worker
LAUNCH
export E2E_RUN_DIR="$RUN_DIR"
rm -f "$RUN_DIR/worker.pid"
bash "$RUN_DIR/start-worker.sh" >"$LOGS/worker.log" 2>&1 &
WORKER_PID=$!
PIDS+=("$WORKER_PID")
# The Windows PID when there is one (what taskkill and restartWorker need), else the process group leader.
if [ -r "/proc/$WORKER_PID/winpid" ]; then cat "/proc/$WORKER_PID/winpid" >"$RUN_DIR/worker.pid"; else echo "$WORKER_PID" >"$RUN_DIR/worker.pid"; fi
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
# The specs reach the stack directly too: the CLI and the scheduler (E2E_PY), the database, Auth and the API.
E2E_BASE_URL="$WEB_URL" E2E_ADMIN_EMAIL="$ADMIN_EMAIL" E2E_PY="$PY" E2E_SUPABASE_URL="$SUPABASE_URL"   E2E_ANON_KEY="$ANON_KEY" E2E_API_URL="$API_URL" pnpm --dir e2e exec playwright test "$@" 2>&1 | tee "$LOGS/playwright.log"
PW_STATUS=${PIPESTATUS[0]}
set -e
RESULT="$(grep -E '^[[:space:]]+[0-9]+ (passed|failed|flaky|skipped|did not run)' "$LOGS/playwright.log" \
  | sed -E 's/^[[:space:]]+//; s/ \(.*\)$//' | paste -sd ',' - | sed 's/,/, /g')"
exit "$PW_STATUS"
