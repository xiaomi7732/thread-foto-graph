#!/usr/bin/env bash
set -euo pipefail

if (( $# != 0 )); then
  echo "Usage: bash restart_continuous.sh" >&2
  exit 2
fi

service=thread-foto-graph-continuous.service
echo "Restarting $service; waiting for any active cycle to finish."
systemctl --user restart "$service"
systemctl --user status "$service" --no-pager
