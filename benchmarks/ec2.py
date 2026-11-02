"""Provision a throwaway EC2 instance, run the benchmarks on it, tear it down.

Why a lane at all: the local dev stack lane measures a differential on a noisy
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
two independent reasons to die - a timed `shutdown` it arms before anything
else, and this module's own terminate call - with
`InstanceInitiatedShutdownBehavior=terminate` making every shutdown a
termination, because the one real cost risk here is a leaked instance. Both
need something to run (the user data, this launcher); when neither does,
`cleanup` is what stops the instance.

The image is Ubuntu LTS (the local lane's distribution, so glibc and the
kernel generation match across lanes), resolved from Canonical's public SSM
parameters; the release is a knob.

`setup-iam` (one-time, needs an administrator) creates the instance role;
`run` provisions and measures; `cleanup` terminates anything a crashed `run`
left tagged and deletes the benchmark buckets it left. See design/benchmark.md
"Recording a baseline on EC2".
"""

from __future__ import annotations

import contextlib
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks import awsenv
from benchmarks.core import BenchmarkError

if TYPE_CHECKING:
    import argparse
    from collections.abc import Generator, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

# Everything this lane creates carries this tag, so `cleanup` can find a
# crashed run's leftovers and a human can audit the bill by one filter.
TAG_KEY = "boto3-s3-bench"

# The IAM names `setup-iam` creates and `run` expects. The instance profile is
# what an instance actually references; it wraps the role of the same name.
ROLE_NAME = "boto3-s3-bench"
INSTANCE_PROFILE_NAME = "boto3-s3-bench"

# The image is Ubuntu LTS, resolved from Canonical's public SSM parameters
# (reading them needs no special permission). Ubuntu rather than Amazon Linux
# because the local lane develops and measures on Ubuntu (WSL2): the same
# glibc and kernel generation on both lanes leaves hardware, network, and real
# S3 as the only differences between them. The release is a knob so a
# baseline can also be taken on the previous LTS.
DEFAULT_UBUNTU_RELEASE = "26.04"
_UBUNTU_SSM_ARCH = {"x86_64": "amd64", "arm64": "arm64"}


def ubuntu_ssm_parameter(release: str, arch: str) -> str:
    """Canonical's parameter naming the current stable Ubuntu Server AMI."""
    return (
        f"/aws/service/canonical/ubuntu/server/{release}/stable/current/"
        f"{_UBUNTU_SSM_ARCH[arch]}/hvm/ebs-gp3/ami-id"
    )


# Defaults; each is overridable (a CLI flag, then an env var). The pair are the
# same size class on the two architectures, fixed-performance (not burstable),
# with enough vCPU and RAM to keep the transfer pool, CRT's native threads, and
# the harness from contending, and a tmpfs work tree off the EBS path.
DEFAULT_INSTANCE_TYPE = "m7i.xlarge"
DEFAULT_PYTHON = "3.14"
DEFAULT_MAX_MINUTES = 60
# Against real S3 on a 12.5 Gbps NIC a 64 MB object moves in well under the
# startup constant; 1 GB puts the single-object rows a clear second above it.
DEFAULT_LARGE_TRANSFER_MB = 1024


def _s3_config() -> Any:
    """Client config for the launcher's S3 calls, the presigned URLs above all.

    botocore presigns S3 URLs with SigV2 and rewrites the host to the global
    endpoint (`bucket.s3.amazonaws.com`) unless told otherwise. A bucket
    created minutes earlier in another region answers there with a 307 to its
    regional endpoint, which `curl -T` does not follow: the instance's log and
    DONE PUTs silently failed that way on the first run (the tarball GET only
    worked because it ran with `-L`). SigV4 plus virtual-host addressing keeps
    every URL on the bucket's regional endpoint, where no redirect happens.
    """
    from botocore.config import Config

    return Config(signature_version="s3v4", s3={"addressing_style": "virtual"})


def _client(kind: str, region: str, profile: str | None) -> Any:
    import boto3

    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client(kind, config=_s3_config() if kind == "s3" else None)


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


def _pinned_uv_version() -> str:
    """The uv version mise.toml pins for development, CI and the release workflows."""
    path = REPO_ROOT / "mise.toml"
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "uv":
            return value.strip().strip('"')
    raise BenchmarkError(f"could not read the pinned uv version from {path}")


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


def _describe_instance_type(ec2: Any, instance_type: str) -> tuple[str, int]:
    """Ask EC2 what an instance type is: ``(architecture, memory in MiB)``.

    Both answers come from the service so neither is guessed: the
    architecture picks the AMI, the memory bounds the tmpfs work tree.
    """
    resp = ec2.describe_instance_types(InstanceTypes=[instance_type])
    infos = resp.get("InstanceTypes", [])
    if not infos:
        raise BenchmarkError(f"unknown instance type {instance_type!r}")
    arches = infos[0]["ProcessorInfo"]["SupportedArchitectures"]
    memory_mib = int(infos[0]["MemoryInfo"]["SizeInMiB"])
    for arch in ("x86_64", "arm64"):
        if arch in arches:
            return arch, memory_mib
    raise BenchmarkError(
        f"{instance_type} reports no supported architecture in {sorted(_UBUNTU_SSM_ARCH)}"
    )


