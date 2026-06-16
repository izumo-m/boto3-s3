#!/bin/bash
# scripts/compose.sh - thin wrapper around `docker compose` pinned to the
# project's compose.dev.yaml, so any compose subcommand works from any cwd:
#
#   scripts/compose.sh up -d
#   scripts/compose.sh down
#   scripts/compose.sh ps
#   scripts/compose.sh logs minio
#
# For "start and wait until the buckets are provisioned" use
# scripts/compose-up.sh, which builds on this.

set -euo pipefail

scriptdir=$(cd "$(dirname "$0")" && pwd)
exec docker compose -f "$scriptdir/compose.dev.yaml" "$@"
