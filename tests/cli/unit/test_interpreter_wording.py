"""Reports quoting the interpreter's own text read as aws's bundled 3.14 words it.

aws prints ``str(exc)`` for a bare ``int()`` failure and for whatever reaches
its general handler, so where that text is the interpreter's the parity target
is the official build's Python (design/cli.md section 2). The older hosts word
two shapes differently: ``int()``'s digit limit before 3.11 and ``json``'s
trailing-comma failures before 3.13. On a newer host these tests pass without
the rewrite; the dev and CI floor (3.10) is where they bite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from boto3_s3_cli import cli, usage

_DIGIT_LIMIT = (
    "Exceeds the limit (4300 digits) for integer string conversion: value has 5000 "
    "digits; use sys.set_int_max_str_digits() to increase the limit"
)


class TestExceptionText:
    def test_the_digit_limit_gains_its_unit(self) -> None:
        exc = ValueError(
            "Exceeds the limit (4300) for integer string conversion: value has 5000 "
            "digits; use sys.set_int_max_str_digits() to increase the limit"
        )
        assert usage.exception_text(exc) == _DIGIT_LIMIT

    @pytest.mark.parametrize(
        ("doc", "expected"),
        [
            ('{"a": 1,}', "Illegal trailing comma before end of object: line 1 column 8 (char 7)"),
            ("[1, 2,\n]", "Illegal trailing comma before end of array: line 1 column 6 (char 5)"),
            (
                '{"a": 1,',
                "Expecting property name enclosed in double quotes: line 1 column 9 (char 8)",
            ),
        ],
    )
    def test_a_json_decode_failure_reads_as_on_3_13(self, doc: str, expected: str) -> None:
        with pytest.raises(json.JSONDecodeError) as exc_info:
            json.loads(doc)
        assert usage.exception_text(exc_info.value) == expected

    def test_anything_else_is_its_str(self) -> None:
        assert usage.exception_text(KeyError("Bucket")) == "'Bucket'"
        assert usage.exception_text(ValueError("Invalid endpoint: x")) == "Invalid endpoint: x"


class TestReports:
    @pytest.mark.parametrize("option", ["--cli-read-timeout", "--cli-connect-timeout"])
    def test_a_timeout_past_the_digit_limit(
        self, option: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
        rc = cli.main(["ls", "s3://b", option, "1" * 5000])
        assert rc == 255
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {_DIGIT_LIMIT}\n"

    def test_a_config_value_the_sdk_converts_past_the_digit_limit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore's own int() of max_attempts: a bare ValueError the general
        # report renders.
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1" * 5000)
        rc = cli.main(["ls", "s3://b"])
        assert rc == 255
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {_DIGIT_LIMIT}\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="a POSIX credential_process script")
    def test_a_credential_process_answer_with_a_trailing_comma(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        script = tmp_path / "creds.sh"
        script.write_text('#!/bin/sh\nprintf \'%s\' \'{"Version": 1, "AccessKeyId": "a",}\'\n')
        script.chmod(0o755)
        config = tmp_path / "config"
        config.write_text(f"[default]\ncredential_process = {script}\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
        rc = cli.main(["ls", "s3://b"])
        assert rc == 255
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: Illegal trailing comma before end of object: "
            "line 1 column 34 (char 33)\n"
        )