# Memory the instance keeps for itself when the tmpfs work tree is sized:
# the OS, the two CLIs (the CRT lane's native threads included), and the
# harness's own seeding.
_INSTANCE_HEADROOM_MIB = 4096


def _tmpfs_gb(large_mb: int, memory_mib: int, *, instance_type: str) -> int:
    """Size the tmpfs work tree, or refuse a payload the instance cannot hold.

    Peak occupancy is about three payloads - the upload source, the download
    source, and one download destination - plus the small-file corpora; the
    harness frees each engine's work tree before the next engine seeds its
    own, so the two engines do not stack. The rest of the instance's memory
    stays free for everything else.
    """
    need_gb = 2 + 3 * large_mb // 1024
    if need_gb * 1024 + _INSTANCE_HEADROOM_MIB > memory_mib:
        raise BenchmarkError(
            f"--large-transfer-mb {large_mb} needs a {need_gb} GB tmpfs work tree, which "
            f"{instance_type} ({memory_mib} MiB) cannot hold with {_INSTANCE_HEADROOM_MIB} MiB "
            "left over; pick a smaller size or a larger instance"
        )
    return need_gb


def _resolve_ami(ssm: Any, arch: str, release: str) -> str:
    from botocore.exceptions import ClientError

    name = ubuntu_ssm_parameter(release, arch)
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        raise BenchmarkError(
            f"no Ubuntu {release} AMI parameter for {arch} in this region ({name}); "
            "pass --ubuntu-release with an LTS Canonical publishes there"
        ) from exc


