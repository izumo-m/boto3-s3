"""Shared test harness: CLI runners, bucket seeding/cleanup, ls normalization.

Used by every CLI test tier (``tests/cli/``): the awscli ports and unit tests
run the CLI in-process, the functional (moto) and e2e (MinIO / real S3)
suites additionally need seeding, cleanup, and output normalization.
"""

from __future__ import annotations

import contextlib
import io
import re
import shutil
import subprocess
import urllib.parse
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from boto3_s3_cli.commands.base import Context

_SUBPROCESS_TIMEOUT = 60.0

# Token the goldens use in place of the concrete bucket name, so the same
# golden replays against any bucket (moto's fixed bucket, the e2e bucket).
BUCKET_TOKEN = "<BUCKET>"

# ``aws s3 ls`` object/bucket lines start with a 19-column local-time
# timestamp. Capture/replay happen at different times (and possibly in
# different timezones), so goldens mask it. Anchored at line start: PRE lines
# (leading spaces) and summarize lines pass through untouched.
_LS_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
TIMESTAMP_TOKEN = "<TIMESTAMP>"

# Presign URL masks (normalize_presign_stdout): the endpoint and the
# time/credential-dependent query values follow the environment, not the CLI.
ENDPOINT_TOKEN = "<ENDPOINT>"
ACCESS_KEY_TOKEN = "<ACCESS_KEY>"
DATE_TOKEN = "<DATE>"
AMZ_DATE_TOKEN = "<AMZ_DATE>"
SIGNATURE_TOKEN = "<SIGNATURE>"
REGION_TOKEN = "<REGION>"


class CliResult(NamedTuple):
    """Outcome of one CLI invocation (in-process or subprocess)."""

    rc: int
    stdout: str
    stderr: str


def run_cli_in_process(argv: list[str], *, ctx: Context | None = None) -> CliResult:
    """Invoke ``boto3_s3_cli.cli.main`` in-process, capturing output and rc.

    ``main`` absorbs ``SystemExit`` itself and always returns an int, so no
    extra handling is needed here.
    """
    from boto3_s3_cli import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv, ctx=ctx)
    return CliResult(rc, out.getvalue(), err.getvalue())


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: str | None = None,
    input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> CliResult:
    proc_env = None
    if env is not None:
        # Merge over the inherited environment (the e2e suite needs the host's
        # credentials/endpoint intact); the overrides win (the CRT lane points
        # AWS_CONFIG_FILE at a preferred_transfer_client=crt profile).
        import os

        proc_env = {**os.environ, **env}
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        timeout=_SUBPROCESS_TIMEOUT,
        cwd=cwd,
        input=input_bytes,
        env=proc_env,
    )
    return CliResult(
        proc.returncode,
        proc.stdout.decode(errors="replace"),
        proc.stderr.decode(errors="replace"),
    )


def run_cli_subprocess(argv: list[str], *, cwd: str | None = None) -> CliResult:
    """Run the installed ``boto3-s3`` console script - the real user entry point.

    Resolved via PATH: ``uv run pytest`` puts the project venv's bin dir
    there. The host environment (AWS_ENDPOINT_URL_S3, credentials) is
    inherited unchanged, which is what the e2e suite needs. ``cwd`` runs the
    CLI from a scenario workdir so relative local paths (and their rendering
    in transfer result lines) are cwd-stable for goldens.
    """
    exe = shutil.which("boto3-s3")
    if exe is None:
        pytest.fail("boto3-s3 console script not on PATH; run the suite via `uv run pytest`")
    return _run_subprocess([exe, *argv], cwd=cwd)


def run_aws_subprocess(argv: list[str], *, cwd: str | None = None) -> CliResult:
    """Run ``aws s3 <argv...>`` with the host environment inherited unchanged."""
    aws = shutil.which("aws")
    if aws is None:
        pytest.fail("aws v2 binary not on PATH (required for the e2e parity suite)")
    return _run_subprocess([aws, "s3", *argv], cwd=cwd)


