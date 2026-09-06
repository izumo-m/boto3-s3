"""Provision a throwaway EC2 instance, run the benchmarks on it, tear it down.

Why a lane at all: the local MinIO lane measures a differential on a noisy
host against a zero-latency, no-TLS, unbounded-bandwidth endpoint. It is the
right regression signal but the wrong absolute picture. A quiet, fixed-size
EC2 instance against real S3 in one region is the environment `aws s3` is
actually run in, and its "instance type + AMI + region" is a specification
someone else can reproduce. This lane is for recording a baseline, not for
every commit.

The instance does the work; this module only orchestrates. It resolves the
AMI and architecture, hands the working tree to the instance over S3, waits
for a completion marker, pulls the results back into `benchmarks/results/`,
and terminates the instance. The instance is given no long-lived state and
three independent reasons to die (a timed `shutdown` armed before anything
else, `InstanceInitiatedShutdownBehavior=terminate`, and this module's own
terminate call), because the one real cost risk here is a leaked instance.

`setup-iam` (one-time, needs an administrator) creates the instance role;
`run` provisions and measures; `cleanup` terminates anything a crashed `run`
left tagged. See design/benchmark.md "Recording a baseline on EC2".
"""

from __future__ import annotations

import secrets
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks import awsenv
from benchmarks.core import BenchmarkError

if TYPE_CHECKING:
    import argparse

REPO_ROOT = Path(__file__).resolve().parent.parent

# Everything this lane creates carries this tag, so `cleanup` can find a
# crashed run's leftovers and a human can audit the bill by one filter.
TAG_KEY = "boto3-s3-bench"

# The IAM names `setup-iam` creates and `run` expects. The instance profile is
# what an instance actually references; it wraps the role of the same name.
ROLE_NAME = "boto3-s3-bench"
INSTANCE_PROFILE_NAME = "boto3-s3-bench"

# Public SSM parameters for the latest Amazon Linux 2023 AMI, per architecture.
# Amazon owns these; reading them needs no special permission.
_AL2023_SSM = {
    "x86_64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "arm64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
}

# Defaults; each is overridable (a CLI flag, then an env var). The pair are the
# same size class on the two architectures, fixed-performance (not burstable),
# with enough vCPU and RAM to keep the transfer pool, CRT's native threads, and
# the harness from contending, and a tmpfs work tree off the EBS path.
DEFAULT_INSTANCE_TYPE = "m7i.xlarge"
DEFAULT_PYTHON = "3.14"
DEFAULT_MAX_MINUTES = 60


def _client(kind: str, region: str, profile: str | None) -> Any:
    import boto3

    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client(kind)


def _pinned_aws_version() -> str:
    """The aws-cli version the goldens and parity suite are pinned to.

    Read from the submodule's `__init__.py` the same way install-awscli.sh
    does, so the instance installs the one reference `aws`. The instance never
    sees the submodule (the code tarball is `git archive HEAD`, which omits
    it), so the version has to travel as a value.
    """
    init = REPO_ROOT / "vendor" / "aws-cli" / "awscli" / "__init__.py"
    for line in init.read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("'")[1]
    raise BenchmarkError(f"could not read the pinned aws-cli version from {init}")


