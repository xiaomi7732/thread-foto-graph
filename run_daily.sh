#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
continuous=false
phase=preflight
if [[ -n "${CONTINUOUS_CYCLE_ID:-}" ]]; then
  continuous=true
  trap 'rc=$?; if [[ "$phase" == git ]]; then exit 70; else exit "$rc"; fi' ERR
fi

stop_git() {
  if "$continuous"; then exit 70; else exit 1; fi
}

publish=false
case "${1:-}" in
  "") ;;
  --publish) publish=true ;;
  *) echo "Usage: bash run_daily.sh [--publish]" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
  echo "Usage: bash run_daily.sh [--publish]" >&2
  exit 2
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
mkdir -p cache
exec 9>cache/pipeline.lock
if ! flock -n 9; then
  echo "Another pipeline run holds the lock; no work started." >&2
  if "$continuous"; then exit 73; else exit 1; fi
fi

python=.venv/bin/python
if [[ ! -x "$python" ]]; then
  echo "Create .venv and install requirements.txt first." >&2
  if "$continuous"; then exit 78; else exit 1; fi
fi
export GIT_TERMINAL_PROMPT=0
if "$continuous"; then
  "$python" -m src.continuous validate-limits
fi

run_git() {
  timeout --signal=TERM --kill-after=5s "${GIT_TIMEOUT_SECONDS:-120}s" git "$@"
}

if "$publish"; then
  phase=git
  checkout_status=$(run_git status --porcelain)
  if [[ -n "$checkout_status" ]]; then
    echo "Checkout is dirty; review local changes before publishing." >&2
    stop_git
  fi
  branch=$(run_git symbolic-ref --quiet --short HEAD)
  upstream=$(run_git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')
  remote=$(run_git config --get "branch.$branch.remote")
  remote_ref=$(run_git config --get "branch.$branch.merge")
  if [[ "$remote" == "." ]]; then
    echo "Publishing requires a remote tracking branch." >&2
    stop_git
  fi
  run_git fetch "$remote"
  unpushed=$(run_git rev-list "$upstream..HEAD")
  if [[ -n "$unpushed" ]]; then
    echo "Unpushed commits exist; review and push them before another run." >&2
    stop_git
  fi
  run_git pull --ff-only "$remote" "$remote_ref"
fi

# The Python deadline leaves time to render outputs; this is a hard backstop.
phase=pipeline
pipeline_status=0
timeout --signal=TERM --kill-after=30s "${PIPELINE_TIMEOUT_SECONDS:-3000}s" \
  "$python" -u -m src.main || pipeline_status=$?
if "$continuous"; then
  "$python" -m src.continuous validate-status "$pipeline_status" || exit 1
fi
if (( pipeline_status != 0 )); then
  exit "$pipeline_status"
fi

if ! "$publish"; then
  echo "Local run complete; nothing committed or pushed."
  exit 0
fi

phase=git
run_git add -- data/needs-reply.md data/needs-like.md data/actions.md data/topics.md \
  data/threads data/history docs/graph.json
if run_git diff --cached --quiet; then
  echo "No output changes to publish."
  exit 0
else
  diff_status=$?
  if (( diff_status != 1 )); then
    echo "Could not inspect staged output; nothing committed or pushed." >&2
    if "$continuous"; then exit 70; else exit "$diff_status"; fi
  fi
fi
run_git commit -m "Update thread topic graph ($(date -u +'%Y-%m-%d %H:%M UTC'))"

for attempt in 1 2 3; do
  if run_git push "$remote" "HEAD:$remote_ref"; then
    exit 0
  fi
  run_git fetch "$remote"
  if ! run_git rebase "$upstream"; then
    if ! run_git rebase --abort; then
      echo "Git recovery was interrupted; inspect git status before retrying. Local cache retained." >&2
      stop_git
    fi
    echo "Conflicting remote changes; local commit and cache retained. Resolve manually." >&2
    stop_git
  fi
  sleep "$((attempt * 5))"
done
echo "Push failed; local commit and cache retained. Review and push manually." >&2
stop_git
