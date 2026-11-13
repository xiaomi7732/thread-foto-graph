#!/usr/bin/env bash
set -euo pipefail
umask 077

if (( $# != 0 )); then
  echo "Usage: bash run_continuous.sh" >&2
  exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
mkdir -p cache
exec 8>cache/continuous.lock
if ! flock -n 8; then
  echo "Another continuous supervisor holds the lock." >&2
  exit 73
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "Create .venv and install requirements.txt first." >&2
  exit 78
fi
# Python keeps this lock; subprocess close_fds prevents cycles inheriting it.
exec .venv/bin/python -u -m src.continuous