def _archive_working_tree() -> bytes:
    """`git archive HEAD` of the repository as a gzipped tar.

    HEAD, not the working tree: a baseline must be attributable to a commit,
    and archiving HEAD refuses to smuggle uncommitted edits onto the instance.
    The submodule is excluded (archive does not descend into it); the pinned
    aws-cli is installed from its release zip on the instance instead.
    """
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    if dirty:
        raise BenchmarkError(
            "working tree is not clean; a recorded baseline must come from a committed "
            "revision (git archive HEAD ignores uncommitted changes). Commit or stash first."
        )
    proc = subprocess.run(
        ["git", "archive", "--format=tar.gz", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _resolve_architecture(ec2: Any, instance_type: str) -> str:
    """Ask EC2 which architecture an instance type runs, so it is never guessed."""
    resp = ec2.describe_instance_types(InstanceTypes=[instance_type])
    infos = resp.get("InstanceTypes", [])
    if not infos:
        raise BenchmarkError(f"unknown instance type {instance_type!r}")
    arches = infos[0]["ProcessorInfo"]["SupportedArchitectures"]
    for arch in ("x86_64", "arm64"):
        if arch in arches:
            return arch
    raise BenchmarkError(f"{instance_type} reports no supported architecture in {_AL2023_SSM}")


def _resolve_ami(ssm: Any, arch: str) -> str:
    return ssm.get_parameter(Name=_AL2023_SSM[arch])["Parameter"]["Value"]


def _boot_bucket_name(account: str, region: str) -> str:
    """A per-account, per-region bucket for the code hand-off and results.

    Distinct from the benchmark's own throwaway bucket: this one persists
    between runs (creating it is not free of eventual-consistency quirks) and
    only ever holds a code tarball and each run's results under a run prefix.
    """
    return f"boto3-s3-bench-boot-{account}-{region}"


def _ensure_boot_bucket(s3: Any, name: str, region: str) -> None:
    from tests.utils.harness import create_bucket_in_region

    if any(b["Name"] == name for b in s3.list_buckets().get("Buckets", [])):
        return
    create_bucket_in_region(s3, name)


def _user_data(
    *, boot_bucket: str, run_id: str, region: str, python: str, aws_version: str, max_minutes: int
) -> str:
    """The cloud-init script the instance runs as root.

    It arms a timed shutdown before doing anything else, unpacks the tree from
    a presigned URL (so no AWS CLI is needed just to fetch it), provisions the
    environment, runs both modes against real S3, and uploads the results and
    log. A trap writes a `DONE` marker and the log on every exit path - success
    or early failure - so the launcher never waits on a dead instance, then
    powers off.
    """
    # `{{` / `}}` are literal braces for the shell; the rest is str.format.
    return _USER_DATA_TEMPLATE.format(
        max_minutes=max_minutes,
        boot_bucket=boot_bucket,
        run_id=run_id,
        region=region,
        python=python,
        aws_version=aws_version,
    )


_USER_DATA_TEMPLATE = r"""#!/bin/bash
exec > /var/log/bench-userdata.log 2>&1
set -x
# Safety net #1: power off after the budget no matter what happens below.
shutdown -h +{max_minutes} &

BOOT={boot_bucket}
RUN={run_id}
export AWS_REGION={region}
export AWS_DEFAULT_REGION={region}
RC=unknown

finish() {{
  cd /opt/bench 2>/dev/null || true
  if [ -x .venv/bin/python ]; then
    ./.venv/bin/python - <<PYEOF || true
import boto3
s3 = boto3.client("s3", region_name="{region}")
try:
    s3.upload_file("/var/log/bench-userdata.log", "$BOOT", "$RUN/bench-userdata.log")
except Exception as exc:
    print("log upload failed:", exc)
s3.put_object(Bucket="$BOOT", Key="$RUN/DONE", Body=b"rc=$RC")
PYEOF
  fi
  shutdown -h now
}}
trap finish EXIT

dnf -y install tar gzip
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
export PATH=/usr/local/bin:$PATH
export HOME=/root

mkdir -p /opt/bench && cd /opt/bench
# The launcher put a presigned GET here; fetch with curl so we need no AWS CLI
# before the venv exists.
curl -fsSL "$(cat /run/bench-tarball-url 2>/dev/null || echo PLACEHOLDER)" -o repo.tar.gz
tar xzf repo.tar.gz

export UV_PYTHON={python}
uv sync --all-packages --locked
scripts/install-awscli.sh {aws_version}

export BOTO3_S3_BENCH_ALLOW_REMOTE=1
export PATH="$PWD/.venv/bin:$PATH"
set +e
uv run python -m benchmarks run --engine both --mode all
RC=$?
set -e

./.venv/bin/python - <<PYEOF
import boto3, glob, os
s3 = boto3.client("s3", region_name="{region}")
for f in sorted(glob.glob("benchmarks/results/*.jsonl")):
    s3.upload_file(f, "$BOOT", "$RUN/results/" + os.path.basename(f))
    print("uploaded", f)
PYEOF
"""


def _write_tarball_url_step(user_data: str, presigned_url: str) -> str:
    """Inline the presigned tarball URL into the user-data.

    The URL is signed and can be long; keeping it out of the template and
    substituting a single placeholder avoids any str.format collision with the
    query string's own braces or percent-encoding.
    """
    marker = "$(cat /run/bench-tarball-url 2>/dev/null || echo PLACEHOLDER)"
    replacement = "'" + presigned_url + "'"
    return user_data.replace(marker, replacement)


def _estimated_note(instance_type: str, region: str) -> str:
    return (
        f"about to launch one on-demand {instance_type} in {region}; a run is ~30-40 min, "
        "so on-demand instance cost is typically well under US$1 plus S3 request charges."
    )


def cmd_run(args: argparse.Namespace) -> int:
    awsenv.refuse_minio_env()
    region = awsenv.resolve_region(args.region)
    if region is None:
        raise BenchmarkError("no region; pass --region or set AWS_REGION / a profile with one")
    profile = args.profile

    ec2 = _client("ec2", region, profile)
    ssm = _client("ssm", region, profile)
    s3 = _client("s3", region, profile)
    sts = _client("sts", region, profile)

    account = sts.get_caller_identity()["Account"]
    instance_type = args.instance_type
    arch = _resolve_architecture(ec2, instance_type)
    ami = _resolve_ami(ssm, arch)
    aws_version = _pinned_aws_version()

    _require_instance_profile(ec2, region, profile)

    print(f"account {account}, region {region}")
    print(f"instance type {instance_type} ({arch}), AMI {ami}, Python {args.python}")
    print(f"pinned aws-cli {aws_version}")
    print(_estimated_note(instance_type, region))

    boot_bucket = _boot_bucket_name(account, region)
    _ensure_boot_bucket(s3, boot_bucket, region)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)

    tarball_key = f"{run_id}/repo.tar.gz"
    s3.put_object(Bucket=boot_bucket, Key=tarball_key, Body=_archive_working_tree())
    presigned = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": boot_bucket, "Key": tarball_key},
        ExpiresIn=3600,
    )

    user_data = _user_data(
        boot_bucket=boot_bucket,
        run_id=run_id,
        region=region,
        python=args.python,
        aws_version=aws_version,
        max_minutes=args.max_minutes,
    )
    user_data = _write_tarball_url_step(user_data, presigned)

    instance_id = _launch(
        ec2,
        ami=ami,
        instance_type=instance_type,
        user_data=user_data,
        run_id=run_id,
    )
    print(f"launched {instance_id}; run id {run_id}")

    try:
        ok = _await_completion(s3, ec2, boot_bucket, run_id, instance_id, args.max_minutes)
        _download_results(s3, boot_bucket, run_id)
        if not ok:
            _dump_diagnostics(s3, ec2, boot_bucket, run_id, instance_id)
    finally:
        if args.keep:
            print(f"--keep: leaving {instance_id} running (it self-terminates at the budget)")
        else:
            ec2.terminate_instances(InstanceIds=[instance_id])
            print(f"terminated {instance_id}")

    print(
        "\nresults downloaded to benchmarks/results/; render with "
        "`uv run python -m benchmarks report`"
    )
    return 0 if ok else 2


