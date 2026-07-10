#!/usr/bin/env bash
#
# Run the case-conflict tests that require a CASE-INSENSITIVE filesystem.
#
# A handful of `--case-conflict` tests (the `*_with_existing_file` cases, marked
# with aws-cli's `skip_if_case_sensitive`) only reproduce when the download
# destination is case-insensitive - the conflict is detected through
# `os.path.exists`, which on a case-sensitive filesystem never sees the twin.
# macOS (APFS/HFS+) and Windows (NTFS) are case-insensitive by default and run
# these tests as part of the normal suite; on a case-sensitive Linux host they
# skip cleanly (the `case_insensitive_workdir` fixture, tests/conftest.py).
#
# This script runs them on Linux anyway. It mounts a tiny FAT (vfat) loopback
# image - natively case-insensitive, in-kernel, no extra packages on the host -
# inside a privileged Docker container and points the fixture at it through
# $BOTO3_S3_PYTEST_CASE_INSENSITIVE_DIR (ciopfs, the usual FUSE option, is no
# longer packaged on recent Ubuntu, and ext4 casefold needs a kernel built with
# CONFIG_UNICODE). Under WSL2 you do not need this script at all - point the env
# var at a /mnt/c path and run pytest directly, since the Windows drive is
# case-insensitive. The repo is mounted read-only; the project's own `.venv` is
# never touched (a throwaway venv is built inside the container).
#
# Usage (Docker must be available; the container runs --privileged for the loop
# mount):
#   tests/run_case_insensitive_fs.sh
#   tests/run_case_insensitive_fs.sh -k test_skip_with_existing_file -v
#
# Extra args are forwarded to pytest. Override the container image with
# BOTO3_S3_CI_FS_IMAGE (default: python:3.10-bookworm, the supported Python floor).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." # repo root
repo="$PWD"
image="${BOTO3_S3_CI_FS_IMAGE:-python:3.10-bookworm}"

# Default selection: the case-insensitive-only tests. The env var only steers the
# `case_insensitive_workdir` fixture, so other tests (including the lib
# TestCaseConflictGate cases, which assert that twins land in SEPARATE files -
# true only on a case-sensitive FS) keep using the normal tmp dir. Anything
# passed on the command line replaces this default.
pytest_args=("-k" "existing_file")
if [ "$#" -gt 0 ]; then
    pytest_args=("$@")
fi

command -v docker >/dev/null || { echo "docker is required but not found" >&2; exit 1; }

exec docker run --rm --privileged \
    -v "${repo}:/repo:ro" \
    -e "PYTEST_ARGS=$(printf '%q ' "${pytest_args[@]}")" \
    "$image" bash -euc '
    apt-get update -qq >/dev/null && apt-get install -y -qq dosfstools >/dev/null
    # A case-insensitive loopback filesystem; the fixture creates its work dirs here.
    dd if=/dev/zero of=/ci.img bs=1M count=256 status=none
    mkfs.vfat /ci.img >/dev/null
    mkdir -p /mnt/ci && mount -o loop,umask=000 /ci.img /mnt/ci
    printf probe >/mnt/ci/Probe.txt
    [ -f /mnt/ci/probe.txt ] || { echo "mounted FS is not case-insensitive" >&2; exit 1; }
    export BOTO3_S3_PYTEST_CASE_INSENSITIVE_DIR=/mnt/ci
    # Build a throwaway venv (the read-only host .venv would be the wrong Python);
    # both workspace packages are needed for the CLI ports.
    pip install -q uv
    export UV_PROJECT_ENVIRONMENT=/tmp/venv UV_CACHE_DIR=/tmp/uvcache
    cd /repo
    uv sync --all-packages --frozen -q
    # PYTEST_ARGS is shell-quoted on the host (printf %q), so restore the exact
    # argv - a -k expression with spaces stays one word - before forwarding.
    # shellcheck disable=SC2086
    eval set -- ${PYTEST_ARGS}
    echo "--- pytest (case-conflict dirs on a case-insensitive FS) $* ---"
    uv run pytest tests/ --ignore=tests/cli/e2e "$@" -o cache_dir=/tmp/pcache
'
