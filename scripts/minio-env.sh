# scripts/minio-env.sh - point AWS tooling at the local MinIO stack.
#
# Source this file (it only exports environment variables, no side effects):
#
#   scripts/compose-up.sh        # start MinIO first (separate concern)
#   source scripts/minio-env.sh
#   uv run pytest                # full suite including tests/cli/e2e
#   aws s3 ls s3://test-bucket/  # or poke at MinIO manually
#
# AWS_ENDPOINT_URL_S3 is honored by both aws-cli v2 and botocore, so neither
# tool needs an explicit --endpoint-url. BOTO3_S3_E2E_BUCKET gates the e2e
# suite (tests/cli/e2e/conftest.py); the bucket is created by mc-init
# (compose.dev.yaml) and must stay empty between test runs.
#
# Windows twin (same values, runner form): scripts/minio-env.cmd
# (docs/testing.md section 8).

export AWS_ENDPOINT_URL_S3=http://127.0.0.1:9000
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_REGION=us-east-1
export BOTO3_S3_E2E_BUCKET=boto3-s3-e2e