def _require_instance_profile(ec2: Any, region: str, profile: str | None) -> None:
    iam = _client("iam", region, profile)
    try:
        iam.get_instance_profile(InstanceProfileName=INSTANCE_PROFILE_NAME)
    except iam.exceptions.NoSuchEntityException as exc:
        raise BenchmarkError(
            f"instance profile {INSTANCE_PROFILE_NAME!r} does not exist; run "
            "`python -m benchmarks ec2 setup-iam` once (needs an administrator)"
        ) from exc


def _launch(ec2: Any, *, ami: str, instance_type: str, user_data: str, run_id: str) -> str:
    resp = ec2.run_instances(
        ImageId=ami,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        UserData=user_data,
        IamInstanceProfile={"Name": INSTANCE_PROFILE_NAME},
        InstanceInitiatedShutdownBehavior="terminate",  # safety net #2
        MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": TAG_KEY, "Value": run_id}, {"Key": "Name", "Value": TAG_KEY}],
            }
        ],
    )
    return resp["Instances"][0]["InstanceId"]


def _await_completion(
    s3: Any, ec2: Any, boot_bucket: str, run_id: str, instance_id: str, max_minutes: int
) -> bool:
    """Poll for the DONE marker, giving up if the instance dies or time runs out.

    Returns True only on a marker that reports rc=0. A marker with a non-zero
    rc, a terminated instance, or the deadline all return False - the caller
    then pulls whatever diagnostics exist.
    """
    from botocore.exceptions import ClientError

    deadline = time.time() + max_minutes * 60 + 300  # instance budget plus boot slack
    marker = f"{run_id}/DONE"
    while time.time() < deadline:
        try:
            body = s3.get_object(Bucket=boot_bucket, Key=marker)["Body"].read().decode()
        except ClientError:
            body = None
        if body is not None:
            rc = body.strip()
            print(f"instance finished: {rc}")
            return rc == "rc=0"
        state = _instance_state(ec2, instance_id)
        if state in ("terminated", "stopped", "shutting-down"):
            print(f"instance entered {state!r} without a DONE marker")
            return False
        time.sleep(20)
    print("timed out waiting for the instance to finish")
    return False


