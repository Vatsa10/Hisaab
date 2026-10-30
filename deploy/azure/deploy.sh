#!/usr/bin/env bash
# Usage: deploy/azure/deploy.sh <resource-group>
# Builds both images in ACR and deploys deploy/azure/main.bicep. This spends Azure credit.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <resource-group>" >&2
  exit 2
fi
RG="$1"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ENV_FILE="$HERE/.env.deploy"

# The image tag is the commit sha, so the tree must be exactly that commit (untracked files count as changes).
. "$HERE/guard.sh"
if ! git_tree_clean "$ROOT"; then
  echo "refusing to deploy: the git tree has uncommitted or untracked changes" >&2
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "missing $ENV_FILE - copy env.deploy.example and fill it in" >&2
  exit 1
fi

# Load KEY=value lines without echoing them or running them as shell.
for key in DATABASE_URL ASK_DATABASE_URL SUPABASE_JWT_SECRET SUPABASE_JWKS_URL CORS_ORIGINS ANTHROPIC_API_KEY TYPESAFE_API_KEY PROFILE_MASTER_KEY PROXY_URL_TEMPLATE LOCATION PREFIX; do
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 || true)"
  value="${line#*=}"
  value="${value%$'\r'}"
  printf -v "$key" '%s' "$value"
done
for key in DATABASE_URL ASK_DATABASE_URL SUPABASE_JWT_SECRET SUPABASE_JWKS_URL CORS_ORIGINS ANTHROPIC_API_KEY TYPESAFE_API_KEY PROFILE_MASTER_KEY PROXY_URL_TEMPLATE; do
  if [ -z "${!key}" ]; then
    echo "$key is empty in $ENV_FILE" >&2
    exit 1
  fi
done
LOCATION="${LOCATION:-centralindia}"
PREFIX="${PREFIX:-expagent}"

PY=""
for cand in python3 python; do
  # Run it: the Windows Store python3 alias is a stub that exists but fails.
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys' >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "a working python3 or python is required on PATH" >&2
  exit 1
fi

# Both database URLs must be the Supabase session pooler over TLS (never 6543). They reach the check through the
# environment, never argv, and the messages never echo them.
export DATABASE_URL ASK_DATABASE_URL
if ! PYTHONPATH="$ROOT" PYTHONIOENCODING=utf-8 "$PY" -c '
import os, sys
from server import config
try:
    config.check_database_url(os.environ["DATABASE_URL"], "worker")
    config.check_database_url(os.environ["ASK_DATABASE_URL"], "api", name="ASK_DATABASE_URL")
except ValueError as e:
    print(e, file=sys.stderr)
    sys.exit(1)
'; then
  echo "refusing to deploy: fix the database URL in $ENV_FILE" >&2
  exit 1
fi

# The whole repo is the build context; make sure the secrets file cannot be uploaded.
if ! dockerignore_excludes_env_deploy "$ROOT/.dockerignore"; then
  echo "refusing to build: $ROOT/.dockerignore must exclude deploy/azure/.env.deploy" >&2
  exit 1
fi

SHA="$(git -C "$ROOT" rev-parse --short HEAD)"

# Secrets go through a temp parameters file (not argv, which is visible in ps).
PARAMS="$(mktemp)"
trap 'rm -f "$PARAMS"' EXIT
chmod 600 "$PARAMS"
# Native Windows az/python cannot open Git Bash /tmp paths; convert when cygpath exists.
PARAMS_NATIVE="$PARAMS"
if command -v cygpath >/dev/null 2>&1; then
  PARAMS_NATIVE="$(cygpath -w "$PARAMS")"
fi
export SUPABASE_JWT_SECRET SUPABASE_JWKS_URL CORS_ORIGINS ANTHROPIC_API_KEY TYPESAFE_API_KEY PROFILE_MASTER_KEY PROXY_URL_TEMPLATE
"$PY" - "$PARAMS_NATIVE" <<'PY'
import json, os, sys
names = {
    "databaseUrl": "DATABASE_URL",
    "askDatabaseUrl": "ASK_DATABASE_URL",
    "supabaseJwksUrl": "SUPABASE_JWKS_URL",
    "corsOrigins": "CORS_ORIGINS",
    "supabaseJwtSecret": "SUPABASE_JWT_SECRET",
    "anthropicApiKey": "ANTHROPIC_API_KEY",
    "typesafeApiKey": "TYPESAFE_API_KEY",
    "profileMasterKey": "PROFILE_MASTER_KEY",
    "proxyUrlTemplate": "PROXY_URL_TEMPLATE",
}
doc = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {k: {"value": os.environ[v]} for k, v in names.items()},
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(doc, f)
PY

az group create --name "$RG" --location "$LOCATION" --output none

# Phase 1: registry, storage, identity, environment (no apps yet, so there is somewhere to push images).
az deployment group create --resource-group "$RG" \
  --template-file "$HERE/main.bicep" \
  --parameters "@$PARAMS_NATIVE" location="$LOCATION" prefix="$PREFIX" deployApps=false \
  --output none

ACR_LOGIN="$(az deployment group show --resource-group "$RG" --name main \
  --query properties.outputs.acrLoginServer.value --output tsv)"
ACR_NAME="${ACR_LOGIN%%.*}"

az acr build --registry "$ACR_NAME" --image "api:$SHA" \
  --file "$ROOT/docker/api.Dockerfile" "$ROOT"
az acr build --registry "$ACR_NAME" --image "worker:$SHA" \
  --file "$ROOT/docker/worker.Dockerfile" "$ROOT"

# Phase 2: the Container Apps.
az deployment group create --resource-group "$RG" \
  --template-file "$HERE/main.bicep" \
  --parameters "@$PARAMS_NATIVE" location="$LOCATION" prefix="$PREFIX" deployApps=true \
  apiImage="$ACR_LOGIN/api:$SHA" workerImage="$ACR_LOGIN/worker:$SHA" \
  --output none

az deployment group show --resource-group "$RG" --name main \
  --query properties.outputs.apiUrl.value --output tsv
