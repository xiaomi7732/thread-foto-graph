#!/usr/bin/env bash
set -euo pipefail
umask 077

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
  exit 1
fi

python=.venv/bin/python
if [[ ! -x "$python" ]]; then
  echo "Create .venv and install requirements.txt first." >&2
  exit 1
fi
export GIT_TERMINAL_PROMPT=0

if "$publish"; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "Checkout is dirty; review local changes before publishing." >&2
    exit 1
  fi
  branch=$(git symbolic-ref --quiet --short HEAD)
  upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')
  remote=$(git config --get "branch.$branch.remote")
  remote_ref=$(git config --get "branch.$branch.merge")
  if [[ "$remote" == "." ]]; then
    echo "Publishing requires a remote tracking branch." >&2
    exit 1
  fi
  git fetch "$remote"
  if [[ -n "$(git rev-list "$upstream..HEAD")" ]]; then
    echo "Unpushed commits exist; review and push them before another run." >&2
    exit 1
  fi
  git pull --ff-only "$remote" "$remote_ref"
fi

# The Python deadline leaves time to render outputs; this is a hard backstop.
timeout --signal=TERM --kill-after=30s "${PIPELINE_TIMEOUT_SECONDS:-3000}s" \
  "$python" -u -m src.main

if ! "$publish"; then
  echo "Local run complete; nothing committed or pushed."
  exit 0
fi

git add -- data/needs-reply.md data/needs-like.md data/actions.md data/topics.md \
  data/threads data/history docs/graph.json
if git diff --cached --quiet; then
  echo "No output changes to publish."
  exit 0
fi
git commit -m "Update thread topic graph ($(date -u +'%Y-%m-%d %H:%M UTC'))"

for attempt in 1 2 3; do
  if git push "$remote" "HEAD:$remote_ref"; then
    exit 0
  fi
  git fetch "$remote"
  if ! git rebase "$upstream"; then
    git rebase --abort
    echo "Conflicting remote changes; local commit and cache retained. Resolve manually." >&2
    exit 1
  fi
  sleep "$((attempt * 5))"
done
echo "Push failed; local commit and cache retained. Review and push manually." >&2
exit 1
