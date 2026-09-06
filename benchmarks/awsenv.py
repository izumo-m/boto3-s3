"""Shared AWS environment resolution for the benchmark harness and EC2 lane.

Two callers need the same answers about the ambient AWS environment: the E2E
harness (`e2e.py`), which builds an S3 client and decides whether it is
pointed at local MinIO or real S3, and the EC2 launcher (`ec2.py`), which
provisions an instance and must not misfire against a MinIO-configured shell.
Keeping the resolution here means both agree on region precedence and on what
counts as "a MinIO shell".
"""

from __future__ import annotations

import os
import urllib.parse

from benchmarks.core import BenchmarkError

# Hosts an `AWS_ENDPOINT_URL_S3` may name and still be the local MinIO stack
# (the e2e suite's endpoint), not real S3.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# Set to 1 to accept a non-local endpoint, or to run against real S3 with no
# endpoint override at all. Off by default so a bare run (a shell that never
# sourced scripts/minio-env.sh) fails fast instead of creating a bucket and
# moving hundreds of MB on real S3.
ALLOW_REMOTE_ENV = "BOTO3_S3_BENCH_ALLOW_REMOTE"


def endpoint_host() -> str | None:
    """The hostname of `AWS_ENDPOINT_URL_S3`, or None when it is unset."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3")
    if not endpoint:
        return None
    return urllib.parse.urlsplit(endpoint).hostname


def targeting_local_minio() -> bool:
    """True when `AWS_ENDPOINT_URL_S3` names a local host (the MinIO stack)."""
    return endpoint_host() in LOCAL_HOSTS


def resolve_region(explicit: str | None = None) -> str | None:
    """Resolve the AWS region the way a careful tool should, or None.

    Precedence: an explicit value, then `AWS_REGION` (which aws-cli honors but
    this project's botocore floor does not read), then `AWS_DEFAULT_REGION`,
    then the active profile's configured region. Returning None lets the caller
    decide whether a missing region is fatal (real S3) or has a safe default
    (MinIO reports us-east-1).
    """
    for candidate in (explicit, os.environ.get("AWS_REGION"), os.environ.get("AWS_DEFAULT_REGION")):
        if candidate:
            return candidate
    import botocore.session

    session = botocore.session.Session(profile=os.environ.get("AWS_PROFILE"))
    region = session.get_config_variable("region")
    return region or None


def refuse_minio_env() -> None:
    """Abort if the shell is configured for local MinIO.

    The EC2 launcher makes real AWS calls (S3 for the code hand-off, EC2/STS
    for provisioning). An `AWS_ENDPOINT_URL_S3` left over from
    scripts/minio-env.sh would silently redirect every S3 call to MinIO - the
    code tarball upload most damagingly - so the launcher refuses to start in
    such a shell rather than half-work. Run it from a clean shell instead.
    """
    problems: list[str] = []
    for key in ("AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"):
        if os.environ.get(key):
            problems.append(f"{key}={os.environ[key]}")
    if os.environ.get("AWS_ACCESS_KEY_ID") == "minioadmin":
        problems.append("AWS_ACCESS_KEY_ID=minioadmin")
    if problems:
        raise BenchmarkError(
            "this shell looks configured for local MinIO ("
            + ", ".join(problems)
            + "); the EC2 lane makes real AWS calls. Run it from a shell that has "
            "not sourced scripts/minio-env.sh."
        )
