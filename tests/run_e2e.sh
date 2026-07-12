#!/usr/bin/env bash
#
# Run the e2e / CRT parity suite against a REAL S3 bucket.
#
# Creates a unique, empty bucket, runs `pytest tests/cli/e2e`, and ALWAYS
# force-removes the bucket (plus the mb/rb siblings) on exit. The largest object
# the suite transfers is 9 MiB (multipart coverage); everything else is tiny.
#
# Required env (dedicated to e2e, so nothing leaks from your normal shell):
#   BOTO3_S3_E2E_PROFILE   AWS named profile   -> exported as AWS_PROFILE
#   BOTO3_S3_E2E_REGION    AWS region          -> exported as AWS_REGION / AWS_DEFAULT_REGION
#
# Extra args are forwarded to pytest, e.g.:
#   BOTO3_S3_E2E_PROFILE=myprofile BOTO3_S3_E2E_REGION=ap-northeast-1 \
#       tests/run_e2e.sh -k cp -x
#
set -euo pipefail

: "${BOTO3_S3_E2E_PROFILE:?set BOTO3_S3_E2E_PROFILE to the AWS profile for e2e}"
: "${BOTO3_S3_E2E_REGION:?set BOTO3_S3_E2E_REGION to the AWS region for e2e}"

cd "$(dirname "${BASH_SOURCE[0]}")/.." # repo root

# Prefer the pinned aws in the project venv (scripts/install-awscli.sh) for the
# script's own aws calls too, so they match the aws the suite runs (uv run puts
# .venv/bin on PATH). Falls back to the host aws when the venv has none.
PATH="$PWD/.venv/bin:$PATH"

export AWS_REGION="$BOTO3_S3_E2E_REGION"
export AWS_DEFAULT_REGION="$AWS_REGION"
# Resolve the profile's credentials into env vars and drop AWS_PROFILE: a test
# that supplies its own AWS_CONFIG_FILE (the CRT lane writes a [default] config
# selecting the CRT engine) must not be shadowed by a named profile's config.
creds="$(aws configure export-credentials --profile "$BOTO3_S3_E2E_PROFILE" --format env)" \
    || { echo "could not resolve credentials for profile $BOTO3_S3_E2E_PROFILE" >&2; exit 1; }
# Also drop any stale session credentials from the calling shell: a static-key
# profile's export-credentials emits no AWS_SESSION_TOKEN line, so eval would
# leave the outer shell's token alive and mismatched with the new access key.
unset AWS_PROFILE AWS_ENDPOINT_URL_S3 AWS_ENDPOINT_URL AWS_SESSION_TOKEN AWS_CREDENTIAL_EXPIRATION
eval "$creds"

suffix="$(openssl rand -hex 5 2>/dev/null \
    || python3 -c 'import secrets; print(secrets.token_hex(5))' 2>/dev/null \
    || echo "${RANDOM}${RANDOM}")"
export BOTO3_S3_E2E_BUCKET="boto3s3-e2e-${suffix}"
bucket="$BOTO3_S3_E2E_BUCKET"

cleanup() {
    echo "--- cleanup: force-remove test buckets ---"
    for name in "$bucket" "${bucket}-mb" "${bucket}-rb"; do
        if aws s3 rb "s3://${name}" --force >/dev/null 2>&1; then
            echo "removed  s3://${name}"
        else
            echo "absent   s3://${name}"
        fi
    done
}
trap cleanup EXIT

echo "--- account=$(aws sts get-caller-identity --query Account --output text) \
region=${AWS_REGION} bucket=${bucket} ---"
aws s3 mb "s3://${bucket}"

echo "--- uv run pytest tests/cli/e2e ${*} ---"
uv run pytest tests/cli/e2e "$@"
