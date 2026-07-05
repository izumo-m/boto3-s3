"""Unit tests for the aws-cli exit-code mapping (docs/cli.md section 6).

``exit_code_for`` is exercised directly for each branch, and ``main`` is
exercised end-to-end for the paths that do not go through a library error
(usage errors, unknown options) plus the ClientError path through a fake
client, so the parse -> dispatch -> error -> exit-code wiring is covered.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from botocore.exceptions import ClientError

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


class TestMainExitCodes:
    def test_unknown_option_exits_252_with_aws_wording(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["ls", "--extra-argument-foo", "s3://bucket/p/"])
        err = capsys.readouterr().err
        assert rc == 252
        assert "Unknown options" in err
        assert "--extra-argument-foo" in err

    def test_unknown_options_joined_with_comma_no_space(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws s3 joins multiple unknown options with "," and NO space (the
        # customizations command layer; verified against real aws 2.35.5), not
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


class TestParseToValidationOrder:
    """The head order aws applies before its path validations (measured
    against the pinned aws 2.35.5; docs/cli.md section 6): the
    ``--endpoint-url`` scheme check (252) -> the integer coercions (255) ->
    paramfile / shorthand / blob value resolution (252) -> the session
    profile resolution (255) -> the path/usage checks. Each test pins one
    combined-error pair whose exit code that order decides."""

    _BOGUS = "boto3_s3_no_such_profile_for_order_tests"
    _MISSING = "/nonexistent_src_for_head_order_test.txt"

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

    def test_metadata_resolution_loses_to_the_integer_coercion(self) -> None:
        # The map option is the exception: its value (paramfile and shorthand
        # alike) resolves AFTER the coercions - aws exits the int's 255 here.
        rc = cli.main(
            ["cp", self._MISSING, "s3://b/k", "--metadata", "file:///no/x", "--page-size", "abc"]
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
    def readonly_dir(self, tmp_path: Any) -> Any:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root creates anything")
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        yield ro
        ro.chmod(0o755)

    def test_cp_uncreatable_recursive_dest_exits_255(self, readonly_dir: Any) -> None:
        rc = cli.main(["cp", "s3://b/pre", str(readonly_dir / "new"), "--recursive"])
        assert rc == 255

    def test_mv_uncreatable_recursive_dest_exits_255(self, readonly_dir: Any) -> None:
        rc = cli.main(["mv", "s3://b/pre", str(readonly_dir / "new"), "--recursive"])
        assert rc == 255
