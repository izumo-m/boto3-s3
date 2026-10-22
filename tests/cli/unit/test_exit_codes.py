"""Unit tests for the aws-cli exit-code mapping (design/cli.md section 6).

`exit_code_for` is exercised directly for each branch, and `main` is
exercised end-to-end for the paths that do not go through a library error
(usage errors, unknown options) plus the ClientError path through a fake
client, so the parse -> dispatch -> error -> exit-code wiring is covered. The
catch-all backstop (`_exit_code_for_unexpected`: a non-Boto3S3Error escaping
a command -> 252/253/254/255) is covered end-to-end through `main` too, at both
of its sites: around the command, where a failing stdout write lands, and
around the whole dispatch in `main`.
"""

from __future__ import annotations

import argparse
import errno
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import (
    NoCredentialsError,
    NoRegionError,
    ParamValidationError,
    PartialCredentialsError,
)

from boto3_s3 import (
    Boto3S3Error,
    ConfigurationError,
    InvalidConfigError,
    InvalidValueError,
    NotFoundError,
    TransportError,
    ValidationError,
)
from boto3_s3_cli import cli, filters
from boto3_s3_cli.commands import mb
from boto3_s3_cli.commands.base import Context
from tests.utils.fakes3 import MTIME, client_error
from tests.utils.harness import built_client_ctx, unused_ctx


def _with_cause(exc: Boto3S3Error, cause: BaseException) -> Boto3S3Error:
    exc.__cause__ = cause
    return exc


def _every_built_parser() -> list[argparse.ArgumentParser]:
    """Every parser the dispatch or a help page builds, subparsers included."""
    parsers: list[argparse.ArgumentParser] = [
        cli._shared_globals_parent(),
        cli._build_stage1_parser(),
        cli._build_globals_parser(),
        cli.build_parser(),
    ]
    for name in cli._COMMAND_TABLE:
        command = cli._load_command(name)()
        parsers.append(cli._build_command_parser(name, command))
        parsers.append(cli._build_command_parse_parser(name, command))
    parsers.extend(
        subparser
        for parser in list(parsers)
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for subparser in action.choices.values()
    )
    return parsers


class TestExitCodeFor:
    def test_server_side_error_maps_to_254(self) -> None:
        exc = _with_cause(
            NotFoundError("no such bucket"), client_error("NoSuchBucket", 404, "ListObjectsV2")
        )
        assert cli.exit_code_for(exc) == 254

    def test_client_error_cause_wins_over_validation_category(self) -> None:
        # A server-rejected call (HTTP 400) that the library files under
        # ValidationError still exits 254: aws-cli maps every error that
        # reached the server to CLIENT_ERROR_RC.
        exc = _with_cause(
            ValidationError("bad request"), client_error("InvalidRequest", 400, "ListObjectsV2")
        )
        assert cli.exit_code_for(exc) == 254

    def test_client_side_validation_maps_to_252(self) -> None:
        assert cli.exit_code_for(ValidationError("bad page size")) == 252

    def test_configuration_error_maps_to_253(self) -> None:
        exc = ConfigurationError("Unable to locate credentials")
        assert cli.exit_code_for(exc) == 253

    def test_other_errors_map_to_255(self) -> None:
        assert cli.exit_code_for(TransportError("connection reset")) == 255
        assert cli.exit_code_for(Boto3S3Error("unexpected")) == 255

    def test_refining_subclasses_map_to_255_not_their_parents_rc(self) -> None:
        # aws routes post-parse value failures and bad config through its
        # general handler (255); the refining subclasses must NOT inherit
        # ValidationError's 252 / ConfigurationError's 253.
        assert cli.exit_code_for(InvalidValueError("invalid literal for int()")) == 255
        assert cli.exit_code_for(InvalidConfigError("Invalid size value: abc")) == 255

    def test_client_error_cause_wins_over_refining_subclass(self) -> None:
        # The ClientError-cause branch stays first: an error that reached the
        # server exits 254 regardless of the taxonomy class.
        exc = _with_cause(
            InvalidConfigError("rejected"), client_error("InvalidRequest", 400, "ListObjectsV2")
        )
        assert cli.exit_code_for(exc) == 254


