#!/usr/bin/env bash
# Sourced by deploy.sh and test_guard.sh. Defines the build-context guard.

# dockerignore_excludes_env_deploy <file>
# Returns 0 only if <file> contains an exact pattern that excludes deploy/azure/.env.deploy
# and no later negation line that could re-include it.
dockerignore_excludes_env_deploy() {
  local file="$1" line excluded=0
  [ -f "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    # trim leading/trailing whitespace
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in
      '!'*)
        if [ "$excluded" -eq 1 ]; then
          case "$line" in
            *deploy*|*.env*) return 1 ;;
          esac
        fi
        ;;
      'deploy/azure/.env.deploy'|'/deploy/azure/.env.deploy'|'**/.env.deploy'|'**/.env*' \
        |'deploy/azure'|'deploy/azure/'|'deploy'|'deploy/')
        excluded=1
        ;;
    esac
  done < "$file"
  [ "$excluded" -eq 1 ]
}
