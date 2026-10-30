#!/usr/bin/env bash
# Smoke test: build and start the local stack, wait for API health and the worker banner, then tear down.
set -u
cd "$(dirname "$0")/.."
# Own project name: never touches (or wipes the volumes of) the default stack used for e2e work.
DC="docker compose -p ea-smoke"
trap '$DC down -v >/dev/null 2>&1' EXIT

$DC up -d --build || { echo "SMOKE FAIL: compose up failed"; exit 1; }

api_ok=0
worker_ok=0
for _ in $(seq 1 60); do
  if [ "$api_ok" = 0 ] && curl -fs --max-time 5 localhost:8000/healthz >/dev/null; then api_ok=1; fi
  if [ "$worker_ok" = 0 ] && $DC logs worker 2>&1 | grep -q "worker started"; then worker_ok=1; fi
  if [ "$api_ok" = 1 ] && [ "$worker_ok" = 1 ]; then
    # /ask runs on the ask_reader login (ASK_DATABASE_URL): 503 means that login is not wired, so it must not be 503.
    # 200 with an ANTHROPIC_API_KEY; without one the analyst's LLM call fails after the login worked, which is 502.
    code="$($DC exec -T api python -c '
import os, time, urllib.request, urllib.error
import jwt
tok = jwt.encode({"sub": "00000000-0000-0000-0000-000000000001", "aud": "authenticated", "role": "authenticated",
                  "exp": int(time.time()) + 300}, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
req = urllib.request.Request("http://localhost:8000/ask", data=b"{\"question\": \"how much did I spend?\"}", method="POST",
                             headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=20).status)
except urllib.error.HTTPError as e:
    print(e.code)
' 2>/dev/null | tail -n1 | tr -d '\r')"
    if [ "$code" = 200 ] || { [ "$code" = 502 ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; }; then echo "SMOKE OK"; exit 0; fi
    echo "SMOKE FAIL: /ask answered ${code:-nothing} (expected 200; 502 only without ANTHROPIC_API_KEY)"
    $DC logs --tail=30 api
    exit 1
  fi
  sleep 2
done
echo "SMOKE FAIL: api_ok=$api_ok worker_ok=$worker_ok"
$DC logs --tail=50
exit 1
