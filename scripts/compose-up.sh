#!/bin/bash
# scripts/compose-up.sh - bring up the project's MinIO compose stack if it is
# not already running, then wait for the bucket-init helper to finish so
# callers can rely on AWS_ENDPOINT_URL_S3=http://127.0.0.1:9000 being fully
# provisioned (test-bucket + boto3-s3-e2e created). No-op when the stack is
# already up.
#
# For raw compose subcommands (down, ps, logs, ...) use scripts/compose.sh.

set -euo pipefail

scriptdir=$(cd "$(dirname "$0")" && pwd)
compose="$scriptdir/compose.sh"

# Exact-name match: the compose project name is pinned in compose.dev.yaml,
# so this never mistakes another stack's minio (one sharing ports 9000/9001
# but lacking the boto3-s3-e2e bucket) for ours.
if [ -n "$(docker container ls --filter "name=^boto3-s3-dev-minio-1$" --quiet)" ]; then
    exit 0
fi

echo 'Starting docker minio'
if ! "$compose" up -d; then
    echo "$0: docker start failed" >&2
    exit 1
fi

mc_init_cid=$("$compose" ps -aq mc-init 2>/dev/null)
if [ -n "$mc_init_cid" ]; then
    if ! mc_init_rc=$(timeout 10 docker wait "$mc_init_cid"); then
        echo "$0: timed out waiting for mc-init (10s); buckets may not be ready" >&2
        exit 1
    fi
    if [ "$mc_init_rc" != "0" ]; then
        echo "$0: mc-init exited with status $mc_init_rc; buckets may not be ready" >&2
        exit 1
    fi
fi
