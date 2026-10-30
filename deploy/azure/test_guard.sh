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

[ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
