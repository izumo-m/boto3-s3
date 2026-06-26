"""``boto3_s3.naming``: the pure path-shape rules behind cp/mv/sync.

Pins the aws-cli parity contracts (aws-cli ``fileformat.py`` +
``find_dest_path_comp_key`` + ``filters._get_*_root``): how a path pair
resolves to roots and ``use_src_name``, how each item's destination and
``compare_key`` derive from its source path, and how the filter root feeds
``globsieve.translate_pattern_for_root``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import BinaryIO, Literal

import pytest
from typing_extensions import override

from boto3_s3 import LocalStorage, S3Storage, Storage, naming
from boto3_s3.exceptions import ValidationError
from boto3_s3.globsieve import translate_pattern_for_root
from boto3_s3.naming import (
    classify,
    dest_for,
    item_paths,
    local_format,
    normalize_s3_uri,
    s3_format,
    same_key,
    same_path,
    split_bucket_key,
)
from boto3_s3.types import FileInfo, ScanOptions

_ACCESSPOINT_ARN = "arn:aws:s3:us-west-2:123456789012:accesspoint/myap"
_OUTPOST_ARN = "arn:aws:s3-outposts:us-east-1:123456789012:outpost/op-01234567/accesspoint/my-ap"


def _storage(arg: str) -> S3Storage | LocalStorage:
    """The schema-bearing endpoint the test feeds plan_transfer, chosen by
    ``classify`` on the raw path - exactly how the CLI builds its endpoints."""
    return S3Storage(arg) if classify(arg) == "s3" else LocalStorage(arg)


def plan_transfer(
    src: str, dst: str, *, recursive: bool, operation: str = "cp"
) -> naming.TransferPlan:
    """Test wrapper: build each endpoint Storage from its raw path (classify-chosen)
    and call the real formatter, so the call sites below stay path-focused.
    """
    return naming.plan_transfer(
        _storage(src), _storage(dst), recursive=recursive, operation=operation
    )


class _FakeOpen(Storage):
    """A custom (open-routed) Storage for plan_transfer tests: its schema is
    neither s3 nor local, so it routes through the open seam. Only ``schema`` and
    ``as_text`` matter to the formatter; the I/O methods stay inert."""

    schema = "mem"

    def __init__(self, token: str = "mem://x") -> None:
        self._token = token

    @override
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        raise NotImplementedError

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        raise NotImplementedError

    @override
    def delete(self, key: str) -> None:
        raise NotImplementedError

    @override
    def get_fileinfo(
        self,
        key: str = "",
        *,
        follow_symlinks: bool = True,
        on_warning: Callable[[str], None] | None = None,
    ) -> FileInfo | None:
        return None

    @override
    def as_text(self) -> str:
        return self._token


class TestClassify:
    def test_s3_scheme(self) -> None:
        assert classify("s3://bucket/key") == "s3"

    def test_bare_bucket_is_local(self) -> None:
        # Only the literal scheme marks S3 (aws identify_type); a bare
        # "bucket/key" is a local path for cp purposes.
        assert classify("bucket/key") == "local"

    def test_scheme_is_case_sensitive(self) -> None:
        assert classify("S3://bucket") == "local"


class TestSplitBucketKey:
    def test_plain_forms(self) -> None:
        assert split_bucket_key("b") == ("b", "")
        assert split_bucket_key("b/") == ("b", "")
        assert split_bucket_key("b/k/x") == ("b", "k/x")
        assert split_bucket_key("") == ("", "")
        assert split_bucket_key("/k") == ("", "k")

    def test_accesspoint_arn_keeps_the_arn_whole(self) -> None:
        assert split_bucket_key(f"{_ACCESSPOINT_ARN}/key.txt") == (_ACCESSPOINT_ARN, "key.txt")
        assert split_bucket_key(_ACCESSPOINT_ARN) == (_ACCESSPOINT_ARN, "")

    def test_outpost_accesspoint_arn(self) -> None:
        assert split_bucket_key(f"{_OUTPOST_ARN}/k") == (_OUTPOST_ARN, "k")


class TestLocalFormat:
    def test_existing_directory_takes_src_name(self, tmp_path: Path) -> None:
        assert local_format(str(tmp_path), dir_op=False) == (str(tmp_path) + os.sep, True)

    def test_existing_file_keeps_given_name(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_bytes(b"x")
        assert local_format(str(target), dir_op=False) == (str(target), False)

    def test_dir_op_forces_directory_semantics(self, tmp_path: Path) -> None:
        missing = tmp_path / "new"
        assert local_format(str(missing), dir_op=True) == (str(missing) + os.sep, True)

    def test_trailing_separator_on_missing_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "new"
        assert local_format(str(missing) + os.sep, dir_op=False) == (str(missing) + os.sep, True)

    def test_missing_plain_path_keeps_given_name(self, tmp_path: Path) -> None:
        missing = tmp_path / "renamed.bin"
        assert local_format(str(missing), dir_op=False) == (str(missing), False)

    def test_relative_path_is_absolutized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert local_format("sub/f.txt", dir_op=False) == (str(tmp_path / "sub" / "f.txt"), False)


class TestS3Format:
    def test_dir_op_appends_slash(self) -> None:
        assert s3_format("b/pre", dir_op=True) == ("b/pre/", True)

    def test_dir_op_keeps_existing_slash(self) -> None:
        assert s3_format("b/pre/", dir_op=True) == ("b/pre/", True)

    def test_single_with_trailing_slash_takes_src_name(self) -> None:
        assert s3_format("b/pre/", dir_op=False) == ("b/pre/", True)

    def test_single_plain_keeps_given_name(self) -> None:
        assert s3_format("b/pre/key", dir_op=False) == ("b/pre/key", False)


class TestPlanTransfer:
    def test_local_to_local_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            plan_transfer(str(tmp_path / "a"), str(tmp_path / "b"), recursive=False)

    def test_upload_single_to_keyless_bucket(self, tmp_path: Path) -> None:
        # "s3://bucket" reads as the bucket root "bucket/" (aws-cli
        # _normalize_s3_trailing_slash), so the key takes the source's name.
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        plan = plan_transfer(str(src), "s3://bucket", recursive=False)
        assert (plan.paths_type, plan.dir_op) == ("locals3", False)
        assert (plan.dst_root, plan.use_src_name) == ("bucket/", True)
        assert plan.src_root == str(src)
        assert (plan.src_sep, plan.dst_sep) == (os.sep, "/")

    def test_upload_single_rename(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        plan = plan_transfer(str(src), "s3://bucket/up/key.bin", recursive=False)
        assert (plan.dst_root, plan.use_src_name) == ("bucket/up/key.bin", False)

    def test_download_recursive(self, tmp_path: Path) -> None:
        plan = plan_transfer("s3://b/pre", str(tmp_path / "out"), recursive=True)
        assert plan.paths_type == "s3local"
        assert plan.src_root == "b/pre/"
        assert plan.dst_root == str(tmp_path / "out") + os.sep
        assert plan.use_src_name is True
        assert plan.filter_root == "pre"

    def test_s3_single_source_roots_at_parent(self, tmp_path: Path) -> None:
        plan = plan_transfer("s3://b/pre/key", str(tmp_path / "f"), recursive=False)
        assert plan.src_root == "b/pre/key"
        assert plan.filter_root == "pre"

    def test_s3_to_s3(self) -> None:
        plan = plan_transfer("s3://a/x/", "s3://b/y", recursive=True)
        assert plan.paths_type == "s3s3"
        assert (plan.src_root, plan.dst_root) == ("a/x/", "b/y/")

    def test_bare_service_root_stays_empty(self, tmp_path: Path) -> None:
        plan = plan_transfer("s3://", str(tmp_path), recursive=False)
        assert plan.src_root == ""

    def test_keyless_accesspoint_arn_is_bucket_root(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        plan = plan_transfer(str(src), f"s3://{_ACCESSPOINT_ARN}", recursive=False)
        assert (plan.dst_root, plan.use_src_name) == (f"{_ACCESSPOINT_ARN}/", True)


class TestPlanTransferOpenRoute:
    """The custom-backend 'open' route: opens3 / s3open, rooted at "" so the
    relative compare_key is handed straight to the custom side's open()."""

    def test_custom_source_to_s3_is_opens3(self) -> None:
        plan = naming.plan_transfer(_FakeOpen(), S3Storage("s3://b/dst/"), recursive=True)
        assert plan.paths_type == "opens3"
        # the custom side roots at "" and is addressed by relative compare_key
        assert (plan.src_root, plan.src_sep, plan.filter_root) == ("", "/", "")
        # use_src_name comes from the s3 dst (dir_op -> adopts the source name)
        assert plan.use_src_name is True
        assert dest_for(plan, "sub/f.txt") == "b/dst/sub/f.txt"

    def test_s3_to_custom_dest_is_s3open(self) -> None:
        plan = naming.plan_transfer(S3Storage("s3://b/pre"), _FakeOpen(), recursive=True)
        assert plan.paths_type == "s3open"
        # the custom dst roots at "" so dest_for hands the relative key to open()
        assert (plan.dst_root, plan.dst_sep) == ("", "/")
        assert plan.use_src_name is True
        assert dest_for(plan, "sub/f.txt") == "sub/f.txt"

    def test_custom_dest_single_rename_targets_the_location_itself(self) -> None:
        # non-dir_op, no trailing "/" -> use_src_name False -> dest_for returns ""
        # ("" is the custom Storage's own location, per the Storage.open key rule)
        plan = naming.plan_transfer(
            S3Storage("s3://b/key"), _FakeOpen("mem://one"), recursive=False
        )
        assert (plan.paths_type, plan.use_src_name) == ("s3open", False)
        assert dest_for(plan, "key") == ""

    def test_custom_dest_trailing_slash_adopts_src_name(self) -> None:
        # a trailing "/" in the custom token means directory semantics (s3_format parity)
        plan = naming.plan_transfer(
            S3Storage("s3://b/key"), _FakeOpen("mem://dir/"), recursive=False
        )
        assert plan.use_src_name is True
        assert dest_for(plan, "key") == "key"

    def test_custom_to_local_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            naming.plan_transfer(_FakeOpen(), LocalStorage(str(tmp_path)), recursive=False)

    def test_local_to_custom_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            naming.plan_transfer(LocalStorage(str(tmp_path)), _FakeOpen(), recursive=False)

    def test_custom_to_custom_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            naming.plan_transfer(_FakeOpen(), _FakeOpen(), recursive=False)


