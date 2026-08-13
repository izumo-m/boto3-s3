"""``BOTO_DISABLE_CRT`` is neutralized for this process, kept for its children.

pip's botocore reads that variable once, while ``botocore.compat`` is imported,
and freezes ``HAS_CRT`` from it; the botocore aws ships has no such switch, so
on that side the variable decides nothing - an MRAP presign still signs with
SigV4a and the CRT checksum families still work with it set (measured against
the pinned aws-cli). ``boto3_s3_cli``'s package ``__init__`` drops it before any
module of the package can reach botocore, which is what reproduces that here.

The freeze is what makes these cases need a fresh interpreter: by the time the
test runner is up, ``HAS_CRT`` is already decided, so each case runs its own
subprocess with the variable in the environment - the mechanism
``test_crt_optional.py`` uses for the opposite condition.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_MRAP = "s3://arn:aws:s3::123456789012:accesspoint/mfzwi23gnjvgw.mrap/key"


def _env(**overrides: str) -> dict[str, str]:
    """The parent environment with the AWS knobs a fresh CLI run needs pinned."""
    env = {
        **os.environ,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        **overrides,
    }
    for key in list(env):
        if key == "AWS_PROFILE" or key.startswith("AWS_ENDPOINT_URL"):
            del env[key]
    env.pop("AWS_SESSION_TOKEN", None)
    return env


def _run(code: str, *argv: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *argv],
        env=_env(**overrides),
        capture_output=True,
        text=True,
        check=False,
    )


class TestTheVariableIsDropped:
    def test_importing_the_package_removes_it_from_the_environment(self) -> None:
        result = _run(
            "import os, boto3_s3_cli; print(repr(os.environ.get('BOTO_DISABLE_CRT')))",
            BOTO_DISABLE_CRT="true",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "None"

    def test_botocore_therefore_keeps_crt_enabled(self) -> None:
        # The contract itself, not just the environment edit: whatever the
        # variable said, botocore's frozen answer is the one aws's botocore
        # gives, which is that CRT is available.
        pytest.importorskip("awscrt")
        result = _run(
            "import boto3_s3_cli\nfrom botocore.compat import HAS_CRT\nprint('HAS_CRT', HAS_CRT)\n",
            BOTO_DISABLE_CRT="true",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "HAS_CRT True"

    def test_an_mrap_presign_still_signs_with_sigv4a(self) -> None:
        # The user-visible consequence: SigV4a needs awscrt through botocore,
        # so with the switch honored this run would be the rc-253 missing
        # dependency instead (aws: rc 0 and this algorithm, measured).
        pytest.importorskip("awscrt")
        result = _run(
            "from boto3_s3_cli.cli import main; raise SystemExit(main())",
            "presign",
            _MRAP,
            BOTO_DISABLE_CRT="true",
        )
        assert result.returncode == 0, result.stderr
        assert "X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256" in result.stdout


class TestTheVariableStillReachesChildren:
    """aws ignores the variable but passes the environment on untouched."""

    def test_child_environ_puts_the_value_back(self) -> None:
        result = _run(
            "import boto3_s3_cli; print(repr(boto3_s3_cli.child_environ()['BOTO_DISABLE_CRT']))",
            BOTO_DISABLE_CRT="true",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "'true'"

    def test_child_environ_adds_nothing_when_it_was_never_set(self) -> None:
        result = _run(
            "import boto3_s3_cli; print('BOTO_DISABLE_CRT' in boto3_s3_cli.child_environ())"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"

    @pytest.mark.skipif(sys.platform == "win32", reason="the external alias here is an sh command")
    def test_an_external_alias_sees_what_the_invocation_was_given(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / ".aws" / "cli").mkdir(parents=True)
        (home / ".aws" / "cli" / "alias").write_text(
            "[command s3]\nshow = !sh -c 'echo \"${BOTO_DISABLE_CRT-unset}\"' sh\n"
        )
        result = _run(
            "from boto3_s3_cli.cli import main; raise SystemExit(main())",
            "show",
            BOTO_DISABLE_CRT="true",
            HOME=str(home),
            USERPROFILE=str(home),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "true"
