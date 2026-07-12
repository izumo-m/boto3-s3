"""Unit tests for the ``boto3-s3 ls`` subcommand (dispatch + output + exit code).

A ``Context`` whose client factory returns a hand-rolled fake S3 client is
injected into ``cli.main``, so the tests exercise the full parse -> dispatch ->
scan -> format path without network and without monkeypatching.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context

_MTIME = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self._pages = pages
        self._calls = calls

    def paginate(self, **kwargs: Any) -> Any:
        self._calls.append(kwargs)
        return iter(self._pages)


class _FakeS3Client:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []
        self.paginator_names: list[str] = []

    def can_paginate(self, name: str) -> bool:
        return True

    def get_paginator(self, name: str) -> _FakePaginator:
        self.paginator_names.append(name)
        return _FakePaginator(self._pages, self.calls)


def _fake_ctx(pages: list[dict[str, Any]]) -> tuple[Context, _FakeS3Client]:
    client = _FakeS3Client(pages)
    return Context(client_factory=lambda _args: client), client


def _obj(key: str, size: int = 1) -> dict[str, Any]:
    return {"Key": key, "Size": size, "LastModified": _MTIME}


def _bucket(name: str) -> dict[str, Any]:
    return {"Name": name, "CreationDate": _MTIME}


class TestLs:
    def test_lists_objects_with_basenames(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx, _ = _fake_ctx([{"Contents": [_obj("prefix/a.txt"), _obj("prefix/b.txt")]}])
        rc = cli.main(["ls", "s3://bucket/prefix/"], ctx=ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "a.txt" in out
        assert "b.txt" in out
        assert "prefix/a.txt" not in out  # basenames only when non-recursive

    def test_recursive_shows_full_keys(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx, _ = _fake_ctx([{"Contents": [_obj("prefix/sub/a.txt")]}])
        rc = cli.main(["ls", "--recursive", "s3://bucket/prefix/"], ctx=ctx)
        assert rc == 0
        assert "prefix/sub/a.txt" in capsys.readouterr().out

    def test_common_prefixes_render_pre(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx, _ = _fake_ctx([{"CommonPrefixes": [{"Prefix": "prefix/sub/"}]}])
        rc = cli.main(["ls", "s3://bucket/prefix/"], ctx=ctx)
        assert rc == 0
        assert "PRE sub/" in capsys.readouterr().out

    def test_summarize_footer(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx, _ = _fake_ctx([{"Contents": [_obj("p/a", 100), _obj("p/b", 200)]}])
        rc = cli.main(["ls", "--summarize", "s3://bucket/p/"], ctx=ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Total Objects: 2" in out
        assert "Total Size: 300" in out

    def test_key_prefix_with_no_matches_exits_one(self) -> None:
        ctx, _ = _fake_ctx([{}])
        assert cli.main(["ls", "s3://bucket/prefix/"], ctx=ctx) == 1

    def test_empty_bucket_without_key_exits_zero(self) -> None:
        # aws-cli parity: no key specified -> rc 0 even when nothing matched.
        ctx, _ = _fake_ctx([{}])
        assert cli.main(["ls", "s3://bucket"], ctx=ctx) == 0

    def test_non_integer_page_size_exits_255(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws converts integer options with a bare int(); the ValueError hits
        # its *general* handler -> 255, not argparse's usage error 252. The
        # conversion fails before the client factory runs.
        def factory(_args: Any) -> Any:
            raise AssertionError("client factory must not be called")

        rc = cli.main(
            ["ls", "s3://bucket/p/", "--page-size", "abc"], ctx=Context(client_factory=factory)
        )
        assert rc == 255
        assert "invalid literal for int()" in capsys.readouterr().err


class TestLsAllBuckets:
    """``ls`` with no bucket in the target lists all buckets (aws-cli parity)."""

    def test_no_target_lists_all_buckets(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx, client = _fake_ctx([{"Buckets": [_bucket("alpha"), _bucket("beta")]}])
        rc = cli.main(["ls"], ctx=ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert client.paginator_names == ["list_buckets"]
        date = _MTIME.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        assert out.splitlines() == [f"{date} alpha", f"{date} beta"]  # no size column

    def test_bare_s3_uri_lists_buckets(self) -> None:
        ctx, client = _fake_ctx([{"Buckets": [_bucket("alpha")]}])
        assert cli.main(["ls", "s3://"], ctx=ctx) == 0
        assert client.paginator_names == ["list_buckets"]

    def test_key_without_bucket_lists_buckets(self) -> None:
        # aws-cli quirk kept for parity: "s3:///k" has no bucket, so the key is
        # discarded and all buckets are listed.
        ctx, client = _fake_ctx([{"Buckets": [_bucket("alpha")]}])
        assert cli.main(["ls", "s3:///k"], ctx=ctx) == 0
        assert client.paginator_names == ["list_buckets"]

    def test_bucket_filters_forwarded(self) -> None:
        ctx, client = _fake_ctx([{"Buckets": []}])
        rc = cli.main(["ls", "--bucket-name-prefix", "al", "--bucket-region", "us-west-2"], ctx=ctx)
        assert rc == 0
        assert client.calls[0]["Prefix"] == "al"
        assert client.calls[0]["BucketRegion"] == "us-west-2"

    def test_no_buckets_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx, _ = _fake_ctx([{"Buckets": []}])
        assert cli.main(["ls"], ctx=ctx) == 0
        assert capsys.readouterr().out == ""

    def test_summarize_counts_no_buckets(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws-cli prints the footer with zero totals: buckets are not objects.
        ctx, _ = _fake_ctx([{"Buckets": [_bucket("alpha")]}])
        assert cli.main(["ls", "--summarize"], ctx=ctx) == 0
        out = capsys.readouterr().out
        assert "Total Objects: 0" in out
        assert "Total Size: 0" in out

    def test_ignored_globals_are_accepted(self) -> None:
        ctx, _ = _fake_ctx([{"Contents": [_obj("p/a")]}])
        assert cli.main(["ls", "--no-paginate", "--output", "json", "s3://bucket/p/"], ctx=ctx) == 0

    def test_invalid_choice_exits_param_validation_rc(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse exits 2 on a bad choice; main absorbs it and remaps to
        # aws-cli's 252 (exit-code charter, docs/cli.md section 6).
        assert cli.main(["ls", "--color", "bogus", "s3://bucket/p/"]) == 252
        assert "--color" in capsys.readouterr().err


class TestGlobalOptionPosition:
    """Globals must parse whether they precede or follow the subcommand."""

    def test_global_before_subcommand(self) -> None:
        args = cli.build_parser().parse_args(["--region", "us-west-2", "ls", "s3://b/p"])
        assert args.command == "ls"
        assert args.region == "us-west-2"
        assert args.paths == "s3://b/p"

    def test_global_after_subcommand(self) -> None:
        args = cli.build_parser().parse_args(["ls", "s3://b/p", "--region", "us-west-2"])
        assert args.region == "us-west-2"
        assert args.paths == "s3://b/p"

    def test_global_before_is_not_clobbered_by_subcommand_default(self) -> None:
        # The regression: a value parsed before the subcommand must survive the
        # subparser's (suppressed) default.
        args = cli.build_parser().parse_args(["--profile", "my-profile", "ls", "bucket"])
        assert args.profile == "my-profile"
        assert args.paths == "bucket"


class TestRecognizedExtraGlobals:
    """The remaining aws s3 ls globals: --version, --cli-binary-format, --output off."""

    def test_version_lists_all_components_on_one_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The version action's SystemExit(0) is absorbed by main -> rc 0.
        assert cli.main(["--version"]) == 0
        out = capsys.readouterr().out
        assert out.endswith("\n") and out.count("\n") == 1  # one unwrapped line
        for token in ("boto3-s3-cli/", "boto3-s3/", "boto3/", "botocore/", "Python/"):
            assert token in out

    def test_version_after_subcommand(self) -> None:
        assert cli.main(["ls", "--version"]) == 0

    def test_cli_binary_format_accepted(self) -> None:
        ctx, _ = _fake_ctx([{"Contents": [_obj("p/a")]}])
        assert cli.main(["ls", "--cli-binary-format", "base64", "s3://b/p/"], ctx=ctx) == 0

    def test_output_off_is_accepted(self) -> None:
        ctx, _ = _fake_ctx([{"Contents": [_obj("p/a")]}])
        assert cli.main(["ls", "--output", "off", "s3://b/p/"], ctx=ctx) == 0

    def test_cli_binary_format_invalid_choice_exits_param_validation_rc(self) -> None:
        assert cli.main(["ls", "--cli-binary-format", "bogus", "s3://b/p/"]) == 252


def _unused_factory(_args: Any) -> Any:
    raise AssertionError("client factory must not be called")


class TestParamfileExpansion:
    """aws paramfile-expands the positional and the bucket-listing filters at
    parse time (rc 252 on a bad reference), naming the argument as aws does."""

    def test_positional_file_reference_is_expanded(self, tmp_path: Any) -> None:
        # Expands to a key prefix that matches nothing -> rc 1, proving the
        # reference was parsed as s3://bucket/p/ and not the literal string.
        ref = tmp_path / "u.txt"
        ref.write_text("s3://bucket/p/")
        ctx, _ = _fake_ctx([{}])
        assert cli.main(["ls", f"file://{ref}"], ctx=ctx) == 1

    def test_positional_missing_reference_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["ls", "file:///no/x"], ctx=Context(client_factory=_unused_factory))
        assert rc == 252
        err = capsys.readouterr().err
        assert "Error parsing parameter 'paths': Unable to load paramfile" in err

    def test_bucket_name_prefix_missing_reference_is_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            ["ls", "s3://b", "--bucket-name-prefix", "file:///no/x"],
            ctx=Context(client_factory=_unused_factory),
        )
        assert rc == 252
        assert "Error parsing parameter '--bucket-name-prefix'" in capsys.readouterr().err

    def test_bucket_region_missing_reference_is_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            ["ls", "s3://b", "--bucket-region", "file:///no/x"],
            ctx=Context(client_factory=_unused_factory),
        )
        assert rc == 252
        assert "Error parsing parameter '--bucket-region'" in capsys.readouterr().err

    def test_bucket_name_prefix_reference_is_expanded(self) -> None:
        ctx, client = _fake_ctx([{"Buckets": []}])
        # A readable reference would forward its contents as Prefix; here just
        # confirm a bare value still forwards (the expand is a no-op without the
        # prefix, so behavior is unchanged for the common case).
        assert cli.main(["ls", "--bucket-name-prefix", "al"], ctx=ctx) == 0
        assert client.calls[0]["Prefix"] == "al"

    def test_positional_missing_fileb_reference_is_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws expands fileb:// as well as file://, so a missing one is the same
        # load 252 (not the 254/1 of a literal path reaching the client).
        rc = cli.main(["ls", "fileb:///no/x"], ctx=Context(client_factory=_unused_factory))
        assert rc == 252
        assert (
            "Error parsing parameter 'paths': Unable to load paramfile" in capsys.readouterr().err
        )

    def test_positional_fileb_reference_reproduces_aws_type_bug_255(
        self, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws-cli leaves positional bytes in ListCommand and crashes when its
        # path code calls bytes.startswith(str). Keep this bug-shaped rc 255.
        ref = tmp_path / "u.bin"
        ref.write_bytes(b"s3://bucket/p/")
        rc = cli.main(["ls", f"fileb://{ref}"], ctx=Context(client_factory=_unused_factory))
        assert rc == 255
        assert "startswith first arg must be bytes" in capsys.readouterr().err

    def test_page_size_missing_fileb_reference_is_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The integer option loads the paramfile before int(), so a missing
        # fileb:// is the load 252, not the bare int() coercion's 255.
        rc = cli.main(
            ["ls", "s3://b", "--page-size", "fileb:///no/x"],
            ctx=Context(client_factory=_unused_factory),
        )
        assert rc == 252
        assert "Error parsing parameter '--page-size'" in capsys.readouterr().err

    def test_page_size_fileb_digits_coerce_to_int(self, tmp_path: Any) -> None:
        # aws feeds the loaded bytes to int() (b"5" -> 5), so a readable
        # digit fileb:// drives a normal listing with the coerced page size.
        ref = tmp_path / "n.bin"
        ref.write_bytes(b"5")
        ctx, client = _fake_ctx([{"Contents": []}])
        assert cli.main(["ls", "s3://b", "--page-size", f"fileb://{ref}"], ctx=ctx) == 0
        assert client.calls[0]["PaginationConfig"]["PageSize"] == 5

    def test_page_size_fileb_non_digits_is_255(
        self, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A non-integer fileb:// fails int() at 255 like aws, whose ValueError
        # repr shows the bytes form (measured: ``... base 10: b'abc'``).
        ref = tmp_path / "bad.bin"
        ref.write_bytes(b"abc")
        rc = cli.main(
            ["ls", "s3://b", "--page-size", f"fileb://{ref}"],
            ctx=Context(client_factory=_unused_factory),
        )
        assert rc == 255
        assert "invalid literal for int() with base 10: b'abc'" in capsys.readouterr().err


class TestQueryValidation:
    """aws compiles --query at parse time; a bad JMESPath is its 252, ahead of
    every other head check (globalargs.validate_query)."""

    def test_invalid_query_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(
            ["ls", "s3://b", "--query", "]["], ctx=Context(client_factory=_unused_factory)
        )
        assert rc == 252
        assert "Bad value for --query ][" in capsys.readouterr().err

    def test_valid_query_is_accepted(self) -> None:
        ctx, _ = _fake_ctx([{"Contents": [_obj("p/a")]}])
        assert cli.main(["ls", "s3://b/p/", "--query", "Contents[].Key"], ctx=ctx) == 0
