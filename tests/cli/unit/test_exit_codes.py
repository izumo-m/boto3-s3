"""Unit tests for the aws-cli exit-code mapping (docs/cli.md section 6).

`exit_code_for` is exercised directly for each branch, and `main` is
exercised end-to-end for the paths that do not go through a library error
(usage errors, unknown options) plus the ClientError path through a fake
client, so the parse -> dispatch -> error -> exit-code wiring is covered. The
catch-all backstop (`_exit_code_for_unexpected`: a non-Boto3S3Error escaping
a command -> 252/253/254/255) and the `BrokenPipeError` -> 0 handler are
covered end-to-end through `main` too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
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
from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context


def _client_error(code: str, status: int) -> ClientError:
    response: Any = {
        "Error": {"Code": code, "Message": "stub"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, "ListObjectsV2")


def _with_cause(exc: Boto3S3Error, cause: BaseException) -> Boto3S3Error:
    exc.__cause__ = cause
    return exc


class TestExitCodeFor:
    def test_server_side_error_maps_to_254(self) -> None:
        exc = _with_cause(NotFoundError("no such bucket"), _client_error("NoSuchBucket", 404))
        assert cli.exit_code_for(exc) == 254

    def test_client_error_cause_wins_over_validation_category(self) -> None:
        # A server-rejected call (HTTP 400) that the library files under
        # ValidationError still exits 254: aws-cli maps every error that
        # reached the server to CLIENT_ERROR_RC.
        exc = _with_cause(ValidationError("bad request"), _client_error("InvalidRequest", 400))
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
        exc = _with_cause(InvalidConfigError("rejected"), _client_error("InvalidRequest", 400))
        assert cli.exit_code_for(exc) == 254


class _RaisingClient:
    """Fake S3 client whose paginator raises a ClientError on iteration."""

    def __init__(self, error: ClientError) -> None:
        self._error = error

    def get_paginator(self, name: str) -> Any:
        error = self._error

        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                raise error

        return _Paginator()


class _BrokenPipeClient:
    """Fake whose ListObjectsV2 paginator raises BrokenPipeError on iteration.

    Models a downstream reader closing the pipe (``... | head``): the library
    does not translate a BrokenPipeError (``s3_errors`` catches only
    ClientError / BotoCoreError), so it escapes the command and reaches
    ``_dispatch``'s dedicated ``except BrokenPipeError`` handler.
    """

    def get_paginator(self, name: str) -> Any:
        class _Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                raise BrokenPipeError

        return _Paginator()


class TestMainExitCodes:
    @pytest.mark.parametrize(
        ("argv", "required_name"),
        [
            (["cp"], "paths"),
            (["mv"], "paths"),
            (["rm"], "paths"),
            (["mb"], "path"),
            (["rb"], "path"),
            (["presign"], "path"),
            (["website"], "paths"),
        ],
    )
    def test_missing_required_path_uses_aws_argument_name(
        self,
        argv: list[str],
        required_name: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert cli.main(argv) == 252
        first_line = capsys.readouterr().err.splitlines()[0]
        assert first_line.endswith(f"the following arguments are required: {required_name}")

    def test_unknown_option_exits_252_with_aws_wording(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["ls", "--extra-argument-foo", "s3://bucket/p/"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "An error occurred (ParamValidation):" in err
        assert "Unknown options" in err
        assert "--extra-argument-foo" in err

    def test_unknown_options_joined_with_comma_no_space(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws s3 joins multiple unknown options with "," and NO space (the
        # customizations command layer; verified against the pinned aws-cli), not
        # ", ". Both option positions share this wording.
        assert cli.main(["cp", "a.txt", "s3://b/", "--foo", "--bar"]) == 252
        assert "Unknown options: --foo,--bar" in capsys.readouterr().err
        assert cli.main(["--foo", "--bar", "cp", "a.txt", "s3://b/"]) == 252
        assert "Unknown options: --foo,--bar" in capsys.readouterr().err

    def test_extra_positional_exits_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["ls", "s3://bucket/p/", "stray"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "Unknown options" in err
        assert "stray" in err

    def test_invalid_choice_exits_252_with_param_validation_wrapper(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["sync", ".", "s3://bucket/p/", "--checksum-algorithm", "INVALID"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "An error occurred (ParamValidation):" in err
        assert "argument --checksum-algorithm: Found invalid choice 'INVALID'" in err

    def test_missing_subcommand_exits_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main([]) == 252
        assert "command" in capsys.readouterr().err

    def test_server_error_surfaces_as_254(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = _RaisingClient(_client_error("NoSuchBucket", 404))
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
        ("error", "expected_rc"),
        [
            (NoCredentialsError(), 253),
            (NoRegionError(), 253),
            # PartialCredentialsError has no dedicated aws handler -> the general
            # 255, NOT 253 (only NoCredentials / NoRegion are 253).
            (PartialCredentialsError(provider="env", cred_var="aws_secret_access_key"), 255),
            (_client_error("AccessDenied", 403), 254),
            (ParamValidationError(report="Invalid bucket name"), 252),
            (RuntimeError("boom"), 255),
        ],
    )
    def test_raw_error_escaping_a_command_maps_without_traceback(
        self, error: BaseException, expected_rc: int, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Defense in depth (_exit_code_for_unexpected): a non-Boto3S3Error that
        # escapes command.run untranslated must still map through aws-cli's
        # handler chain (ParamValidation 252, NoCredentials/NoRegion 253,
        # ClientError 254, else 255) without a traceback (the exit-code charter,
        # docs/overview.md section 3).
        # The client factory raising is the cleanest injection - ls.run calls
        # ctx.client_factory(args) directly, so the raw error escapes run.
        def factory(_args: Any) -> Any:
            raise error

        rc = cli.main(["ls", "s3://bucket/p/"], ctx=Context(client_factory=factory))
        err = capsys.readouterr().err
        assert rc == expected_rc
        assert "boto3-s3:" in err
        if expected_rc == 252:
            assert "An error occurred (ParamValidation):" in err
        assert "Traceback" not in err

    def test_broken_pipe_from_a_command_exits_0(self) -> None:
        # aws-cli exits 0 when a downstream reader closes the pipe
        # (docs/cli.md section 6): a BrokenPipeError escaping the command maps to
        # 0, not a fatal rc. The library does not translate it, so it reaches
        # _dispatch's dedicated handler.
        ctx = Context(client_factory=lambda _args: _BrokenPipeClient())  # pyright: ignore[reportArgumentType]
        assert cli.main(["ls", "s3://bucket/p/"], ctx=ctx) == 0

    def test_keyboard_interrupt_exits_130_with_a_bare_newline(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's InterruptExceptionHandler: Ctrl-C is a bare newline on stdout
        # and rc 130 (128+SIGINT), never a traceback (measured on the pinned aws-cli).
        # KeyboardInterrupt is a BaseException, so it passes _dispatch's
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
        assert "scheme is missing" in err
        assert "Traceback" not in err


class TestValidationOrder:
    """The order in which cp / mv / sync run their pre-client validations is
    exit-code-significant: aws-cli ``_validate_path_args`` checks the missing
    local source (a bare RuntimeError -> 255) right after the checksum/path
    pairing and *before* SSE-C / the S3 Express directory-bucket check (252).
    When more than one would fail, that order decides the exit code. aws v2
    returns 255 for all of these (the missing source wins).

    These fire before the client factory, so no Context / network is needed.
    """

    _MISSING = "/nonexistent_src_for_validation_order_test.txt"
    _MISSING_DIR = "/nonexistent_dir_for_validation_order_test"

    def test_cp_missing_source_beats_sse_c_copy_source(self) -> None:
        # --sse-c-copy-source is s3s3-only -> a 252 route error on locals3, but
        # the missing local source must surface first as 255.
        rc = cli.main(
            [
                "cp",
                self._MISSING,
                "s3://bucket/key",
                "--sse-c-copy-source",
                "AES256",
                "--sse-c-copy-source-key",
                "foo",
            ]
        )
        assert rc == 255

    def test_cp_missing_source_beats_unpaired_sse_c(self) -> None:
        rc = cli.main(["cp", self._MISSING, "s3://bucket/key", "--sse-c", "AES256"])
        assert rc == 255

    def test_mv_missing_source_beats_unpaired_sse_c(self) -> None:
        rc = cli.main(["mv", self._MISSING, "s3://bucket/key", "--sse-c", "AES256"])
        assert rc == 255

    def test_sync_missing_source_beats_s3express_dest(self) -> None:
        rc = cli.main(["sync", self._MISSING_DIR, "s3://bucket--x-s3/pre"])
        assert rc == 255

    def test_sync_missing_source_beats_unpaired_sse_c(self) -> None:
        rc = cli.main(["sync", self._MISSING_DIR, "s3://bucket/pre", "--sse-c", "AES256"])
        assert rc == 255

    def test_checksum_path_type_still_beats_missing_source(self) -> None:
        # --checksum-mode is s3local-only -> a 252 path-type error. It is checked
        # BEFORE the missing source in both aws-cli and our order, so it stays 252
        # even with a missing source (the boundary on the other side).
        rc = cli.main(["cp", self._MISSING, "s3://bucket/key", "--checksum-mode", "ENABLED"])
        assert rc == 252


class TestPreParseErrorAttribution:
    """A global that fails to parse - or a parse-time ``--version`` - settles
    the run in the pre-pass, like aws's ``MainArgParser``: it beats the
    invalid-subcommand error and a ``-h`` anywhere in argv (measured against
    the pinned aws-cli: ``s3 bogus --output bad`` blames ``--output``,
    ``s3 ls -h --output bad`` errors instead of helping, ``s3 bogus
    --version`` prints the version at rc 0)."""

    def test_bad_global_choice_beats_the_invalid_subcommand(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["nosuchcmd", "--output", "bad"]) == 252
        err = capsys.readouterr().err
        assert "argument --output: Found invalid choice 'bad'" in err
        assert "nosuchcmd" not in err

    def test_missing_global_value_beats_the_invalid_subcommand(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["nosuchcmd", "--profile"]) == 252
        err = capsys.readouterr().err
        assert "argument --profile: expected one argument" in err
        assert "nosuchcmd" not in err

    def test_version_beats_the_invalid_subcommand(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["nosuchcmd", "--version"]) == 0
        assert capsys.readouterr().out.startswith("boto3-s3-cli/")

    def test_parse_error_beats_help_in_either_position(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws's main parse precedes its help handling entirely, so -h cannot
        # rescue a bad global even when it comes first.
        assert cli.main(["ls", "-h", "--output", "bad"]) == 252
        assert "argument --output: Found invalid choice 'bad'" in capsys.readouterr().err
        assert cli.main(["--help", "--output", "bad"]) == 252
        assert "argument --output: Found invalid choice 'bad'" in capsys.readouterr().err

    def test_replayed_error_renders_the_stage1_usage(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The pre-pass replay must be byte-identical to the stage-1 report it
        # supersedes: same [ERROR] line, same usage block (the globals parser
        # borrows stage 1's usage, <command> token and all).
        assert cli.main(["ls", "--output", "bad"]) == 252
        err = capsys.readouterr().err
        assert "argument --output: Found invalid choice 'bad'" in err
        assert "usage: boto3-s3 " in err
        assert "<command>" in err

    def test_bare_invalid_subcommand_is_still_blamed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # With no global parse error the attribution stays on the subcommand -
        # including beside unknown options and a trailing -h (all measured).
        assert cli.main(["nosuchcmd"]) == 252
        assert "Found invalid choice 'nosuchcmd'" in capsys.readouterr().err
        assert cli.main(["nosuchcmd", "--unknown-opt"]) == 252
        assert "Found invalid choice 'nosuchcmd'" in capsys.readouterr().err
        assert cli.main(["nosuchcmd", "-h"]) == 252
        assert "Found invalid choice 'nosuchcmd'" in capsys.readouterr().err


class TestParseToValidationOrder:
    """The head order aws applies before its path validations (measured
    against the pinned aws-cli; docs/cli.md section 5.7, table in
    section 6): the ``--query`` compile (252) -> the ``--endpoint-url`` scheme
    check (252) -> the ``--cli-read-timeout`` / ``--cli-connect-timeout``
    coercions (255, read first; resolved in the dispatch pre-pass, so they
    also beat invalid choices, unknown options, and missing arguments) ->
    the direct-option paramfiles and the two integer coercions,
    interleaved per aws's option-by-option registration order (a bad paramfile
    is 252, a bad ``int()`` 255, and the earlier option wins) -> the
    ``--metadata`` resolution (252, the one value family the coercions beat) ->
    the session profile resolution (255) -> the path/usage checks. Each test
    pins one combined-error pair whose exit code that order decides."""

    _BOGUS = "boto3_s3_no_such_profile_for_order_tests"
    _MISSING = "/nonexistent_src_for_head_order_test.txt"

    def test_bad_timeout_beats_unknown_options(self) -> None:
        assert cli.main(["ls", "--funky", "--cli-read-timeout", "abc"]) == 255

    def test_bad_timeout_beats_missing_arguments(self) -> None:
        assert cli.main(["cp", "--cli-read-timeout", "abc"]) == 255

    def test_bad_timeout_beats_invalid_choice(self) -> None:
        assert cli.main(["nosuchcmd", "--cli-connect-timeout", "abc"]) == 255

    def test_bad_timeout_beats_top_level_unknown_options(self) -> None:
        assert cli.main(["--funky", "ls", "--cli-read-timeout", "abc"]) == 255

    def test_bad_query_beats_a_bad_timeout(self) -> None:
        rc = cli.main(["ls", "s3://b", "--query", "bad((", "--cli-read-timeout", "abc"])
        assert rc == 252

    def test_bad_endpoint_beats_a_bad_timeout(self) -> None:
        rc = cli.main(["ls", "s3://b", "--endpoint-url", "badurl", "--cli-read-timeout", "abc"])
        assert rc == 252

    def test_read_timeout_beats_connect_timeout(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(
            ["ls", "s3://b", "--cli-read-timeout", "abc1", "--cli-connect-timeout", "abc2"]
        )
        assert rc == 255
        assert "abc1" in capsys.readouterr().err

    def test_bad_timeout_beats_the_page_size_coercion(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["ls", "s3://b", "--page-size", "xyz", "--cli-read-timeout", "abc"])
        assert rc == 255
        assert "abc" in capsys.readouterr().err

    def test_version_wins_over_a_bad_timeout(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws's --version exits during the top-level parse, before its
        # top-level-args-parsed resolutions fire.
        assert cli.main(["--version", "--cli-read-timeout", "abc"]) == 0
        capsys.readouterr()

    def test_help_wins_over_a_bad_timeout(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["--help", "--cli-read-timeout", "abc"]) == 0
        capsys.readouterr()

    def test_bad_profile_beats_the_local_local_usage_error(self) -> None:
        assert cli.main(["cp", "a", "b", "--profile", self._BOGUS]) == 255

    def test_bad_profile_beats_rm_path_usage(self) -> None:
        assert cli.main(["rm", "./missing-local", "--profile", self._BOGUS]) == 255

    def test_bad_profile_beats_mv_same_path(self) -> None:
        assert cli.main(["mv", "s3://b/x", "s3://b/x", "--profile", self._BOGUS]) == 255

    def test_bad_profile_beats_stream_recursive(self) -> None:
        rc = cli.main(["cp", "-", "s3://b/k", "--recursive", "--profile", self._BOGUS])
        assert rc == 255

    def test_bad_metadata_beats_missing_source(self) -> None:
        assert cli.main(["cp", self._MISSING, "s3://b/k", "--metadata", "bad,,=="]) == 252

    def test_bad_endpoint_beats_missing_source(self) -> None:
        rc = cli.main(["cp", self._MISSING, "s3://b/k", "--endpoint-url", "badurl"])
        assert rc == 252

    def test_bad_endpoint_beats_the_integer_coercion(self) -> None:
        rc = cli.main(["rm", "s3://b/k", "--page-size", "abc", "--endpoint-url", "badurl"])
        assert rc == 252

    def test_bad_blob_paramfile_beats_missing_source(self) -> None:
        rc = cli.main(
            ["cp", self._MISSING, "s3://b/k", "--sse-c", "AES256", "--sse-c-key", "fileb:///no/x"]
        )
        assert rc == 252

    def test_direct_paramfile_beats_the_integer_coercion(self) -> None:
        # aws expands file:// on plain options at parse time, BEFORE its bare
        # int(): --content-type's bad paramfile (252) wins over --page-size
        # abc (255). Measured.
        rc = cli.main(
            [
                "cp",
                self._MISSING,
                "s3://b/k",
                "--content-type",
                "file:///no/x",
                "--page-size",
                "abc",
            ]
        )
        assert rc == 252

    def test_ls_integer_coercion_beats_bucket_filter_paramfiles(self) -> None:
        # ls's option order puts --page-size ahead of the bucket-listing
        # filters, so its bare int() (255) fires before their bad paramfiles
        # (252) - the reverse of cp's direct-option case above. Measured.
        rc = cli.main(
            ["ls", "s3://b", "--page-size", "abc", "--bucket-name-prefix", "file:///no/x"]
        )
        assert rc == 255

    def test_ls_page_size_paramfile_beats_bucket_filter_paramfiles(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same slot order with both as bad paramfiles: aws names --page-size,
        # not the bucket filter. Measured.
        rc = cli.main(
            ["ls", "s3://b", "--page-size", "file:///no/x1", "--bucket-region", "file:///no/x2"]
        )
        assert rc == 252
        assert "Error parsing parameter '--page-size'" in capsys.readouterr().err

    def test_metadata_resolution_loses_to_the_integer_coercion(self) -> None:
        # The map option is the exception: its value (paramfile and shorthand
        # alike) resolves AFTER the coercions - aws exits the int's 255 here.
        rc = cli.main(
            ["cp", self._MISSING, "s3://b/k", "--metadata", "file:///no/x", "--page-size", "abc"]
        )
        assert rc == 255

    def test_integer_coercion_beats_a_later_paramfile(self) -> None:
        # aws interleaves the paramfile expansion and the int() per its
        # registration order: --progress-frequency (an integer, registered
        # before --page-size) fails its int() 255 ahead of a bad --page-size
        # file:// (252). CLI order is irrelevant - the registration order is.
        # Measured.
        assert (
            cli.main(
                [
                    "cp",
                    self._MISSING,
                    "s3://b/k",
                    "--progress-frequency",
                    "abc",
                    "--page-size",
                    "file:///no/x",
                ]
            )
            == 255
        )
        assert (
            cli.main(
                [
                    "cp",
                    self._MISSING,
                    "s3://b/k",
                    "--page-size",
                    "file:///no/x",
                    "--progress-frequency",
                    "abc",
                ]
            )
            == 255
        )

    def test_page_size_int_beats_a_later_expected_size_paramfile(self) -> None:
        # --expected-size is registered AFTER --page-size, so --page-size abc's
        # int() 255 wins over --expected-size file://missing (252). Measured.
        rc = cli.main(
            ["cp", "-", "s3://b/k", "--page-size", "abc", "--expected-size", "file:///no/x"]
        )
        assert rc == 255

    def test_ls_endpoint_beats_the_integer_coercion(self) -> None:
        rc = cli.main(["ls", "s3://b", "--page-size", "abc", "--endpoint-url", "badurl"])
        assert rc == 252

    def test_presign_endpoint_beats_the_integer_coercion(self) -> None:
        rc = cli.main(["presign", "s3://b/k", "--expires-in", "abc", "--endpoint-url", "badurl"])
        assert rc == 252

    def test_page_size_paramfile_reference_is_expanded(self) -> None:
        # aws paramfile-expands even the string-typed integer options: a bad
        # file:// reference on --page-size is its 252, not the int()'s 255.
        assert cli.main(["ls", "s3://b", "--page-size", "file:///no/x"]) == 252

    def test_metadata_fileb_shorthand_value_is_a_usage_error(self) -> None:
        # a@=fileb://... loads bytes; aws rejects the non-string map value at
        # parse time (252) - it must not reach the transfer (rc 1).
        rc = cli.main(["cp", self._MISSING, "s3://b/k", "--metadata", "a@=fileb:///no/x"])
        assert rc == 252  # the missing paramfile itself is also a 252

    def test_whole_value_metadata_fileb_missing_is_252(self) -> None:
        # A whole-value --metadata fileb:// load failure is the paramfile 252.
        rc = cli.main(["cp", self._MISSING, "s3://b/k", "--metadata", "fileb:///no/x"])
        assert rc == 252

    def test_whole_value_metadata_fileb_existing_is_255(self, tmp_path: Path) -> None:
        # aws loads the bytes, then crashes indexing them in its map shorthand
        # parser: a general 255, not the map's usage 252. Measured.
        blob = tmp_path / "m.bin"
        blob.write_bytes(b"k=v")
        rc = cli.main(["cp", self._MISSING, "s3://b/k", "--metadata", f"fileb://{blob}"])
        assert rc == 255

    def test_free_string_option_fileb_is_a_param_validation_252(self, tmp_path: Path) -> None:
        # A fileb:// on a string-typed option loads bytes, which botocore
        # rejects for a string parameter (252), not a transfer. Measured.
        blob = tmp_path / "ct.bin"
        blob.write_bytes(b"hi")
        rc = cli.main(
            ["cp", self._MISSING, "s3://b/k", "--content-type", f"fileb://{blob}", "--dryrun"]
        )
        assert rc == 252

    def test_query_compile_beats_the_endpoint_and_paramfile(self) -> None:
        # aws resolves --query at top-level-args-parsed, ahead of --endpoint-url
        # and every paramfile: a bad expression is its 252 first. Measured for
        # each transfer command (they share the classify_paths head).
        for command in ("cp", "mv", "sync"):
            rc = cli.main(
                [command, self._MISSING, "s3://b/k", "--query", "][", "--endpoint-url", "badurl"]
            )
            assert rc == 252, command

    def test_local_local_stays_252_in_a_regionless_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws's client construction cannot fail on a missing region (its
        # bundled botocore defers region resolution to request time), so the
        # usage error must keep winning in a regionless env - this guards
        # against ever hoisting the client build above the validations.
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        assert cli.main(["cp", "a", "b"]) == 252


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
    Measured on the pinned aws-cli: ``s3 help`` and ``s3 ls help`` are 0,
    ``s3 help foo`` is 252, and a bad timeout still beats the token (255)."""

    def test_top_level_help_token(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["help"]) == 0
        assert "usage:" in capsys.readouterr().out.lower()

    def test_help_token_after_globals(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["--debug", "help"]) == 0
        capsys.readouterr()

    def test_subcommand_help_token(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["ls", "help"]) == 0
        assert "usage: boto3-s3 ls" in capsys.readouterr().out

    def test_help_token_beats_missing_arguments(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["cp", "help"]) == 0
        capsys.readouterr()

    def test_help_token_with_extras_is_not_special(self) -> None:
        assert cli.main(["help", "foo"]) == 252

    def test_bad_timeout_beats_the_help_token(self) -> None:
        assert cli.main(["help", "--cli-read-timeout", "abc"]) == 255
