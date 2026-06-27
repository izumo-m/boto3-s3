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

export AWS_PROFILE="$BOTO3_S3_E2E_PROFILE"
export AWS_REGION="$BOTO3_S3_E2E_REGION"
export AWS_DEFAULT_REGION="$AWS_REGION"
# Talk to real AWS via the profile only: drop any MinIO endpoint / static creds
# a normal shell might carry.
unset AWS_ENDPOINT_URL_S3 AWS_ENDPOINT_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

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