def _root_device_name(ec2: Any, ami: str) -> str:
    """The AMI's root device name, so the root volume resize hits the root.

    Distributions differ (Ubuntu AMIs use /dev/sda1, Amazon Linux /dev/xvda);
    naming the wrong device would attach a second volume and leave the root at
    the image's default size.
    """
    images = ec2.describe_images(ImageIds=[ami]).get("Images", [])
    if not images:
        raise BenchmarkError(f"AMI {ami} not found")
    return str(images[0]["RootDeviceName"])


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
    *,
    boot_bucket: str,
    run_id: str,
    region: str,
    python: str,
    aws_version: str,
    uv_version: str,
    max_minutes: int,
    large_mb: int,
    tmpfs_gb: int,
    git_rev: str,
    lane: str,
    image: str,
    bench_bucket: str,
) -> str:
    """The cloud-init script the instance runs as root.

    It arms a timed shutdown before anything else, quiets Ubuntu's background
    apt and snap activity so nothing competes with the measurement, provisions
    under errexit with an ERR trap that names the failing line, unpacks the
    tree from a presigned URL, runs both modes against real S3, and uploads
    the results. An EXIT trap uploads the log and a `DONE` marker carrying the
    outcome on every path - success, a failed provisioning step, a failed
    results upload - through two presigned PUT URLs the launcher minted, so
    reporting needs neither credentials nor a CLI on the instance and works
    however early the failure came; then it powers off. The launcher therefore
    never waits on a dead instance and always has a reason to show. The three
    URLs are spliced in afterwards by `_inline_urls`.
    """
    # `{{` / `}}` are literal braces for the shell; the rest is str.format.
    return _USER_DATA_TEMPLATE.format(
        max_minutes=max_minutes,
        boot_bucket=boot_bucket,
        run_id=run_id,
        region=region,
        python=python,
        aws_version=aws_version,
        uv_version=uv_version,
        large_mb=large_mb,
        tmpfs_gb=tmpfs_gb,
        git_rev=git_rev,
        lane=lane,
        image=image,
        bench_bucket=bench_bucket,
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

# Every exit path lands here: upload the log and a DONE marker carrying RC
# through the launcher's presigned PUTs (no credentials, no CLI, so this
# works before anything was provisioned), then power off.
finish() {{
  trap - ERR
  set +e
  curl -fsS --retry 3 -T /var/log/bench-userdata.log "LOG_PUT_URL_PLACEHOLDER"
  printf 'rc=%s' "$RC" > /run/bench-done
  curl -fsS --retry 3 -T /run/bench-done "DONE_PUT_URL_PLACEHOLDER"
  shutdown -h now
}}
trap finish EXIT
# A shutdown from outside (spot reclaim, the budget timer) arrives as SIGTERM:
# record that it was a signal and let the EXIT trap report; the launcher reads
# the instance's stop reason to say which.
trap 'RC="terminated-by-signal:$RC"; exit 143' TERM
# A failing provisioning step names its line in RC and exits (errexit), so
# the DONE marker says where it died.
trap 'RC="failed-at-line-$LINENO"' ERR
set -e
# A failed download piped into `sh` must fail its own line, not the next one.
set -o pipefail
# Sync the log every minute so even a stop that leaves no time for the trap
# leaves a recent record, and the launcher can show progress meanwhile. The
# loop runs without xtrace so its own curl is not the log's newest line each
# minute (the launcher shows that line as progress).
( set +x
  while sleep 60; do
    # The subshell inherits errexit: one failed upload must not end the loop.
    curl -fsS -T /var/log/bench-userdata.log "LOG_PUT_URL_PLACEHOLDER" >/dev/null 2>&1 || true
  done ) &

export DEBIAN_FRONTEND=noninteractive
# Keep the instance quiet for the measurement: no unattended upgrades, apt
# timers, or snap refreshes competing for CPU and network mid-run. Stopping
# unattended-upgrades also waits out the dpkg lock a first boot may hold.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service \
  snapd.service snapd.socket snapd.seeded.service || true
apt-get -o DPkg::Lock::Timeout=300 -q update
# unzip for the aws-cli release zip install-awscli.sh unpacks (tar/gzip ship).
apt-get -o DPkg::Lock::Timeout=300 -q install -y unzip
# cloud-init may run this without HOME; the installer and uv both want one.
export HOME=/root
# The uv mise.toml pins for every other lane, so the interpreter build it picks
# and its lockfile handling do not change with the day of the run. No shell
# profile edits: nothing here reads them.
export UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1
curl -LsSf https://astral.sh/uv/{uv_version}/install.sh | sh
export PATH=/usr/local/bin:$PATH

# The work tree lives on tmpfs so EBS throughput is off the measured path.
mkdir -p /mnt/bench && mount -t tmpfs -o size={tmpfs_gb}g tmpfs /mnt/bench
export TMPDIR=/mnt/bench
mkdir -p /opt/bench && cd /opt/bench
curl -fsSL "TARBALL_URL_PLACEHOLDER" -o repo.tar.gz
tar xzf repo.tar.gz

# The same interpreter source as the local lane (scripts/bench-env.sh): a
# uv-managed build, never the distribution's python3.14, which Ubuntu ships
# and uv would otherwise pick up as satisfying the request.
export UV_PYTHON={python}
export UV_PYTHON_PREFERENCE=only-managed
uv sync --all-packages --locked
scripts/install-awscli.sh {aws_version}

export BOTO3_S3_BENCH_ALLOW_REMOTE=1
# Provenance: the tree is an archive with no .git, so the launcher supplies
# the commit it archived (always clean), the lane this run belongs to, and
# the image it runs on.
export BOTO3_S3_BENCH_GIT_REV={git_rev}
export BOTO3_S3_BENCH_LANE={lane}
export BOTO3_S3_BENCH_IMAGE={image}
# The launcher names the run's own S3 bucket so it can remove exactly that one
# if the run is cut off before the harness's own delete.
export BOTO3_S3_BENCH_BUCKET={bench_bucket}
export PATH="$PWD/.venv/bin:$PATH"

# Results go up after each mode (the venv's boto3 with the instance role; the
# file names are only known then), so an interruption during the second mode
# still leaves the first one's file. A failed upload is spelled into RC
# instead of ending the script silently.
UPLOAD_FAILED=0
upload_results() {{
  ./.venv/bin/python - <<PYEOF || UPLOAD_FAILED=1
import boto3, glob, os
s3 = boto3.client("s3", region_name="{region}")
for f in sorted(glob.glob("benchmarks/results/*.jsonl")):
    s3.upload_file(f, "$BOOT", "$RUN/results/" + os.path.basename(f))
    print("uploaded", f)
PYEOF
}}

# The benchmark's own exit codes are outcomes, not script failures: no
# errexit and no ERR trap while it runs.
trap - ERR
set +e
uv run python -m benchmarks run --mode inprocess --large-transfer-mb {large_mb}
RC_INPROCESS=$?
upload_results
uv run python -m benchmarks run --mode e2e --engine both --large-transfer-mb {large_mb}
RC_E2E=$?
upload_results
set -e
if [ "$RC_INPROCESS" = 0 ] && [ "$RC_E2E" = 0 ]; then
  RC=0
else
  RC="inprocess=$RC_INPROCESS,e2e=$RC_E2E"
fi
if [ "$UPLOAD_FAILED" = 1 ]; then
  RC="$RC,results-upload-failed"
fi
"""


URL_PLACEHOLDERS = (
    "TARBALL_URL_PLACEHOLDER",
    "LOG_PUT_URL_PLACEHOLDER",
    "DONE_PUT_URL_PLACEHOLDER",
)


def _inline_urls(user_data: str, urls: dict[str, str]) -> str:
    """Splice the presigned URLs into the rendered user-data.

    They are signed and long; keeping them out of the template and replacing
    placeholders afterwards avoids any str.format collision with a query
    string's own braces. Each placeholder sits inside the template's double
    quotes, which is all the quoting a presigned URL needs: the characters
    that would break out of them never occur in one, and a URL that carried
    them is refused rather than spliced in.
    """
    for placeholder, url in urls.items():
        if any(ch in url for ch in '"$`\\'):
            raise BenchmarkError(f"presigned URL contains a shell-active character: {url}")
        assert placeholder in user_data, placeholder
        user_data = user_data.replace(placeholder, url)
    missing = [ph for ph in URL_PLACEHOLDERS if ph in user_data]
    if missing:
        raise BenchmarkError(f"user-data placeholders left unfilled: {missing}")
    return user_data


def _estimated_note(instance_type: str, region: str, *, spot: bool) -> str:
    market = "spot (one-time)" if spot else "on-demand"
    return (
        f"about to launch one {market} {instance_type} in {region}; a run is ~30-40 min, so "
        "instance cost is typically well under US$1, plus about 200k PUT-class S3 requests "
        "at the default scale (about US$0.95 at the ap-northeast-1 rate)."
    )


@dataclass(frozen=True)
class Outcome:
    """How a run ended, as far as the launcher can tell from outside.

    `kind` is ``completed`` when the DONE marker arrived (`rc` is its text),
    ``died`` when the instance reached a terminal state with no marker (`state`
    and EC2's `reason` say what it looked like), ``timeout`` when the launcher
    stopped waiting. `minutes` is wall-clock since launch.
    """

    kind: str
    rc: str | None
    state: str | None
    reason: str | None
    minutes: float

    @property
    def ok(self) -> bool:
        return self.kind == "completed" and self.rc == "0"


def cmd_run(args: argparse.Namespace) -> int:
    import boto3

    awsenv.refuse_dev_stack_env()
    profile = args.profile
    region = awsenv.resolve_region(args.region, profile=profile)
    if region is None:
        raise BenchmarkError("no region; pass --region or set AWS_REGION / a profile with one")
    spot = not args.on_demand

    ec2 = _client("ec2", region, profile)
    ssm = _client("ssm", region, profile)
    s3 = _client("s3", region, profile)
    sts = _client("sts", region, profile)

    account = sts.get_caller_identity()["Account"]
    instance_type = args.instance_type
    arch, memory_mib = _describe_instance_type(ec2, instance_type)
    tmpfs_gb = _tmpfs_gb(args.large_transfer_mb, memory_mib, instance_type=instance_type)
    ami = _resolve_ami(ssm, arch, args.ubuntu_release)
    root_device = _root_device_name(ec2, ami)
    aws_version = _pinned_aws_version()
    uv_version = _pinned_uv_version()

    _require_instance_profile(ec2, region, profile)

    print(f"account {account}, region {region}")
    print(
        f"instance type {instance_type} ({arch}, {memory_mib} MiB), Ubuntu "
        f"{args.ubuntu_release} AMI {ami} (root {root_device}), Python {args.python}, "
        f"large transfer {args.large_transfer_mb} MB on a {tmpfs_gb} GB tmpfs"
    )
    print(f"pinned aws-cli {aws_version}, uv {uv_version}")
    print(_estimated_note(instance_type, region, spot=spot))
    credentials = boto3.Session(profile_name=profile, region_name=region).get_credentials()
    if credentials is not None and credentials.get_frozen_credentials().token:
        # A URL presigned with temporary credentials stops working when they
        # expire, whatever its own expiry says; the instance reports only
        # through such URLs.
        print(
            "note: these credentials are temporary; the instance's report URLs stop working "
            f"when they expire, so they need more than {args.max_minutes + 15} min left"
        )

    boot_bucket = _boot_bucket_name(account, region)
    _ensure_boot_bucket(s3, boot_bucket, region)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    # The run's own bucket, named here so that whatever cuts the run short,
    # this launcher can remove exactly this one afterwards. Inside the family
    # the instance role is scoped to.
    bench_bucket = f"boto3-s3-bench-{run_id}"

    tarball_key = f"{run_id}/repo.tar.gz"
    s3.put_object(Bucket=boot_bucket, Key=tarball_key, Body=_archive_working_tree())
    keep_tarball = False
    try:
        with _finally_on_termination():
            git_rev = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            user_data = _inline_urls(
                _user_data(
                    boot_bucket=boot_bucket,
                    run_id=run_id,
                    region=region,
                    python=args.python,
                    aws_version=aws_version,
                    uv_version=uv_version,
                    max_minutes=args.max_minutes,
                    large_mb=args.large_transfer_mb,
                    tmpfs_gb=tmpfs_gb,
                    git_rev=git_rev,
                    lane=f"ec2-{instance_type}-ubuntu{args.ubuntu_release}",
                    image=ami,
                    bench_bucket=bench_bucket,
                ),
                _presign(s3, boot_bucket, run_id, tarball_key, args.max_minutes),
            )
            instance_id = _launch(
                ec2,
                ami=ami,
                root_device=root_device,
                instance_type=instance_type,
                user_data=user_data,
                run_id=run_id,
                spot=spot,
            )
            launched = time.monotonic()
            print(f"launched {instance_id} ({'spot' if spot else 'on-demand'}); run id {run_id}")

            outcome: Outcome | None = None
            results_back: list[str] = []
            try:
                outcome = _await_completion(
                    s3, ec2, boot_bucket, run_id, instance_id, args.max_minutes, launched=launched
                )
                results_back = _download_results(s3, boot_bucket, run_id)
            finally:
                # The launcher stopped first (its own timeout, an interrupt, an
                # error while waiting): the instance may still be working.
                stopped_first = outcome is None or outcome.kind == "timeout"
                if args.keep and stopped_first:
                    keep_tarball = True
                    print(
                        f"--keep: leaving {instance_id} to finish on its own (it self-terminates "
                        f"at the budget), with its bucket {bench_bucket} and the code tarball; "
                        "`ec2 cleanup` removes the bucket"
                    )
                else:
                    _teardown(ec2, s3, instance_id, bench_bucket, wait=stopped_first)
    finally:
        # The tarball is the one sizeable object; the log and results stay
        # under the run prefix (kilobytes) as the run's record on the bucket.
        # Removed on every ending but a kept instance's, which may not have
        # fetched it yet: `cleanup` leaves the hand-off bucket alone.
        if not keep_tarball:
            try:
                s3.delete_object(Bucket=boot_bucket, Key=tarball_key)
            except Exception as exc:  # report, never mask the outcome
                print(f"could not remove s3://{boot_bucket}/{tarball_key}: {exc}")

    # The wait returned (an exception would have ended the run above).
    assert outcome is not None
    downloaded = len(results_back)
    ok = outcome.ok and downloaded > 0
    verdict = _classify(outcome, args.max_minutes, user_data, results_back=results_back)
    if outcome.ok and downloaded == 0:
        verdict = "the run reported success but no results file came back"
    elif ok and downloaded < 2:
        missing = [mode for mode in _MODES if mode not in _modes_back(results_back)]
        print(f"no results file came back for the {' and '.join(missing)} mode")
    print(f"\noutcome: {verdict}")
    if not ok:
        _dump_diagnostics(s3, ec2, boot_bucket, run_id, instance_id, outcome)
    else:
        print(
            "results downloaded to benchmarks/results/; render with "
            "`uv run python -m benchmarks report`"
        )
    return 0 if ok else 2


@contextlib.contextmanager
def _finally_on_termination() -> Generator[None]:
    """Turn SIGTERM and SIGHUP into SystemExit while the run is out.

    A signal's default action ends the process without running `finally`, so
    a closed terminal would leave the instance to its budget, and its bucket
    and the code tarball to `cleanup`; as an exception it unwinds through the
    teardown like an interrupt does.
    """
    import signal

    def exit_on(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:  # no SIGHUP on Windows
            continue
        previous[number] = signal.signal(number, exit_on)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _presign(
    s3: Any, boot_bucket: str, run_id: str, tarball_key: str, max_minutes: int
) -> dict[str, str]:
    """The three URLs the user data uses, valid past the instance's budget.

    The instance reports through the PUTs, so the final DONE still has to
    land after a full run.
    """
    expiry = max_minutes * 60 + 900
    return {
        "TARBALL_URL_PLACEHOLDER": s3.generate_presigned_url(
            "get_object", Params={"Bucket": boot_bucket, "Key": tarball_key}, ExpiresIn=expiry
        ),
        "LOG_PUT_URL_PLACEHOLDER": s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": boot_bucket, "Key": f"{run_id}/bench-userdata.log"},
            ExpiresIn=expiry,
        ),
        "DONE_PUT_URL_PLACEHOLDER": s3.generate_presigned_url(
            "put_object", Params={"Bucket": boot_bucket, "Key": f"{run_id}/DONE"}, ExpiresIn=expiry
        ),
    }


def _teardown(ec2: Any, s3: Any, instance_id: str, bench_bucket: str, *, wait: bool) -> None:
    """Terminate the instance, then remove the run's bucket; report, never raise.

    The harness deletes its bucket on a clean finish; any other ending leaves
    it, and it is this run's alone, so it is removed here (a no-op when it is
    already gone). With *wait* - the launcher stopped first, so the instance
    may still be writing into the bucket - the termination is waited out
    before the bucket is emptied. Whatever fails is reported and left to
    `ec2 cleanup`.
    """
    from tests.utils.harness import force_delete_bucket

    try:
        ec2.terminate_instances(InstanceIds=[instance_id])
        print(f"terminating {instance_id}")
        if wait and not _wait_terminated(ec2, [instance_id]):
            print(f"{instance_id} is not terminated yet; removing its bucket anyway")
    except Exception as exc:
        print(f"could not terminate {instance_id}: {exc}; `ec2 cleanup` will")
    try:
        force_delete_bucket(s3, bench_bucket)
    except Exception as exc:
        print(f"could not remove {bench_bucket}: {exc}; `ec2 cleanup` will")


def _wait_terminated(ec2: Any, ids: Sequence[str], *, timeout: float = 600.0) -> bool:
    """Wait until every instance in *ids* is `terminated`; False when *timeout* runs out.

    Polls DescribeInstances rather than using botocore's InstanceTerminated
    waiter, which gives up at once on an instance it sees `pending` or
    `stopping`.
    """
    deadline = time.monotonic() + timeout
    while True:
        if all(_instance_status(ec2, instance_id)[0] == "terminated" for instance_id in ids):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(10)


def _require_instance_profile(ec2: Any, region: str, profile: str | None) -> None:
    iam = _client("iam", region, profile)
    try:
        iam.get_instance_profile(InstanceProfileName=INSTANCE_PROFILE_NAME)
    except iam.exceptions.NoSuchEntityException as exc:
        raise BenchmarkError(
            f"instance profile {INSTANCE_PROFILE_NAME!r} does not exist; run "
            "`python -m benchmarks ec2 setup-iam` once (needs an administrator)"
        ) from exc


# ClientError codes that mean "no spot capacity right now" rather than a
# malformed request; they are the ones worth answering with --on-demand.
_SPOT_CAPACITY_CODES = frozenset(
    {
        "InsufficientInstanceCapacity",
        "SpotMaxPriceTooLow",
        "MaxSpotInstanceCountExceeded",
        "InsufficientFreeAddressesInSubnet",
        "Unsupported",
    }
)


def _market_options(spot: bool) -> dict[str, Any]:
    """The run_instances market choice: a one-time spot request, or nothing.

    Spot because the hardware is identical and a benchmark this short (well
    under an hour) is rarely reclaimed; when it is, the run is simply repeated.
    One-time with terminate-on-interruption matches the instance's own
    shutdown behavior, so every ending is a termination.
    """
    if not spot:
        return {}
    return {
        "InstanceMarketOptions": {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate",
            },
        }
    }


def _launch(
    ec2: Any,
    *,
    ami: str,
    root_device: str,
    instance_type: str,
    user_data: str,
    run_id: str,
    spot: bool,
) -> str:
    from botocore.exceptions import ClientError

    try:
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
                    "DeviceName": root_device,
                    "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": True},
                }
            ],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": TAG_KEY, "Value": run_id},
                        {"Key": "Name", "Value": TAG_KEY},
                    ],
                }
            ],
            **_market_options(spot),
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if spot and code in _SPOT_CAPACITY_CODES:
            # Deliberately no silent fallback: paying on-demand is a choice.
            raise BenchmarkError(
                f"no spot capacity for {instance_type} right now ({code}); retry later, "
                "try another instance type, or pass --on-demand"
            ) from exc
        raise
    return resp["Instances"][0]["InstanceId"]


_TERMINAL_STATES = ("terminated", "stopped", "shutting-down", "stopping")


def _await_completion(
    s3: Any,
    ec2: Any,
    boot_bucket: str,
    run_id: str,
    instance_id: str,
    max_minutes: int,
    *,
    launched: float,
) -> Outcome:
    """Wait for the DONE marker, watching the instance and narrating its log.

    The marker is the primary signal. An instance seen in a terminal state is
    given a short grace to land the marker its EXIT trap may still be
    uploading (shutdown runs the trap while the network is still up); only
    then does it count as having died, and EC2's stop reason is read so a spot
    reclaim, the budget timer, and a service-side stop can be told apart. The
    log the instance syncs every minute is tailed as progress.
    """
    from botocore.exceptions import ClientError

    deadline = time.time() + max_minutes * 60 + 300  # instance budget plus boot slack
    marker = f"{run_id}/DONE"
    log_key = f"{run_id}/bench-userdata.log"
    last_shown = ""

    def minutes() -> float:
        return (time.monotonic() - launched) / 60.0

    def marker_text() -> str | None:
        try:
            return s3.get_object(Bucket=boot_bucket, Key=marker)["Body"].read().decode()
        except ClientError:
            return None

    while time.time() < deadline:
        body = marker_text()
        if body is not None:
            rc = body.strip().removeprefix("rc=")
            print(f"instance finished after {minutes():.1f} min: rc={rc}")
            state = reason = None
            if rc.startswith("terminated-by-signal"):
                # Stopped from outside: only EC2's stop reason tells a spot
                # reclaim from the budget timer, and the trap's marker can land
                # before the instance has even left `running`.
                for _ in range(9):
                    state, reason = _instance_status(ec2, instance_id)
                    if state in _TERMINAL_STATES and reason:
                        break
                    time.sleep(10)
            return Outcome("completed", rc, state, reason, minutes())
        state, reason = _instance_status(ec2, instance_id)
        if state in _TERMINAL_STATES:
            for _ in range(9):  # up to 90 s for the trap's upload to land
                time.sleep(10)
                body = marker_text()
                if body is not None:
                    rc = body.strip().removeprefix("rc=")
                    print(f"instance finished after {minutes():.1f} min: rc={rc}")
                    return Outcome("completed", rc, state, reason, minutes())
            print(f"instance entered {state!r} without a DONE marker")
            return Outcome("died", None, state, reason, minutes())
        try:
            log = s3.get_object(Bucket=boot_bucket, Key=log_key)["Body"].read()
        except ClientError:
            log = b""
        lines = [line for line in log.decode(errors="replace").splitlines() if line.strip()]
        if lines and lines[-1] != last_shown:
            last_shown = lines[-1]
            print(f"[instance {minutes():.0f} min] {last_shown[:160]}")
        time.sleep(20)
    state, _reason = _instance_status(ec2, instance_id)
    print(f"gave up waiting after {minutes():.0f} min; the instance is {state!r}")
    return Outcome("timeout", None, state, None, minutes())


def _instance_status(ec2: Any, instance_id: str) -> tuple[str, str | None]:
    """The instance's state name and, when EC2 gives one, its stop reason.

    Just after RunInstances, DescribeInstances may not know the instance yet
    (EC2 is eventually consistent, and botocore does not retry that error);
    it reads as still pending, and the wait's own deadline bounds it.
    """
    from botocore.exceptions import ClientError

    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "InvalidInstanceID.NotFound":
            return "pending", None
        raise
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            state = str(inst["State"]["Name"])
            reason_info = inst.get("StateReason") or {}
            code = reason_info.get("Code")
            message = reason_info.get("Message")
            reason = f"{code}: {message}" if code else (message or None)
            return state, reason
    return "unknown", None


def _download_results(s3: Any, boot_bucket: str, run_id: str) -> list[str]:
    """Pull the run's results files into benchmarks/results/; return their names."""
    dest = REPO_ROOT / "benchmarks" / "results"
    dest.mkdir(parents=True, exist_ok=True)
    prefix = f"{run_id}/results/"
    resp = s3.list_objects_v2(Bucket=boot_bucket, Prefix=prefix)
    names: list[str] = []
    for obj in resp.get("Contents", []):
        name = obj["Key"].rsplit("/", 1)[-1]
        s3.download_file(boot_bucket, obj["Key"], str(dest / name))
        print(f"downloaded {name}")
        names.append(name)
    return names


# The modes the instance runs, in order; a results filename spells its mode.
_MODES = ("inprocess", "e2e")


def _modes_back(results_back: Sequence[str]) -> list[str]:
    """The modes whose results files came back, read from the filenames."""
    return [mode for mode in _MODES if any(f"_{mode}_" in name for name in results_back)]


def _failing_line(user_data: str, rc: str) -> str | None:
    """The user-data line a ``failed-at-line-N`` outcome points at, if any."""
    prefix = "failed-at-line-"
    if not rc.startswith(prefix):
        return None
    number = rc[len(prefix) :].split(":", 1)[0]
    if not number.isdigit():
        return None
    lines = user_data.splitlines()
    index = int(number) - 1
    if 0 <= index < len(lines):
        return lines[index].strip()
    return None


def _classify(
    outcome: Outcome, max_minutes: int, user_data: str, *, results_back: Sequence[str] = ()
) -> str:
    """One sentence on how the run ended, naming the cause where it is known.

    `results_back` names the results files that came back; it separates an
    instance that shut itself down after a full run whose DONE marker never
    arrived (a reporting fault) from one that died before producing anything,
    and says which mode's file is missing in between.
    """
    reason = outcome.reason or ""
    spot_reclaimed = "SpotInstanceTermination" in reason
    over_budget = outcome.minutes >= max_minutes - 1
    if outcome.kind == "timeout":
        return (
            f"the launcher stopped waiting after {outcome.minutes:.0f} min with the instance "
            f"{outcome.state!r}; it self-terminates at its {max_minutes} min budget"
        )
    if outcome.kind == "died":
        if spot_reclaimed:
            return f"spot interruption: EC2 reclaimed the instance mid-run ({reason}); run again"
        if "InstanceInitiatedShutdown" in reason and over_budget:
            return f"budget exceeded: the instance shut itself down at {max_minutes} min"
        if reason.startswith("Server."):
            return f"EC2 stopped the instance on its side ({reason}); run again"
        if "InstanceInitiatedShutdown" in reason and results_back:
            missing = [mode for mode in _MODES if mode not in _modes_back(results_back)]
            ran = (
                f"the {' and '.join(missing)} mode left no results"
                if missing
                else "the benchmark ran to the end"
            )
            return (
                f"the instance shut itself down after uploading {len(results_back)} results "
                f"file(s) but its DONE marker never arrived: {ran}, its exit codes are unknown, "
                "and the report path (the presigned PUTs) needs a look"
            )
        return f"the instance ended {outcome.state!r} without reporting" + (
            f" ({reason})" if reason else ""
        )
    rc = outcome.rc or "unknown"
    if rc == "0":
        return "completed"
    if rc.startswith("terminated-by-signal"):
        if spot_reclaimed:
            return f"spot interruption: EC2 reclaimed the instance mid-run ({reason}); run again"
        if over_budget:
            return f"budget exceeded: the instance shut itself down at {max_minutes} min"
        return f"the instance was shut down from outside mid-run (rc={rc}" + (
            f", {reason})" if reason else ")"
        )
    line = _failing_line(user_data, rc)
    if line is not None:
        return f"provisioning failed at `{line}` (rc={rc})"
    if rc.startswith("inprocess=") or rc.startswith("e2e="):
        note = (
            "the benchmark itself finished with a non-zero exit code (1 = regression flag, "
            "2 = failed scenarios or a harness error)"
        )
        if rc.endswith(",results-upload-failed"):
            note += " and a results upload failed"
        return f"{note}: {rc}"
    if "results-upload-failed" in rc:
        return f"the benchmark ran but a results upload failed ({rc})"
    return f"the instance exited without a recognizable outcome (rc={rc})" + (
        f", {reason}" if reason else ""
    )


def _dump_diagnostics(
    s3: Any, ec2: Any, boot_bucket: str, run_id: str, instance_id: str, outcome: Outcome
) -> None:
    """Print what the instance left behind: its log, or failing that its console."""
    from botocore.exceptions import ClientError

    if outcome.reason:
        print(f"instance stop reason: {outcome.reason}")
    try:
        log = s3.get_object(Bucket=boot_bucket, Key=f"{run_id}/bench-userdata.log")
        print("---- instance log (tail) ----")
        print("\n".join(log["Body"].read().decode(errors="replace").splitlines()[-40:]))
        return
    except ClientError:
        pass
    console = ec2.get_console_output(InstanceId=instance_id).get("Output", "")
    print("---- instance console (tail) ----")
    print("\n".join(console.splitlines()[-40:]) or "(no console output available)")


def cmd_setup_iam(args: argparse.Namespace) -> int:
    """Create the instance role and profile the instance assumes (one-time).

    The role's only permission is S3 on the benchmark's own bucket families -
    the throwaway `boto3-s3-bench*` buckets and the `boto3-s3-bench-boot-*`
    hand-off bucket. It needs nothing else: the AMI and architecture are
    resolved by the launcher, and the pinned aws-cli is a public download.
    """
    import json

    awsenv.refuse_dev_stack_env()
    region = awsenv.resolve_region(args.region, profile=args.profile)
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
    """Remove what a crashed or budget-killed run leaves behind.

    Two kinds of leftover exist. A still-running tagged instance, when the
    launcher died before terminating it. And a per-run benchmark bucket on
    real S3, when the instance was cut off mid-run (budget shutdown, a
    launcher timeout) so the harness's own delete never ran; those names are
    random, so the whole `boto3-s3-bench-*` family in the account is swept,
    the persistent `boto3-s3-bench-boot-*` hand-off buckets excepted. The
    instances go first, and are waited out, so none is still writing into a
    bucket while it is being emptied.
    """
    from botocore.exceptions import ClientError

    from tests.utils.harness import force_delete_bucket

    awsenv.refuse_dev_stack_env()
    region = awsenv.resolve_region(args.region, profile=args.profile)
    if region is None:
        raise BenchmarkError("no region; pass --region or set AWS_REGION")
    ec2 = _client("ec2", region, args.profile)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag-key", "Values": [TAG_KEY]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "shutting-down", "stopping", "stopped"],
            },
        ]
    )
    ids = [
        inst["InstanceId"]
        for reservation in resp.get("Reservations", [])
        for inst in reservation.get("Instances", [])
    ]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        print(f"terminating {', '.join(ids)}")
        if _wait_terminated(ec2, ids):
            print(f"terminated {', '.join(ids)}")
        else:
            print("gave up waiting for the termination; removing the buckets anyway")
    else:
        print("no tagged benchmark instances to clean up")
    s3 = _client("s3", region, args.profile)
    leftovers = [
        b["Name"]
        for b in s3.list_buckets().get("Buckets", [])
        if b["Name"].startswith("boto3-s3-bench-")
        and not b["Name"].startswith("boto3-s3-bench-boot-")
    ]
    for name in leftovers:
        try:
            force_delete_bucket(s3, name)
            print(f"deleted leftover bucket {name}")
        except ClientError as exc:
            print(f"could not delete {name} (another region?): {exc}")
    if not leftovers:
        print("no leftover benchmark buckets")
    return 0