def run_cli_subprocess_with_stdin(
    argv: list[str],
    *,
    cwd: str | None = None,
    stdin_payload: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> CliResult:
    """Like :func:`run_cli_subprocess`, feeding *stdin_payload* to the process.

    *env* overlays the inherited environment (the CRT parity lane points
    ``AWS_CONFIG_FILE`` at a ``preferred_transfer_client=crt`` profile).
    """
    exe = shutil.which("boto3-s3")
    if exe is None:
        pytest.fail("boto3-s3 console script not on PATH; run the suite via `uv run pytest`")
    return _run_subprocess([exe, *argv], cwd=cwd, input_bytes=stdin_payload, env=env)


def run_aws_subprocess_with_stdin(
    argv: list[str],
    *,
    cwd: str | None = None,
    stdin_payload: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> CliResult:
    """Like :func:`run_aws_subprocess`, feeding *stdin_payload* to the process."""
    aws = shutil.which("aws")
    if aws is None:
        pytest.fail("aws v2 binary not on PATH (required for the e2e parity suite)")
    return _run_subprocess([aws, "s3", *argv], cwd=cwd, input_bytes=stdin_payload, env=env)


class _BinaryStdinShim:
    """A ``sys.stdin`` stand-in exposing ``buffer`` (what ``cp -`` reads)."""

    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


class _BinaryStdoutShim:
    """A ``sys.stdout`` stand-in capturing both text writes and ``buffer`` bytes."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.text_parts: list[str] = []

    def write(self, text: str) -> int:
        self.text_parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass


def run_cli_in_process_streaming(
    argv: list[str], *, ctx: Context | None = None, stdin_payload: bytes | None = None
) -> CliResult:
    """In-process run for the ``-`` streaming paths.

    ``run_cli_in_process`` captures stdout into a text ``StringIO``, which has
    no ``buffer`` for a streaming download to write object bytes into; this
    variant installs binary-capable shims instead and merges the captured
    bytes (decoded) with any text writes into ``CliResult.stdout`` - in
    practice exactly one of the two is non-empty.
    """
    import sys

    from boto3_s3_cli import cli

    stdout_shim = _BinaryStdoutShim()
    err = io.StringIO()
    original_stdin = sys.stdin
    if stdin_payload is not None:
        sys.stdin = _BinaryStdinShim(stdin_payload)  # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(stdout_shim), contextlib.redirect_stderr(err):  # type: ignore[arg-type]
            rc = cli.main(argv, ctx=ctx)
    finally:
        sys.stdin = original_stdin
    stdout = stdout_shim.buffer.getvalue().decode(errors="replace") + "".join(
        stdout_shim.text_parts
    )
    return CliResult(rc, stdout, err.getvalue())


def seed_bucket(client: Any, bucket: str, seed: Mapping[str, int | bytes]) -> None:
    """Put one object per entry: an ``int`` means ``b"x" * size``, ``bytes``
    are the exact body (transfer scenarios compare downloaded content)."""
    for key, spec in seed.items():
        body = spec if isinstance(spec, bytes) else b"x" * spec
        client.put_object(Bucket=bucket, Key=key, Body=body)


def delete_keys(client: Any, bucket: str, *keys: str) -> None:
    """Delete the listed keys from *bucket* (no-op for an empty list).

    Tests call this in their cleanup path with exactly the keys they put;
    already-absent keys are silently ignored by ``DeleteObjects``.
    """
    objects = [{"Key": key} for key in keys]
    for i in range(0, len(objects), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": objects[i : i + 1000]})


def delete_under(client: Any, bucket: str, prefix: str) -> None:
    """Delete every object under *prefix* in *bucket*.

    A cleanup helper for prefixes the test owns - not a "purge bucket" call,
    even though ``prefix=""`` would behave like one.
    """
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    delete_keys(client, bucket, *keys)


def normalize_ls_stdout(stdout: str, *, bucket: str) -> list[str]:
    """Normalize ``ls`` stdout for golden storage / cross-run comparison.

    Per line: the leading timestamp is masked with ``TIMESTAMP_TOKEN`` and the
    concrete bucket name is replaced with ``BUCKET_TOKEN``. Nothing else
    changes - in particular the lines are **not sorted** (``ls`` output order
    is deterministic and itself parity-relevant) and no line is dropped (the
    blank line before the ``--summarize`` footer is part of the contract).
    ``splitlines()`` absorbs ``\\r\\n`` from aws on Windows.
    """
    lines: list[str] = []
    for line in stdout.splitlines():
        line = _LS_TIMESTAMP_RE.sub(TIMESTAMP_TOKEN, line)
        lines.append(line.replace(bucket, BUCKET_TOKEN))
    return lines


def normalize_rm_stdout(stdout: str, *, bucket: str) -> list[str]:
    """Normalize ``rm`` stdout for golden storage / cross-run comparison.

    The bucket name is tokenized like ``ls``, but the lines are **sorted** -
    the opposite of :func:`normalize_ls_stdout`, deliberately: aws-cli's
    delete lines come out in parallel-completion order, which is
    nondeterministic run to run (observed on MinIO with 30 keys), so the line
    *set* is the contract, not the order. ``rm`` stdout is delete lines only,
    so sorting loses nothing; what sorting relaxes, the end-state
    (``remaining_keys``) comparison pins back down.

    The bucket-lifecycle commands (``mb`` / ``rb``) share this normalization:
    their lines carry no timestamps, and ``rb --force`` interleaves the inner
    rm's nondeterministically ordered delete lines; their end-state pin is
    ``capture_bucket_state``. ``website`` shares it too - its stdout is
    empty in every outcome, so any normalization fits; sharing keeps the
    bucket-config family uniform.
    """
    return sorted(line.replace(bucket, BUCKET_TOKEN) for line in stdout.splitlines())


def normalize_cp_stdout(stdout: str, *, bucket: str) -> list[str]:
    """Normalize ``cp`` stdout for golden storage / cross-run comparison.

    aws prints progress with the carriage-return rewrite protocol - no isatty
    gate, so piped stdout carries ``Completed ...`` segments joined by ``\\r``
    and result lines right-padded over them. Per physical line, the segment
    after the last ``\\r`` (what a terminal would leave visible) is kept,
    padding is stripped, and pure progress statements (``Completed ...``) are
    dropped - their byte/speed text is time-dependent. What remains are the
    transfer result lines, bucket-tokenized and **sorted** (parallel
    completion order, the rm rationale); the end-state fields of the golden
    (``remaining_keys`` / ``local_tree``) pin what sorting relaxes.
    """
    lines: list[str] = []
    for raw in stdout.splitlines():
        line = raw.rsplit("\r", 1)[-1].rstrip()
        if not line or line.startswith("Completed "):
            continue
        lines.append(line.replace(bucket, BUCKET_TOKEN))
    return sorted(lines)


def capture_local_tree(root: str) -> list[str]:
    """The local end state a transfer scenario is compared on.

    One ``relpath:size:sha256-prefix`` entry per file under *root*
    (``/``-separated, sorted). Mtimes are deliberately not captured - the
    download mtime stamp is asserted live per side, not via goldens.
    """
    import hashlib
    import os

    entries: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()[:8]
            entries.append(f"{rel}:{os.path.getsize(full)}:{digest}")
    return sorted(entries)


def head_object_fields(
    client: Any, bucket: str, key: str, fields: tuple[str, ...]
) -> dict[str, Any]:
    """Selected HeadObject fields - how transfer scenarios pin object shape
    (ContentType / Metadata / StorageClass...) in goldens; absent fields are
    ``None`` so the JSON stays comparable across endpoints."""
    response = client.head_object(Bucket=bucket, Key=key)
    return {field: response.get(field) for field in fields}


def _normalize_presign_url(url: str, *, bucket: str, mask_region: str | None) -> str:
    parts = urllib.parse.urlsplit(url)
    path = parts.path
    if parts.netloc.startswith(f"{bucket}."):
        # Virtual-host style (the moto replay runs without an endpoint
        # override): canonicalize to path-style so it compares equal to a
        # path-style capture (MinIO's IP endpoint).
        path = f"/{bucket}{path}"
    normalized = f"{ENDPOINT_TOKEN}{path}".replace(bucket, BUCKET_TOKEN)
    if not parts.query:
        return normalized
    params: list[str] = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if key == "X-Amz-Date":
            value = AMZ_DATE_TOKEN
        elif key == "X-Amz-Signature":
            value = SIGNATURE_TOKEN
        elif key == "X-Amz-Credential":
            # "<access-key>/<yyyymmdd>/<region>/s3/aws4_request": mask the
            # first two segments, keep the scope - the region is parity. When
            # the scope region matches the environment default (*mask_region*)
            # it is folded to <REGION> too, so a golden captured in one region
            # replays in any e2e region; an explicit --region stays raw (the
            # e2e same-run check then keeps that region under test).
            scope = value.split("/", 2)[-1]
            if mask_region is not None:
                scope_region, sep, rest = scope.partition("/")
                if scope_region == mask_region:
                    scope = f"{REGION_TOKEN}{sep}{rest}"
            value = f"{ACCESS_KEY_TOKEN}/{DATE_TOKEN}/{scope}"
        params.append(f"{key}={value}")
    return normalized + "?" + "&".join(params)


def normalize_presign_stdout(
    stdout: str, *, bucket: str, mask_region: str | None = None
) -> list[str]:
    """Normalize ``presign`` stdout (a URL line) for goldens / comparison.

    The endpoint (``scheme://netloc``) is masked after canonicalizing a
    virtual-host URL (bucket in the netloc) to path-style, so the moto
    replay (no endpoint override -> virtual-host) compares equal to the
    MinIO capture (IP endpoint -> path-style). Query values are
    percent-decoded for readable goldens, and the time/credential-dependent
    ones are masked: ``X-Amz-Date``, ``X-Amz-Signature``, and the access
    key + date inside ``X-Amz-Credential``. The credential scope keeps its
    region except when it equals *mask_region* (the environment default,
    from ``presign_scope_mask_region``), which is folded to <REGION> so a
    golden does not drift across e2e regions; an explicit --region region
    differs from the default and stays raw. Everything else is the contract:
    parameter *order* included (botocore emits a fixed order),
    ``X-Amz-Expires``, ``X-Amz-SignedHeaders``, and the key path. Non-URL
    lines just get the bucket tokenized.
    """
    lines: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(("http://", "https://")):
            lines.append(_normalize_presign_url(line, bucket=bucket, mask_region=mask_region))
        else:
            lines.append(line.replace(bucket, BUCKET_TOKEN))
    return lines


def presign_scope_mask_region(argv: Sequence[str]) -> str | None:
    """The credential-scope region to fold to <REGION> for a presign golden.

    The scope carries whatever region botocore signed with: the environment
    default (``AWS_REGION`` / ``AWS_DEFAULT_REGION``) unless *argv* passes an
    explicit ``--region``. Masking the environment default keeps a golden
    stable across e2e runs in any region (docs/testing.md endpoint policy
    step 1), while an explicit ``--region`` stays raw so its region is still
    compared verbatim (that region is the point of the scenario). When the
    two coincide the explicit one wins - nothing is masked - so the
    ``--region`` golden never drifts either.
    """
    import os

    explicit: str | None = None
    if "--region" in argv:
        index = list(argv).index("--region")
        if index + 1 < len(argv):
            explicit = argv[index + 1]
    default = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not default or default == explicit:
        return None
    return default


def presign_scope_region(stdout: str) -> str | None:
    """The raw credential-scope region of a presign URL (or ``None``).

    Reads the region segment out of ``X-Amz-Credential`` on the first URL
    line, before any masking - the e2e same-run check compares aws's against
    ours so the region the two CLIs sign with must be identical even though
    the normalizer folds the environment default away.
    """
    for line in stdout.splitlines():
        if not line.startswith(("http://", "https://")):
            continue
        query = urllib.parse.urlsplit(line).query
        for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
            if key == "X-Amz-Credential":
                segments = value.split("/")
                if len(segments) >= 3:
                    return segments[2]
        return None
    return None


def fetch_url(url: str) -> tuple[int, bytes]:
    """HTTP-GET *url*, returning ``(status, body)`` without raising on 4xx/5xx.

    For the e2e fetch checks: whether the endpoint *accepts* a presigned
    URL's signature is parity the URL string alone cannot prove.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def bucket_exists(client: Any, bucket: str) -> bool:
    """Whether *bucket* exists (HeadBucket; any ClientError counts as "no")."""
    from botocore.exceptions import ClientError

    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        return False
    return True


def create_bucket_in_region(client: Any, bucket: str) -> None:
    """Create *bucket*, honoring the client's region.

    Real S3 rejects a plain CreateBucket outside us-east-1 with
    ``IllegalLocationConstraintException``, so any other region must be passed
    as ``CreateBucketConfiguration.LocationConstraint``; us-east-1 (the region
    MinIO reports) forbids the field and takes the plain form. The mb/rb parity
    suites pre-create their sibling bucket through here so their run_e2e.sh
    lane works in any region.
    """
    region = client.meta.region_name
    if region and region != "us-east-1":
        client.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    else:
        client.create_bucket(Bucket=bucket)


def force_delete_bucket(client: Any, bucket: str) -> None:
    """Purge *bucket* and delete it; a missing bucket is a no-op.

    The bucket-lifecycle suites (mb/rb) reset their sibling bucket with this
    between the aws and boto3-s3 runs and in fixture teardown.
    """
    from botocore.exceptions import ClientError

    try:
        delete_under(client, bucket, "")
        client.delete_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("NoSuchBucket", "404"):
            raise


def capture_bucket_state(client: Any, bucket: str) -> tuple[bool, list[str] | None]:
    """The end state a bucket-lifecycle command is compared on.

    Returns ``(exists, remaining_keys)``; the keys are ``None`` when the
    bucket does not exist (distinct from "exists and empty").
    """
    if not bucket_exists(client, bucket):
        return (False, None)
    return (True, remaining_keys(client, bucket))


def remaining_keys(client: Any, bucket: str) -> list[str]:
    """Every key left in *bucket*, sorted - the end-state a destructive
    command is compared on (complements the sorted-stdout relaxation)."""
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return sorted(keys)


def assert_stderr_tokens(tokens: tuple[str, ...], stderr: str, *, side: str, scenario: str) -> None:
    """Assert each token appears in *stderr* (substring check, never equality).

    aws-cli and boto3-s3 wrap error text differently, so stderr is only ever
    probed for stable tokens (error codes, option names).
    """
    missing = [token for token in tokens if token not in stderr]
    if missing:
        pytest.fail(f"[{scenario}] {side} stderr is missing expected tokens {missing!r}:\n{stderr}")
