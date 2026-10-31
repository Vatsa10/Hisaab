#!/usr/bin/env bash
# Self-check for guard.sh. Usage: bash deploy/azure/test_guard.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=guard.sh
. "$HERE/guard.sh"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
fails=0

check() { # name expected(0|1) content
  printf '%b' "$3" > "$TMP"
  if dockerignore_excludes_env_deploy "$TMP"; then got=0; else got=1; fi
  if [ "$got" -eq "$2" ]; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi
}

check "exact path"                    0 'deploy/azure/.env.deploy\n'
check "rooted exact path"             0 '/deploy/azure/.env.deploy\n'
check "**/.env.deploy"                0 '**/.env.deploy\n'
check "**/.env*"                      0 '**/.env*\n'
check "deploy/azure"                  0 'deploy/azure\n'
check "deploy"                        0 'deploy\n'
check "CRLF line endings"             0 'node_modules\r\ndeploy/azure\r\n'
check "no trailing newline"           0 'deploy/azure/.env.deploy'
check "missing from file"             1 'node_modules\n.git\n'
check "empty file"                    1 ''
check "commented line"                1 '# deploy/azure/.env.deploy\n'
check "root .env.deploy (false pos)"  1 '.env.deploy\n'
check "root .env* (false pos)"        1 '.env*\n'
check "deploy then bare negation"     1 'deploy\n!deploy/azure/.env.deploy\n'
check "deploy then !deploy/azure"     1 'deploy\n!deploy/azure\n'
check "exclusion then !**/.env*"      1 '**/.env.deploy\n!**/.env*\n'
check "negation before exclusion"     0 '!deploy/azure/other\ndeploy/azure\n'
check "unrelated negation after (fail closed)" 1 'deploy/azure\n!docker/keep.txt\n'
check "exclusion then !**/*"          1 'deploy\n!**/*\n'
check "exclusion then !**"            1 'deploy\n!**\n'
check "exclusion then !**/.*"         1 'deploy\n!**/.*\n'
check "exclusion then !*"             1 'deploy\n!*\n'
check "seed negation before exclusion" 0 '!recipes/*.seed.json\ndeploy/azure/.env.deploy\n'
check "comment then negation ignored" 0 'deploy\n# !**\n'

# git_tree_clean: each case runs in its own throwaway repo.
repo_case() { # name expected(0|1) setup-command
  local d got
  d="$(mktemp -d)"
  (cd "$d" && git init -q . && git config user.email t@example.invalid && git config user.name t \
    && echo a > tracked.txt && git add tracked.txt && git commit -q -m init && eval "$3")
  if git_tree_clean "$d"; then got=0; else got=1; fi
  rm -rf "$d"
  if [ "$got" -eq "$2" ]; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi
}
repo_case "clean repo"     0 ':'
repo_case "untracked file" 1 'echo b > new.txt'
repo_case "modified file"  1 'echo b >> tracked.txt'
repo_case "staged file"    1 'echo b >> tracked.txt && git add tracked.txt'
repo_case "not a repo"     1 'rm -rf .git'

# deploy.sh optional keys: a throwaway clean repo, a stub az that records its argv, python capturing the params file.
deploy_case() { # name expected-exit stderr-substring env-lines
  local d stub got err pf
  d="$(mktemp -d)"; stub="$(mktemp -d)"
  mkdir -p "$d/deploy/azure"
  cp "$HERE/deploy.sh" "$HERE/guard.sh" "$d/deploy/azure/"
  mkdir -p "$d/server"; cp "$HERE/../../server/config.py" "$HERE/../../server/__init__.py" "$HERE/../../server/local_db.py" "$d/server/"
  printf 'deploy/azure/.env.deploy
' > "$d/.dockerignore"
  printf '%b' "$4" > "$d/deploy/azure/.env.deploy"
  cat > "$stub/az" <<'AZ'
#!/bin/sh
echo "$@" >> "$AZ_LOG"
for a in "$@"; do case "$a" in @*) cp "${a#@}" "$AZ_PARAMS";; esac; done
case "$*" in *"show"*) echo x.azurecr.io;; esac
exit 0
AZ
  chmod +x "$stub/az"
  (cd "$d" && git init -q . && git config user.email t@example.invalid && git config user.name t     && git add -A && git commit -q -m init)
  AZ_LOG="$d/az.log" AZ_PARAMS="$d/params.json"; export AZ_LOG AZ_PARAMS
  err="$(cd "$d" && PATH="$stub:$PATH" bash deploy/azure/deploy.sh rg 2>&1 >/dev/null)"; got=$?
  DEPLOY_DIR="$d"; DEPLOY_ERR="$err"
  if [ "$got" -eq "$2" ] && { [ -z "$3" ] || printf '%s' "$err" | grep -qF "$3"; }; then echo "PASS $1"; else echo "FAIL $1 ($got: $err)"; fails=$((fails + 1)); fi
}
CORE='DATABASE_URL=postgresql://postgres.r:p@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require
ASK_DATABASE_URL=postgresql://ask_reader.r:p@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require
SUPABASE_JWT_SECRET=s
SUPABASE_JWKS_URL=https://r.supabase.co/j
CORS_ORIGINS=https://app.example.com
ANTHROPIC_API_KEY=a
TYPESAFE_API_KEY=t
PROFILE_MASTER_KEY=m
PROXY_URL_TEMPLATE=http://p
'
deploy_case "APP_URL localhost refused"  1 "APP_URL must not be localhost" "${CORE}APP_URL=https://localhost:3000
"
deploy_case "APP_URL http refused"       1 "APP_URL must be an https" "${CORE}APP_URL=http://app.example.com
"
deploy_case "SMTP_HOST without port"     1 "SMTP_PORT" "${CORE}SMTP_HOST=smtp.example.com
"
[ ! -e "$DEPLOY_DIR/az.log" ] && echo "PASS refusals make no az call" || { echo "FAIL az called on refusal"; fails=$((fails + 1)); }
deploy_case "optional keys forwarded"    0 "" "${CORE}APP_URL=https://app.example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_PASSWORD=smtp-secret
VAPID_PRIVATE_KEY=vapid-secret
VAPID_PUBLIC_KEY=pub
AI_INR_PER_1K_TOKENS=0.07
"
if grep -q smtpPort "$DEPLOY_DIR/params.json" && grep -q vapid-secret "$DEPLOY_DIR/params.json"    && grep -q '"aiInrPer1kTokens": {"value": "0.07"}' "$DEPLOY_DIR/params.json"    && grep -q 'https://app.example.com' "$DEPLOY_DIR/params.json"    && [ "$(grep -c '^deployment group create' "$DEPLOY_DIR/az.log")" -eq 2 ]    && ! grep -q -e smtp-secret -e vapid-secret "$DEPLOY_DIR/az.log"; then echo "PASS params file has optionals, argv has no secrets"; else echo "FAIL param forwarding"; fails=$((fails + 1)); fi

[ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
