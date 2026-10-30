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
        # Fail closed: any negation after the exclusion could re-include the file
        # (wildcards like !** or !**/* do), so none is allowed.
        [ "$excluded" -eq 1 ] && return 1
        ;;
      'deploy/azure/.env.deploy'|'/deploy/azure/.env.deploy'|'**/.env.deploy'|'**/.env*' \
        |'deploy/azure'|'deploy/azure/'|'deploy'|'deploy/')
        excluded=1
        ;;
    esac
  done < "$file"
  [ "$excluded" -eq 1 ]
}

# git_tree_clean <dir>
# Returns 0 only when `git -C <dir> status --porcelain` prints nothing: modified, staged and untracked files all
# count as dirty, so a deploy always matches one commit (the image tag is that commit's sha).
git_tree_clean() {
  local dir="$1" out
  out="$(git -C "$dir" status --porcelain 2>/dev/null)" || return 1
  [ -z "$out" ]
}