class _RaisingClient:
    """Fake S3 client whose page iterator raises the given error on iteration.

    The real paginator's ``paginate()`` only builds the iterator; the failure
    surfaces on the first page fetch, so the fake raises from ``next()``,
    not from ``paginate()`` itself. Any botocore error works, so the library's
    own translation (``s3storage.s3_errors``) runs for real.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def get_paginator(self, name: str) -> Any:
        error = self._error

        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                def pages() -> Any:
                    raise error
                    yield  # pragma: no cover - marks this function as a generator

                return pages()

        return _Paginator()


class _OnePageClient:
    """Fake whose ListObjectsV2 paginator yields a single canned page.

    ``calls`` collects the kwargs each ``paginate`` received, so a test can pin
    which bucket and prefix the parse produced, and ``size`` sets the canned
    key's size for the cases that read the formatted listing back.
    """

    def __init__(self, size: int = 1) -> None:
        self.calls: list[dict[str, Any]] = []
        self._size = size

    def get_paginator(self, name: str) -> Any:
        calls, size = self.calls, self._size

        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                calls.append(kwargs)
                yield {"Contents": [{"Key": "p/x", "Size": size, "LastModified": MTIME}]}

        return _Paginator()


class TestMainExitCodes:
    @pytest.mark.parametrize("name", ["cp", "mv", "rm", "mb", "rb", "presign", "website"])
    def test_missing_required_path_exits_252(
        self, name: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main([name]) == 252
        assert "the following arguments are required" in capsys.readouterr().err

    def test_unknown_option_exits_252_naming_it(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["ls", "--extra-argument-foo", "s3://bucket/p/"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "Unknown options" in err
        assert "--extra-argument-foo" in err

    def test_extra_positional_exits_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["ls", "s3://bucket/p/", "stray"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "Unknown options" in err
        assert "stray" in err

    def test_invalid_choice_exits_252_naming_the_option(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["sync", ".", "s3://bucket/p/", "--checksum-algorithm", "INVALID"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "--checksum-algorithm" in err
        assert "INVALID" in err

    def test_missing_subcommand_exits_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main([]) == 252
        assert "<subcommand>" in capsys.readouterr().err

    def test_server_error_surfaces_as_254(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = _RaisingClient(client_error("NoSuchBucket", 404, "ListObjectsV2"))
        ctx = Context(client_factory=lambda _args: client)  # pyright: ignore[reportArgumentType]
        rc = cli.main(["ls", "s3://bucket/p/"], ctx=ctx)
        err = capsys.readouterr().err
        assert rc == 254
        assert "boto3-s3:" in err
        assert "NoSuchBucket" in err

    def test_assertion_error_is_not_swallowed_by_the_catch_all(self) -> None:
        # The defensive catch-all must NOT swallow AssertionError (an internal
        # invariant violation / a test double's "unexpected call" guard) into a
        # generic rc - it must propagate loudly. Regression guard for the
        # recorder/factory test-double safety net.
        def boom(_args: Any) -> Any:
            raise AssertionError("client factory must not be called")

        with pytest.raises(AssertionError, match="must not be called"):
            cli.main(["ls", "s3://bucket/p/"], ctx=Context(client_factory=boom))

    @pytest.mark.parametrize(
        ("error", "expected_rc", "cause"),
        [
            (NoCredentialsError(), 253, "credentials"),
            # PartialCredentialsError has no dedicated aws handler -> the general
            # 255, NOT 253 (only NoCredentials / NoRegion are 253).
            (
                PartialCredentialsError(provider="env", cred_var="aws_secret_access_key"),
                255,
                "aws_secret_access_key",
            ),
            (client_error("AccessDenied", 403, "ListObjectsV2"), 254, "AccessDenied"),
            (ParamValidationError(report="Invalid bucket name"), 252, "Invalid bucket name"),
            (RuntimeError("boom"), 255, "boom"),
        ],
    )
    def test_raw_error_escaping_a_command_maps_without_traceback(
        self,
        error: BaseException,
        expected_rc: int,
        cause: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Defense in depth (_exit_code_for_unexpected): a non-Boto3S3Error that
        # escapes command.run untranslated must still map through aws-cli's
        # handler chain (ParamValidation 252, NoCredentials/NoRegion 253,
        # ClientError 254, else 255) as a one-line report that shows its cause,
        # without a traceback (design/overview.md section 3).
        # The client factory raising is the cleanest injection - ls.run calls
        # ctx.client_factory(args) directly, so the raw error escapes run.
        def factory(_args: Any) -> Any:
            raise error

        rc = cli.main(["ls", "s3://bucket/p/"], ctx=Context(client_factory=factory))
        err = capsys.readouterr().err
        assert rc == expected_rc
        assert err.startswith("boto3-s3: [ERROR]: ")
        assert cause in err
        assert "Traceback" not in err

    def test_broken_pipe_from_a_command_is_reported_like_any_write_failure(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The `ls | head` shape: ls has a line to print and the reader is gone,
        # so ``uni_write(sys.stdout, ...)`` raises. The library does not
        # translate it (``s3_errors`` catches only ClientError / BotoCoreError),
        # so it escapes the command and lands on _run_command's general backstop,
        # reported with its cause at 255. The final flush then fails as well;
        # the run keeps its 255 and reports nothing twice.
        class _DeadPipe:
            encoding = "utf-8"

            def write(self, _text: str) -> int:
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self) -> None:
                raise BrokenPipeError(32, "Broken pipe")

        ctx = Context(client_factory=lambda _args: _OnePageClient())  # pyright: ignore[reportArgumentType]
        monkeypatch.setattr(sys, "stdout", _DeadPipe())
        rc = cli.main(["ls", "s3://bucket/p/"], ctx=ctx)
        assert rc == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: [Errno 32] Broken pipe\n"

    @pytest.mark.parametrize("work_rc", [0, 2])
    def test_a_successful_run_whose_output_cannot_be_flushed_exits_255(
        self, work_rc: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The work is done - cleanly, or with warnings only - but the output
        # asked for is lost (a full disk, a reader that left): a local I/O
        # failure, reported with its cause - not a success, and not Python's
        # 120 from its own flush at exit.
        class _FullStdout:
            def write(self, text: str) -> int:
                return len(text)

            def flush(self) -> None:
                raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(cli, "_main", lambda _argv, _ctx: work_rc)
        monkeypatch.setattr(sys, "stdout", _FullStdout())
        assert cli.main(["ls"]) == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: [Errno 28] No space left on device\n"

    @pytest.mark.parametrize("argv", [["help"], ["cp", "help"]])
    def test_a_help_page_stdout_cannot_take_exits_255(
        self, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse's print_help swallows the write's OSError from Python 3.11,
        # and a page larger than the buffer goes straight to the descriptor,
        # so the page would be lost at rc 0; written by the CLI, the error is
        # reported once, at 255. Nothing is left to flush afterwards: the
        # write went straight to the descriptor.
        class _FullStdout:
            def write(self, text: str) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")

            def flush(self) -> None:
                pass

        monkeypatch.setattr(sys, "stdout", _FullStdout())
        assert cli.main(argv) == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: [Errno 28] No space left on device\n"

    def test_a_run_without_a_stdout_keeps_its_code(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `1>&-`: Python starts with no stdout, nothing was written to one, and
        # the code is the work's.
        monkeypatch.setattr(cli, "_main", lambda _argv, _ctx: 0)
        monkeypatch.setattr(sys, "stdout", None)
        assert cli.main(["ls"]) == 0
        assert capsys.readouterr().err == ""

    def test_keyboard_interrupt_exits_130_with_a_bare_newline(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's InterruptExceptionHandler: Ctrl-C is a bare newline on stdout
        # and rc 130 (128+SIGINT), never a traceback (measured on the pinned aws-cli).
        # KeyboardInterrupt is a BaseException, so it passes _run_command's
        # handlers and reaches main's backstop.
        class _InterruptClient:
            def get_paginator(self, name: str) -> Any:
                class _Paginator:
                    def paginate(self, **kwargs: Any) -> Any:
                        raise KeyboardInterrupt

                return _Paginator()

        ctx = Context(client_factory=lambda _args: _InterruptClient())  # pyright: ignore[reportArgumentType]
        rc = cli.main(["ls", "s3://bucket/p/"], ctx=ctx)
        out, err = capsys.readouterr()
        assert rc == 130
        assert out == "\n"
        assert err == ""

    def test_keyboard_interrupt_before_the_pipeline_stays_130(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        # The cancelled-run conversion (transferargs.finish_transfer, rc 1)
        # covers only the pipeline span of the transfer family; a Ctrl-C in
        # client construction is still the dispatcher backstop's 130 with the
        # bare newline, like aws's InterruptExceptionHandler.
        (tmp_path / "a.txt").write_bytes(b"x")

        def factory(_args: Any) -> Any:
            raise KeyboardInterrupt

        ctx = Context(client_factory=factory)  # pyright: ignore[reportArgumentType]
        rc = cli.main(["cp", str(tmp_path / "a.txt"), "s3://bucket/k"], ctx=ctx)
        out, err = capsys.readouterr()
        assert rc == 130
        assert out == "\n"
        assert err == ""


class TestClientCreationExitCodes:
    """Client construction (build_client) runs the real boto3 session, which
    raises raw botocore errors. They must reach the exit-code mapping instead of
    escaping main() as an uncaught traceback (the binding charter, overview.md
    section 3). These exercise the real build_client - no injected Context."""

    def test_unknown_profile_exits_255_without_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ProfileNotFound is a BotoCoreError, not credentials/region -> aws's
        # GeneralExceptionHandler (255). It must not crash with a traceback.
        rc = cli.main(["ls", "s3://bucket/p/", "--profile", "boto3_s3_no_such_profile_xyz"])
        err = capsys.readouterr().err
        assert rc == 255
        assert "boto3-s3:" in err
        assert "Traceback" not in err

    def test_schemeless_endpoint_url_exits_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["ls", "s3://bucket/p/", "--endpoint-url", "example.com"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "example.com" in err
        assert "Traceback" not in err


class TestGlobalResolutionFailures:
    """``--query`` and ``--endpoint-url`` report like aws's handler chain even
    when they raise something other than a shaped usage error.

    aws resolves both inside that chain, so a raw exception from one of them
    is one `[ERROR]` line and one of aws's codes - never a traceback with rc 1,
    which the exit-code charter forbids (design/overview.md section 3). Both
    inputs below settle before any request is sent.
    """

    def test_an_endpoint_url_urlsplit_rejects_exits_255(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The unbalanced bracket makes urlsplit raise a bare ValueError, which
        # aws reports through its general handler: rc 255 (measured; the same
        # value in AWS_ENDPOINT_URL_S3 already agreed, because there botocore
        # raises it below the command-level backstop).
        rc = cli.main(["ls", "s3://bucket/p/", "--endpoint-url", "http://[::1:9000"])
        err = capsys.readouterr().err
        assert rc == 255
        assert err.startswith("boto3-s3: [ERROR]: ")
        assert "Traceback" not in err

    def test_a_query_too_deep_to_compile_is_the_param_validation_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's `_resolve_query` catches bare `Exception`, so an expression that
        # exhausts the interpreter's stack is a usage error too (measured: rc
        # 252), reported against the option.
        query = "!" * 2000
        rc = cli.main(["ls", "s3://bucket/p/", "--query", query])
        err = capsys.readouterr().err
        assert rc == 252
        assert "--query" in err
        assert "Traceback" not in err


class TestUnresolvedConfigReports:
    """Credentials or a region that cannot be resolved stop the run at rc 253.

    aws's two dedicated handlers are the only rc-253 failures it reaches on
    this surface; every other botocore failure is its general rc 255.
    """

    def test_no_credentials_is_253(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The whole real path: botocore raises inside the listing and the
        # library translates it, keeping the botocore error as __cause__.
        ctx = Context(client_factory=lambda _args: _RaisingClient(NoCredentialsError()))  # pyright: ignore[reportArgumentType]
        assert cli.main(["ls", "s3://bucket/p/"], ctx=ctx) == 253
        assert "credentials" in capsys.readouterr().err

    def test_no_region_is_253(self, capsys: pytest.CaptureFixture[str]) -> None:
        # What raises it in production is a region-less s3control build (mv's
        # path resolution, pinned in test_clientfactory.py).
        ctx = Context(client_factory=lambda _args: _RaisingClient(NoRegionError()))  # pyright: ignore[reportArgumentType]
        assert cli.main(["ls", "s3://bucket/p/"], ctx=ctx) == 253
        assert "region" in capsys.readouterr().err

    def test_partial_credentials_is_255(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws has no handler for it -> its general one: rc 255 (measured with
        # AWS_ACCESS_KEY_ID alone), naming the missing variable.
        ctx = Context(
            client_factory=lambda _args: _RaisingClient(  # pyright: ignore[reportArgumentType]
                PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY")
            )
        )
        assert cli.main(["ls", "s3://bucket/p/"], ctx=ctx) == 255
        assert "AWS_SECRET_ACCESS_KEY" in capsys.readouterr().err


class TestParseErrorReports:
    """Parse failures are usage errors (rc 252) that name what was rejected.

    The wording is argparse's own, not aws's; what is pinned is the code, the
    rejected token, and which token the classification blames.
    """

    def test_invalid_subcommand_is_named(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["foobar"]) == 252
        assert "invalid choice: 'foobar'" in capsys.readouterr().err

    def test_an_option_form_subcommand_behind_dash_dash_is_blamed_by_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Behind `--` an option-form token is data, so it names the subcommand
        # and is rejected as one (aws: `s3 --region us-east-1 -- --bogus`).
        # The report must blame that name, not read it back as an option.
        assert cli.main(["--region", "us-east-1", "--", "--bogus"]) == 252
        assert "invalid choice: '--bogus'" in capsys.readouterr().err
        # The marker itself is just as much a name once it is behind one.
        assert cli.main(["--", "--", "--bogus"]) == 252
        assert "invalid choice: '--bogus'" in capsys.readouterr().err

    def test_missing_global_value_is_named(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["ls", "--region"]) == 252
        assert "argument --region: expected one argument" in capsys.readouterr().err

    def test_a_protected_profile_token_is_an_unknown_option(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Behind `--` the token is data, so the dispatch reports it as an
        # unknown option (measured on aws).
        assert cli.main(["--", "--profile"]) == 252
        err = capsys.readouterr().err
        assert "Unknown options" in err
        assert "--profile" in err

    @pytest.mark.parametrize("option", ["--storage-class", "--acl"])
    def test_an_unmarked_option_still_rejects_a_dash_led_value(
        self, option: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The contrast to TestCountedOptionTokens below: only the marked
        # options swallow a dash-led token. aws declares every other option
        # without a count, so the token stays an option there and the value is
        # missing (measured per option).
        assert cli.main(["cp", option, "--exclude", "x", "s3://a/", "s3://b/"]) == 252
        assert f"argument {option}: expected one argument" in capsys.readouterr().err


class TestCountedOptionTokens:
    """aws's count-declared options take their tokens however those look.

    ``--exclude`` / ``--include`` are declared with one token and ``mb``'s
    ``--tags`` with two, and the Python aws ships on then hands them dash-led
    tokens as readily as plain ones;
    ``_ParamValidationArgumentParser._match_argument`` reproduces that count
    from the ``aws_nargs`` marker. A swallowed token stops being a positional,
    so what surfaces is whatever the shifted positionals produce - each
    outcome is the pinned aws-cli's own.

    The hook settles the count only; what class a token belongs to stays
    argparse's decision, and up to Python 3.11 that pass rejects a value
    ambiguously abbreviating one of the command's own options (``--exclude
    --ss``) before consumption can happen - the residual design/cli.md
    section 2 records. Nothing below depends on it.
    """

    @pytest.mark.parametrize("option", ["--exclude", "--excl"])
    def test_the_token_is_taken_as_the_pattern_abbreviations_included(
        self, option: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws: `s3 sync --exclude -h a b` leaves `a b` as the two paths, so
        # the local-to-local pair is what fails, not the option.
        assert cli.main(["sync", option, "-h", "a", "b"], ctx=built_client_ctx()) == 252
        assert "Invalid argument type" in capsys.readouterr().err

    def test_a_following_known_option_is_swallowed_and_shifts_the_positionals(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws: `--include` is the pattern, so cp sees three paths and the
        # extra one is reported as an unknown option.
        assert cli.main(["cp", "--exclude", "--include", "x", "s3://a/", "s3://b/"]) == 252
        err = capsys.readouterr().err
        assert "Unknown options" in err
        assert "s3://b/" in err

    def test_a_negative_number_is_taken_as_the_pattern_too(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse classifies `-1` as a plain token rather than an option, so
        # the hook's count applies to a token it would have taken anyway; aws
        # lands on the same synopsis failure as the dash-led cases (measured).
        assert cli.main(["sync", "--exclude", "-1", "a", "b"], ctx=built_client_ctx()) == 252
        assert "Invalid argument type" in capsys.readouterr().err

    def test_a_following_double_dash_is_not_a_pattern(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--` is argparse's positional marker, not a token: aws reports the
        # missing value here, so the hook must leave that classification be.
        assert cli.main(["sync", "--exclude", "--", "a", "b"]) == 252
        assert "argument --exclude: expected one argument" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["mb", "--tags", "k"],
            ["mb", "--tags", "k", "--", "v", "s3://a"],
        ],
    )
    def test_a_two_token_option_short_of_tokens_reports_the_count(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `mb --tags` is aws's two-token declaration: one token left, or a
        # `--` among them, is short either way (aws rejects each argv here
        # too, measured; the token patterns are TestCountedOptionMarker's).
        assert cli.main(argv) == 252
        assert "argument --tags: expected 2 arguments" in capsys.readouterr().err


class TestCountedOptionMarker:
    """The ``_match_argument`` hook itself, asked about token patterns.

    The end-to-end tests above cannot separate the hook from the interpreter
    on Python 3.12+, where stock argparse consumes the same tokens by itself;
    these ask the hook directly, so a marker that stopped covering its count
    fails here on every version. ``A`` / ``O`` / ``-`` are argparse's classes
    for a plain token, an option-like one and the ``--`` separator.
    """

    @pytest.mark.parametrize("pattern", ["OO", "AO", "OA", "AA", "OOA"])
    def test_two_marked_tokens_are_taken_whatever_their_class(self, pattern: str) -> None:
        parser = cli._ParamValidationArgumentParser(add_help=False)
        action = parser.add_argument("--tags", action=mb._AppendTagsAction, nargs=2)
        assert parser._match_argument(action, pattern) == 2

    @pytest.mark.parametrize("pattern", ["O", "A", "-AA", "", "O-A"])
    def test_two_marked_tokens_are_not_invented(self, pattern: str) -> None:
        # Short, or `--` inside the pair: argparse's own rejection stands.
        parser = cli._ParamValidationArgumentParser(add_help=False)
        action = parser.add_argument("--tags", action=mb._AppendTagsAction, nargs=2)
        with pytest.raises(argparse.ArgumentError):
            parser._match_argument(action, pattern)

    @pytest.mark.parametrize("pattern", ["O", "A", "OA", "AO"])
    def test_one_marked_token_is_taken_whatever_its_class(self, pattern: str) -> None:
        parser = cli._ParamValidationArgumentParser(add_help=False)
        action = parser.add_argument("--exclude", action=filters.AppendFilterAction, dest="filters")
        assert parser._match_argument(action, pattern) == 1

    @pytest.mark.parametrize("pattern", ["", "-A", "-"])
    def test_one_marked_token_is_not_invented(self, pattern: str) -> None:
        parser = cli._ParamValidationArgumentParser(add_help=False)
        action = parser.add_argument("--exclude", action=filters.AppendFilterAction, dest="filters")
        with pytest.raises(argparse.ArgumentError):
            parser._match_argument(action, pattern)

    @pytest.mark.parametrize("pattern", ["O", "OA"])
    def test_an_unmarked_option_keeps_argparse_s_own_matching(self, pattern: str) -> None:
        # The mutant guard for the marker check: without it every option would
        # start swallowing option-like tokens, which aws does not do.
        parser = cli._ParamValidationArgumentParser(add_help=False)
        action = parser.add_argument("--storage-class")
        with pytest.raises(argparse.ArgumentError):
            parser._match_argument(action, pattern)


class TestNegativeNumberClassification:
    """A dash-led token that opens like a negative number is a plain value.

    Python 3.14 loosened argparse's negative-number matcher from a whole-token
    number to a prefix (`-1x` and `-.5x` now classify as plain tokens, not as
    options), and aws's official distribution runs on 3.14, so its parse
    consumes such a token as the following option's value. The parser pins
    3.14's pattern on every supported Python; without the pin our 3.10 floor
    would report the value as missing. Every ``main`` outcome below is the
    pinned aws-cli's own (the wording of a usage error is argparse's); the two
    structural tests at the end assert no output, pinning where the matcher is
    installed and the gate that would take it out of play.
    """

    @pytest.mark.parametrize("value", ["-1x", "-.5x", "-1x2"])
    def test_the_token_becomes_the_value_and_the_choice_is_rejected(
        self, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["cp", "--storage-class", value, "s3://a/k", "s3://b/k"]) == 252
        assert f"invalid choice: '{value}'" in capsys.readouterr().err

    def test_an_int_option_takes_it_and_fails_the_conversion(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws lets the ValueError out of its own int conversion, so the run
        # ends at the general-error code, naming the value.
        assert cli.main(["ls", "--page-size", "-1x", "s3://a/"]) == 255
        assert "-1x" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["-x", "-.x", "-.", "--1"])
    def test_a_token_that_only_looks_dash_led_stays_an_option(
        self, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The boundary the loosened pattern draws: a digit must follow the
        # dash (one optional dot aside), so these keep reporting the missing
        # value on aws too.
        assert cli.main(["cp", "--storage-class", value, "s3://a/k", "s3://b/k"]) == 252
        assert "argument --storage-class: expected one argument" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["-1", "-1.5", "-.5", "-"])
    def test_a_whole_number_shaped_token_is_a_value_on_every_python(
        self, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["cp", "--storage-class", value, "s3://a/k", "s3://b/k"]) == 252
        assert f"argument --storage-class: invalid choice: '{value}'" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["-1", "-1x"])
    def test_the_subcommand_scan_classifies_it_the_same_way(
        self, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `_find_command_token` mirrors the parser's classification, so such a
        # token names the (invalid) subcommand rather than being walked past
        # as an option - which is what `-x` does in the test below.
        assert cli.main([value, "s3://a/"]) == 252
        assert f"invalid choice: '{value}'" in capsys.readouterr().err

    def test_a_dash_led_non_number_is_skipped_by_the_scan(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The contrast that makes the test above about the classification:
        # `-x` is option-like, so the scan walks past it and the *next* token
        # is what names the (invalid) subcommand.
        assert cli.main(["-x", "s3://a/"]) == 252
        assert "invalid choice: 's3://a/'" in capsys.readouterr().err

    def test_it_reaches_a_positional_too(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Taken as the local source path, which does not exist.
        assert cli.main(["cp", "-1x", "s3://b/"], ctx=built_client_ctx()) == 255
        assert "-1x" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["-1", "-1x", "-x"])
    def test_a_marked_filter_option_is_unaffected(
        self, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--exclude` consumes A and O alike through the `aws_nargs` hook, so
        # the classification never decides anything for it: all three shapes
        # leave `a b` as the paths and fail the same synopsis check.
        assert cli.main(["sync", "--exclude", value, "a", "b"], ctx=built_client_ctx()) == 252
        assert "Invalid argument type" in capsys.readouterr().err

    def test_the_globals_parser_takes_it_as_a_global_value(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # And the top-level globals pass, whose own choice-bearing option
        # rejects the consumed token the same way a subcommand's does.
        assert cli.main(["--output", "-1x", "ls", "s3://a/"], ctx=unused_ctx()) == 252
        assert "argument --output: invalid choice: '-1x'" in capsys.readouterr().err

    def test_the_pattern_is_the_pinned_one(self) -> None:
        assert cli._NEGATIVE_NUMBER_RE.match("-1x")
        assert not cli._NEGATIVE_NUMBER_RE.match("-x")

    def test_no_option_string_anywhere_disables_the_classification(self) -> None:
        # argparse only consults the matcher while no registered option string
        # looks like a negative number itself. That gate is fed by each
        # container's own matcher - the argument groups' is the host Python's,
        # not the pin - so it is only moot as long as this holds. A digit-led
        # option would need the gate pinned too.
        for parser in _every_built_parser():
            for action in parser._actions:
                for option_string in action.option_strings:
                    assert not cli._NEGATIVE_NUMBER_RE.match(option_string)
            assert not parser._has_negative_number_optionals


class TestOptionValueResolution:
    """How a single option value is resolved before the run: the paramfile
    expansion (a bad reference is 252), the integer coercion (255), and the
    ``--metadata`` map (252). Measured against the pinned aws-cli, except the
    paramfile uses aws mishandles, which are refused at 252 here."""

    _MISSING = "/nonexistent_src_for_option_value_test.txt"

    def test_expected_size_readable_fileb_is_a_param_validation_252(self, tmp_path: Path) -> None:
        # --expected-size is a string-model option in aws (no cli_type_name),
        # NOT part of the integer-family expansion: a readable fileb:// is
        # refused like on any string option (252; aws's botocore type check
        # ends the same), where the integer helper would run int(b'5') and
        # proceed.
        blob = tmp_path / "size.bin"
        blob.write_bytes(b"5")
        rc = cli.main(["cp", "-", "s3://b/k", "--expected-size", f"fileb://{blob}"])
        assert rc == 252

    def test_page_size_paramfile_reference_is_expanded(self) -> None:
        # aws paramfile-expands even the string-typed integer options: a bad
        # file:// reference on --page-size is its 252, not the int()'s 255.
        assert cli.main(["ls", "s3://b", "--page-size", "file:///no/x"]) == 252

    def test_metadata_fileb_shorthand_value_is_a_usage_error(self, tmp_path: Path) -> None:
        # a@=fileb://... loads real bytes; aws rejects the non-string map value
        # at parse time (252) - it must not reach the transfer (rc 1). The file
        # exists, so the 252 cannot be the missing-paramfile one.
        blob = tmp_path / "v.bin"
        blob.write_bytes(b"\x00\x01")
        rc = cli.main(["cp", self._MISSING, "s3://b/k", "--metadata", f"a@=fileb://{blob}"])
        assert rc == 252

    def test_metadata_shorthand_paramfile_load_failure_is_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Like the whole-value case below: a file named on the command line
        # that cannot be read is 252 wherever it is named, for either prefix.
        # (aws's shorthand parser loads it outside the handler that names the
        # option, so there it reaches the general handler at 255.)
        for ref in ("file:///no/x", "fileb:///no/x"):
            assert cli.main(["cp", self._MISSING, "s3://b/k", "--metadata", f"a@={ref}"]) == 252
            assert ref in capsys.readouterr().err

    def test_whole_value_metadata_fileb_missing_is_252(self) -> None:
        # A whole-value --metadata fileb:// is refused unread (252), so a
        # missing file is the same 252.
        rc = cli.main(["cp", self._MISSING, "s3://b/k", "--metadata", "fileb:///no/x"])
        assert rc == 252

    def test_whole_value_metadata_fileb_existing_is_252(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws loads the bytes, then crashes indexing them in its map shorthand
        # parser (255) - an aws bug not copied: refused as a usage error.
        blob = tmp_path / "m.bin"
        blob.write_bytes(b"k=v")
        rc = cli.main(["cp", self._MISSING, "s3://b/k", "--metadata", f"fileb://{blob}"])
        assert rc == 252
        assert "a fileb:// reference is not supported" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["ls", "rm", "mb", "rb", "presign", "website"])
    def test_positional_fileb_is_refused_on_every_command(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws breaks a different way on each command (252, 255, or rm deleting
        # the decoded path) - an aws bug not copied: one 252 everywhere, with
        # nothing read or sent.
        ref = tmp_path / "path.bin"
        ref.write_bytes(b"s3://bucket/key")
        rc = cli.main([command, f"fileb://{ref}"], ctx=unused_ctx())
        assert rc == 252
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "fileb://" in captured.err
        assert "cannot" in captured.err

    def test_lone_grants_paramfile_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws loads it and parses the text as grants one character at a time
        # (rc 1 in flight) - an aws bug not copied.
        ref = tmp_path / "g.txt"
        ref.write_text("read=uri=http://acs.amazonaws.com/groups/global/AllUsers")
        rc = cli.main(
            ["cp", self._MISSING, "s3://b/k", "--grants", f"file://{ref}"], ctx=unused_ctx()
        )
        assert rc == 252
        assert "Error parsing parameter '--grants'" in capsys.readouterr().err

    def test_free_string_option_fileb_is_a_param_validation_252(self, tmp_path: Path) -> None:
        # A fileb:// on a string-typed option is refused unread (252; aws's
        # botocore type check ends the same), not a transfer.
        blob = tmp_path / "ct.bin"
        blob.write_bytes(b"hi")
        rc = cli.main(
            ["cp", self._MISSING, "s3://b/k", "--content-type", f"fileb://{blob}", "--dryrun"]
        )
        assert rc == 252


class TestRecursiveDestDirCreation:
    """aws pre-creates the s3local dir_op destination during validation
    (its ``_validate_path_args``), so a creation failure is rc 255 - not the
    pipeline's rc 1. sync always did this; cp/mv --recursive share it now."""

    @pytest.fixture()
    def blocked_parent(self, tmp_path: Path) -> Path:
        # A file where the destination's parent directory should be: makedirs
        # fails on every platform (a chmod-denied directory would not hold on
        # Windows or as root), and every OSError category maps to the same 255.
        blocker = tmp_path / "ro"
        blocker.write_bytes(b"")
        return blocker

    def test_cp_uncreatable_recursive_dest_exits_255(self, blocked_parent: Any) -> None:
        rc = cli.main(["cp", "s3://b/pre", str(blocked_parent / "new"), "--recursive"])
        assert rc == 255

    def test_mv_uncreatable_recursive_dest_exits_255(self, blocked_parent: Any) -> None:
        rc = cli.main(["mv", "s3://b/pre", str(blocked_parent / "new"), "--recursive"])
        assert rc == 255


