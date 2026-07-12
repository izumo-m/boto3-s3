@echo off
rem scripts/minio-env.cmd - scripts/minio-env.sh's Windows twin (same values).
rem
rem cmd.exe cannot export into the calling shell the way `source` does, so
rem this is a runner: it sets the MinIO environment, then executes the rest
rem of its command line in that environment (cwd is left untouched):
rem
rem   cmd.exe /c "scripts\minio-env.cmd uv run pytest -q tests\cli\e2e"
rem
rem Keep the values in lockstep with scripts/minio-env.sh.
set "AWS_ENDPOINT_URL_S3=http://127.0.0.1:9000"
set "AWS_ACCESS_KEY_ID=minioadmin"
set "AWS_SECRET_ACCESS_KEY=minioadmin"
set "AWS_REGION=us-east-1"
set "BOTO3_S3_E2E_BUCKET=boto3-s3-e2e"
rem The pinned aws.exe (scripts\install-awscli.cmd) shadows any system
rem install for the suite, mirroring how .venv/bin/aws wins on Linux.
if exist "%LOCALAPPDATA%\boto3-s3\aws-cli\current\aws.exe" set "PATH=%LOCALAPPDATA%\boto3-s3\aws-cli\current;%PATH%"
%*