def _instance_state(ec2: Any, instance_id: str) -> str:
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            return inst["State"]["Name"]
    return "unknown"


def _download_results(s3: Any, boot_bucket: str, run_id: str) -> None:
    dest = REPO_ROOT / "benchmarks" / "results"
    dest.mkdir(parents=True, exist_ok=True)
    prefix = f"{run_id}/results/"
    resp = s3.list_objects_v2(Bucket=boot_bucket, Prefix=prefix)
    for obj in resp.get("Contents", []):
        name = obj["Key"].rsplit("/", 1)[-1]
        s3.download_file(boot_bucket, obj["Key"], str(dest / name))
        print(f"downloaded {name}")


def _dump_diagnostics(s3: Any, ec2: Any, boot_bucket: str, run_id: str, instance_id: str) -> None:
    """Print the instance log for a failed run: its uploaded log, else the console."""
    from botocore.exceptions import ClientError

    try:
        log = s3.get_object(Bucket=boot_bucket, Key=f"{run_id}/bench-userdata.log")
        print("---- instance log (tail) ----")
        print("\n".join(log["Body"].read().decode(errors="replace").splitlines()[-40:]))
        return
    except ClientError:
        pass
    console = ec2.get_console_output(InstanceId=instance_id).get("Output", "")
    print("---- instance console (tail) ----")
    print("\n".join(console.splitlines()[-40:]) or "(no console output yet)")


def cmd_setup_iam(args: argparse.Namespace) -> int:
    """Create the instance role and profile the instance assumes (one-time).

    The role's only permission is S3 on the benchmark's own bucket families -
    the throwaway `boto3-s3-bench*` buckets and the `boto3-s3-bench-boot-*`
    hand-off bucket. It needs nothing else: the AMI and architecture are
    resolved by the launcher, and the pinned aws-cli is a public download.
    """
    import json

    awsenv.refuse_minio_env()
    region = awsenv.resolve_region(args.region)
    if region is None:
        raise BenchmarkError("no region; pass --region or set AWS_REGION")
    iam = _client("iam", region, args.profile)

    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="boto3-s3 benchmark instance: S3 on the benchmark buckets only",
            Tags=[{"Key": TAG_KEY, "Value": "role"}],
        )
        print(f"created role {ROLE_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"role {ROLE_NAME} already exists")

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="s3-benchmark-buckets",
        PolicyDocument=json.dumps(instance_policy_document()),
    )
    print("attached inline policy s3-benchmark-buckets")

    try:
        iam.create_instance_profile(InstanceProfileName=INSTANCE_PROFILE_NAME)
        print(f"created instance profile {INSTANCE_PROFILE_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"instance profile {INSTANCE_PROFILE_NAME} already exists")
    try:
        iam.add_role_to_instance_profile(
            InstanceProfileName=INSTANCE_PROFILE_NAME, RoleName=ROLE_NAME
        )
        print("linked role to instance profile")
    except iam.exceptions.LimitExceededException:
        print("role already linked to instance profile")
    print("IAM ready; `python -m benchmarks ec2 run` can now launch instances")
    return 0


def instance_policy_document() -> dict[str, Any]:
    """The instance role's inline policy: S3 on the benchmark bucket families only."""
    bench = "arn:aws:s3:::boto3-s3-bench*"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    "s3:ListBucketMultipartUploads",
                    "s3:GetBucketLocation",
                ],
                "Resource": bench,
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:CreateBucket",
                    "s3:DeleteBucket",
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:DeleteObject",
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                "Resource": [bench, bench + "/*"],
            },
        ],
    }


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Terminate any still-running instance a crashed run left tagged."""
    awsenv.refuse_minio_env()
    region = awsenv.resolve_region(args.region)
    if region is None:
        raise BenchmarkError("no region; pass --region or set AWS_REGION")
    ec2 = _client("ec2", region, args.profile)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag-key", "Values": [TAG_KEY]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
    ids = [
        inst["InstanceId"]
        for reservation in resp.get("Reservations", [])
        for inst in reservation.get("Instances", [])
    ]
    if not ids:
        print("no tagged benchmark instances to clean up")
        return 0
    ec2.terminate_instances(InstanceIds=ids)
    print(f"terminated {', '.join(ids)}")
    return 0