class TestHelpToken:
    """aws's parser turns an exactly-``['help']`` token list into the help
    page (rc 0) at every level - its ``ArgTableArgParser`` special case.
    Measured on the pinned aws-cli: ``s3 help`` and ``s3 ls help`` are 0, and
    ``s3 help foo`` is 252."""

    def test_top_level_help_token(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["help"]) == 0
        assert "usage:" in capsys.readouterr().out.lower()

    def test_help_token_after_globals(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["--debug", "help"]) == 0
        capsys.readouterr()

    @pytest.mark.parametrize("name", sorted(cli._COMMAND_TABLE))
    def test_subcommand_help_token(self, name: str, capsys: pytest.CaptureFixture[str]) -> None:
        # The token is now the only route to a help page, so every entry in the
        # command table is pinned - including the ones whose normal parse would
        # fail on missing positionals (measured: all nine are rc 0 on aws).
        assert cli.main([name, "help"]) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"usage: boto3-s3 {name}")
        # The page is the full renderer's, not the leaf parse parser's reduced
        # view: it carries the recognized-but-ignored globals section too.
        assert out == cli._build_command_parser(name, cli._load_command(name)()).format_help()

    def test_help_token_with_extras_is_not_special(self) -> None:
        assert cli.main(["help", "foo"]) == 252

    def test_help_token_with_trailing_globals(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The top-level pass strips globals on either side, so aws pages here
        # (measured: `s3 help --region us-east-1` and
        # `s3 presign help --region us-east-1 --no-cli-pager` are both 0).
        assert cli.main(["help", "--region", "us-east-1"]) == 0
        assert "usage:" in capsys.readouterr().out.lower()
        assert cli.main(["presign", "help", "--region", "us-east-1", "--no-cli-pager"]) == 0
        assert "usage: boto3-s3 presign" in capsys.readouterr().out


class TestHelpOptionsAreUnknownOptions:
    """``-h`` / ``--help`` are options on neither tool: the ``help`` token is
    the only route to a help page, and both spellings fall through as ordinary
    unrecognized options (252), wherever they stand in the command line."""

    @pytest.mark.parametrize(
        ("argv", "reported"),
        [
            (["--help"], "--help"),
            (["-h"], "-h"),
            (["--", "--help"], "--help"),
            (["ls", "--help"], "--help"),
            (["-h", "ls"], "-h"),
            (["ls", "help", "--help"], "--help"),
            (["sync", "-h", "a", "b"], "-h"),
            (["cp", "a", "b", "--help"], "--help"),
        ],
        ids=[
            "top-level-long",
            "top-level-short",
            "behind-a-leading-marker",
            "after-the-subcommand",
            "before-the-subcommand",
            "after-a-help-shaped-path",
            "between-the-subcommand-and-its-paths",
            "after-satisfied-paths",
        ],
    )
    def test_reported_as_unknown_options(
        self, argv: list[str], reported: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(argv) == 252
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Unknown options" in captured.err
        assert reported in captured.err

    @pytest.mark.parametrize(
        "argv",
        [["help", "--help"], ["-h", "help"]],
        ids=["help-token-then-option", "short-then-help-token"],
    )
    def test_a_help_option_beside_the_help_token_pages_nothing(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The help-token rule needs an exactly-``['help']`` remainder, so an
        # accompanying ``-h`` / ``--help`` turns the token into a subcommand
        # name, which is rejected.
        assert cli.main(argv) == 252
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("argv", [["--version", "--help"], ["--help", "--version"]])
    def test_version_wins_in_either_order(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--version` is a real option here and on aws, and its parse-time
        # action fires before the unknown options are ever collected.
        assert cli.main(argv) == 0
        captured = capsys.readouterr()
        assert captured.out.startswith("boto3-s3-cli/")
        assert captured.err == ""

    def test_help_option_behind_a_leaf_marker_is_positional_data(self) -> None:
        # Measured: `s3 ls -- --help` lists the bucket named `--help` rather
        # than reporting anything - the marker survives to the leaf parse.
        client = _OnePageClient()
        rc = cli.main(["ls", "--", "--help"], ctx=Context(client_factory=lambda _a: client))
        assert rc == 0
        assert [call["Bucket"] for call in client.calls] == ["--help"]

    def test_dash_dash_h_abbreviates_to_human_readable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Measured: `s3 ls --h` runs (aws resolves the abbreviation against
        # `--human-readable`); with `-h` gone nothing shadows it here either.
        client = _OnePageClient(size=2048)
        rc = cli.main(
            ["ls", "s3://bucket/pre/", "--h"], ctx=Context(client_factory=lambda _a: client)
        )
        assert rc == 0
        assert "2.0 KiB" in capsys.readouterr().out


class _RecordingPresignClient:
    """Offline presign stand-in recording (Params, ExpiresIn) per call."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], int]] = []

    def generate_presigned_url(self, method: str, **kwargs: Any) -> str:
        self.calls.append((kwargs["Params"], kwargs["ExpiresIn"]))
        return "https://example.test/presigned"


def _presign_ctx(client: _RecordingPresignClient) -> Context:
    return Context(client_factory=lambda _args: client)  # pyright: ignore[reportArgumentType]


class TestTopLevelGlobalsStripping:
    """The aws-shaped token flow (measured on the pinned aws-cli 2.36.40).

    aws parses and REMOVES the globals over the full argv first, locates the
    subcommand among the survivors (unknown optionals assumed valueless), and
    hands every other token to the leaf parser in original order. The first
    ``--`` protects what follows from the globals pass and the leaf re-honors
    it - except a leading ``--``, which aws's first parse uses up against its
    service token, so the tail is re-read as options downstream.
    """

    def test_global_prefix_wins_over_command_option_prefix(self) -> None:
        # aws resolves `--e` against the globals alone (its first parse runs
        # before any command parser), never reporting the `--endpoint-url` /
        # `--expires-in` ambiguity a merged parser would see. Measured rc 0
        # with the URL built on https://example.com.
        client = _RecordingPresignClient()
        args = ["--region", "us-east-1", "presign", "s3://b/k", "--e", "https://example.com"]
        assert cli.main(args, ctx=_presign_ctx(client)) == 0
        assert client.calls == [({"Bucket": "b", "Key": "k"}, 3600)]

    def test_global_between_option_and_value(self) -> None:
        # Measured: `s3 presign --expires-in --region us-east-1 120 s3://b/k`
        # is rc 0 with Expires=120 - the globals pass lifts the pair out from
        # between `--expires-in` and its value.
        client = _RecordingPresignClient()
        args = ["presign", "--expires-in", "--region", "us-east-1", "120", "s3://b/k"]
        assert cli.main(args, ctx=_presign_ctx(client)) == 0
        assert client.calls == [({"Bucket": "b", "Key": "k"}, 120)]

    def test_command_option_before_subcommand(self) -> None:
        # Measured: `s3 --expires-in=120 presign s3://b/k` is rc 0 - the
        # option-form token flows past the subcommand scan to the leaf.
        client = _RecordingPresignClient()
        assert cli.main(["--expires-in=120", "presign", "s3://b/k"], ctx=_presign_ctx(client)) == 0
        assert client.calls == [({"Bucket": "b", "Key": "k"}, 120)]

    def test_double_dash_before_subcommand(self) -> None:
        # Measured rc 0: the leading `--` is consumed by aws's first parse
        # (its service token always precedes it), so the subcommand still
        # resolves.
        client = _RecordingPresignClient()
        args = ["--region", "us-east-1", "--", "presign", "s3://b/k"]
        assert cli.main(args, ctx=_presign_ctx(client)) == 0
        assert client.calls == [({"Bucket": "b", "Key": "k"}, 3600)]

    def test_leading_double_dash_help_token_pages(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Measured: `s3 -- help` pages (rc 0) - the dropped marker leaves an
        # exactly-['help'] remainder.
        assert cli.main(["--", "help"]) == 0
        assert "usage:" in capsys.readouterr().out.lower()

    def test_dropped_leading_double_dash_rereads_options(self) -> None:
        # Measured: `s3 -- presign --expires-in 120 s3://b/k` is rc 0 - once
        # the leading marker is gone the leaf parses the option normally.
        client = _RecordingPresignClient()
        args = ["--", "presign", "--expires-in", "120", "s3://b/k"]
        assert cli.main(args, ctx=_presign_ctx(client)) == 0
        assert client.calls == [({"Bucket": "b", "Key": "k"}, 120)]

    def test_global_after_dropped_double_dash_is_unknown(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Measured: `s3 -- presign --region us-east-2 s3://b/k` is rc 252 -
        # the leaf parser does not know the globals, and the value lands in
        # the path slot.
        assert cli.main(["--", "presign", "--region", "us-east-2", "s3://b/k"]) == 252
        assert "--region" in capsys.readouterr().err

    def test_interior_double_dash_protects_the_tail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Measured: `s3 presign -- s3://b/k --expires-in 120` is rc 252 - with
        # a token ahead of it the marker survives to the leaf and the tail
        # stays positional.
        assert cli.main(["presign", "--", "s3://b/k", "--expires-in", "120"]) == 252
        assert "--expires-in" in capsys.readouterr().err

    def test_kept_double_dash_after_globals(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Measured: `s3 --region us-east-2 -- help` -> invalid choice 'help'
        # (the marker is kept when consumed globals precede it, so the help
        # token is a protected positional, not the help-token rule).
        assert cli.main(["--region", "us-east-2", "--", "help"]) == 252
        assert "invalid choice: 'help'" in capsys.readouterr().err


class TestClosedStandardStreams:
    """``--version`` when stdout is closed.

    A descriptor closed before the interpreter starts leaves ``sys.stdout``
    ``None``. Like argparse's own version action, the line then goes to
    stderr and the run exits 0.
    """

    @pytest.mark.parametrize("argv", [["--version"], ["ls", "--version"], ["--version", "bogus"]])
    def test_the_version_line_falls_back_to_stderr(
        self, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "stdout", None)
        assert cli.main(argv) == 0
        assert capsys.readouterr().err.startswith("boto3-s3-cli/")


class TestTheEntryPointBackstop:
    """Nothing the dispatcher itself raises leaves as a traceback.

    aws's entry point hands whatever escapes its driver to its handler chain;
    a command's failures are settled in `_run_command`, and `main` applies
    the same mapping to the dispatcher's own steps.
    """

    def test_an_unclaimed_exception_is_the_general_report(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def explode(*args: object, **kwargs: object) -> int:
            raise RuntimeError("boom")

        monkeypatch.setattr(cli, "_dispatch", explode)
        assert cli.main(["ls", "s3://b/"]) == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: boom\n"

    def test_an_assertion_stays_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*args: object, **kwargs: object) -> int:
            raise AssertionError("an invariant")

        monkeypatch.setattr(cli, "_dispatch", explode)
        with pytest.raises(AssertionError, match="an invariant"):
            cli.main(["ls", "s3://b/"])
