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
  if [ "$api_ok" = 1 ] && [ "$worker_ok" = 1 ]; then echo "SMOKE OK"; exit 0; fi
  sleep 2
done
echo "SMOKE FAIL: api_ok=$api_ok worker_ok=$worker_ok"
$DC logs --tail=50
exit 1