class TestItemPaths:
    def test_recursive_upload_slices_the_source_root(self, tmp_path: Path) -> None:
        plan = plan_transfer(str(tmp_path), "s3://b/tree", recursive=True)
        src_path = plan.src_root + os.path.join("sub", "f.txt")
        dest, compare_key = item_paths(plan, src_path)
        assert dest == "b/tree/sub/f.txt"
        assert compare_key == "sub/f.txt"

    def test_single_upload_uses_the_basename(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        plan = plan_transfer(str(src), "s3://b/up/", recursive=False)
        dest, compare_key = item_paths(plan, plan.src_root)
        assert dest == "b/up/a.txt"
        assert compare_key == "a.txt"

    def test_single_rename_ignores_the_source_name(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        plan = plan_transfer(str(src), "s3://b/up/key.bin", recursive=False)
        dest, compare_key = item_paths(plan, plan.src_root)
        assert dest == "b/up/key.bin"
        assert compare_key == "a.txt"

    def test_directory_source_yields_empty_relative_part(self, tmp_path: Path) -> None:
        # `cp <existing-dir> s3://b/key` (no --recursive): the formatted source
        # ends with os.sep, so the relative part is empty and the destination
        # is taken verbatim - the transfer then fails at open() like aws
        # ([Errno 21] Is a directory, rc 1).
        plan = plan_transfer(str(tmp_path), "s3://b/key", recursive=False)
        dest, compare_key = item_paths(plan, plan.src_root)
        assert dest == "b/key"
        assert compare_key == ""

    def test_recursive_download_translates_separators(self, tmp_path: Path) -> None:
        plan = plan_transfer("s3://b/pre/", str(tmp_path / "out"), recursive=True)
        dest, compare_key = item_paths(plan, "b/pre/sub/f.txt")
        assert dest == str(tmp_path / "out" / "sub" / "f.txt")
        assert compare_key == "sub/f.txt"


class TestDestFor:
    """``dest_for`` is the dest half of ``item_paths``, fed a producer-stamped
    ``compare_key`` directly (what the transfer item builders use)."""

    def test_appends_compare_key_when_dest_takes_source_name(self, tmp_path: Path) -> None:
        plan = plan_transfer(str(tmp_path), "s3://b/tree", recursive=True)
        assert plan.use_src_name is True
        assert dest_for(plan, "sub/f.txt") == "b/tree/sub/f.txt"

    def test_root_verbatim_when_dest_keeps_its_name(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        plan = plan_transfer(str(src), "s3://b/up/key.bin", recursive=False)
        assert plan.use_src_name is False
        assert dest_for(plan, "a.txt") == "b/up/key.bin"

    def test_translates_to_the_destination_separator(self, tmp_path: Path) -> None:
        plan = plan_transfer("s3://b/pre/", str(tmp_path / "out"), recursive=True)
        assert dest_for(plan, "sub/f.txt") == str(tmp_path / "out" / "sub" / "f.txt")

    def test_matches_item_paths(self, tmp_path: Path) -> None:
        plan = plan_transfer(str(tmp_path), "s3://b/tree", recursive=True)
        src_path = plan.src_root + os.path.join("sub", "f.txt")
        dest, compare_key = item_paths(plan, src_path)
        assert dest_for(plan, compare_key) == dest


class TestFilterRoot:
    """The plan's filter root composes with translate_pattern_for_root so the
    matcher - fed each item's compare_key - selects the same set aws-cli's
    rootdir-joined fnmatch selects."""

    def test_local_single_roots_at_the_parent_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        plan = plan_transfer(str(src), "s3://b/k", recursive=False)
        assert plan.filter_root == str(tmp_path)
        assert translate_pattern_for_root("*.txt", plan.filter_root) == "*.txt"

    def test_local_recursive_roots_at_the_directory(self, tmp_path: Path) -> None:
        plan = plan_transfer(str(tmp_path), "s3://b/tree", recursive=True)
        assert plan.filter_root == str(tmp_path)
        anchored = str(tmp_path / "sub") + "/*"
        assert translate_pattern_for_root(anchored, plan.filter_root) == "sub/*"
        assert translate_pattern_for_root("/elsewhere/*", plan.filter_root) is None

    def test_s3_recursive_root_is_the_raw_key(self, tmp_path: Path) -> None:
        plan = plan_transfer("s3://b/pre", str(tmp_path), recursive=True)
        assert plan.filter_root == "pre"
        assert translate_pattern_for_root("*.txt", "pre") == "*.txt"

    def test_s3_bucket_root_single_key(self, tmp_path: Path) -> None:
        plan = plan_transfer("s3://b/key", str(tmp_path / "f"), recursive=False)
        assert plan.filter_root == ""
        assert translate_pattern_for_root("key*", "") == "key*"


class TestMvSamePathGuards:
    """The mv onto-itself rules (aws-cli ``_same_path`` / ``_same_key``).

    All three shapes - exact, implied basename, keyless destination - exit
    252 on aws, and ``--recursive`` is not exempt
    (``mv --recursive s3://b/d s3://b/`` trips the basename rule even
    though no key would map onto itself; the false positive is replicated).
    """

    def test_exact_equality(self) -> None:
        assert same_path("s3://b/k.txt", "s3://b/k.txt")
        assert not same_path("s3://b/k.txt", "s3://b/k2.txt")
        assert not same_path("s3://b/k.txt", "s3://other/k.txt")

    def test_implied_basename_against_a_slash_destination(self) -> None:
        assert same_path("s3://b/d/a.txt", "s3://b/d/")
        assert same_path("s3://b/d", "s3://b/")
        assert not same_path("s3://b/d/a.txt", "s3://b/other/")

    def test_normalize_s3_uri_feeds_the_keyless_shape(self) -> None:
        assert normalize_s3_uri("s3://b") == "s3://b/"
        assert normalize_s3_uri("s3://b/") == "s3://b/"
        assert normalize_s3_uri("s3://b/k") == "s3://b/k"
        assert normalize_s3_uri("s3://") == "s3://"
        # The keyless pair from the probe: `mv s3://b/k.txt s3://b` -> error
        # showing "s3://b/" as the destination.
        assert same_path("s3://b/k.txt", normalize_s3_uri("s3://b"))

    def test_same_key_ignores_the_bucket(self) -> None:
        assert same_key("s3://b1/k.txt", "s3://b2/k.txt")
        assert not same_key("s3://b1/k.txt", "s3://b2/other.txt")
        # The '/'-anchored basename rule applies to the key comparison too:
        # a keyless destination matches a source whose key has no prefix.
        assert same_key("s3://b1/k.txt", "s3://b2/")
        assert not same_key("s3://b1/d/k.txt", "s3://b2/")
        assert same_key("s3://b1/d/k.txt", "s3://b2/d/")

    def test_same_key_splits_access_point_arns_whole(self) -> None:
        assert same_key(f"s3://{_ACCESSPOINT_ARN}/k.txt", "s3://plain-bucket/k.txt")
        assert not same_key(f"s3://{_ACCESSPOINT_ARN}/k.txt", "s3://plain-bucket/other.txt")
