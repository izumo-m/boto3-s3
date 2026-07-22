"""Unit tests for ``Storage.as_text`` - the canonical aws-cli path-shape token.

``as_text`` is the inverse of ``S3.resolve`` for locatable Storages
(``S3Storage`` / ``LocalStorage``) and a display-only token (``"-"``) for stream
endpoints; ``str(storage)`` delegates to it. The goldens below pin the form the
transfer planner (``transferplan``) and each backend's ``format`` grammar
consume - in particular that a keyless S3 location is reconstructed slashless
(``s3://bucket``), not echoed from the raw, possibly trailing-slashed
constructor input.
"""

from __future__ import annotations

import io

import pytest

from boto3_s3 import S3, LocalStorage, S3Storage
from boto3_s3.iostorage import IOStorage, StdioStorage


class TestS3StorageAsText:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("s3://bucket/key", "s3://bucket/key"),  # keyed
            ("s3://bucket/some/prefix/", "s3://bucket/some/prefix/"),  # key keeps its slash
            ("s3://bucket", "s3://bucket"),  # keyless
            ("s3://bucket/", "s3://bucket"),  # keyless: constructor slash dropped
            ("s3://", "s3://"),  # service root
            ("bucket/key", "s3://bucket/key"),  # scheme optional in the constructor
        ],
    )
    def test_as_text(self, url: str, expected: str) -> None:
        assert S3Storage(url).as_text() == expected

    def test_keyless_reconstructs_from_bucket_not_raw_url(self) -> None:
        # url echoes the raw input; as_text rebuilds from bucket/key, so the
        # keyless trailing slash is normalized away (what the format grammar expects).
        storage = S3Storage("s3://bucket/")
        assert storage.uri == "s3://bucket/"
        assert storage.as_text() == "s3://bucket"


class TestLocalStorageAsText:
    @pytest.mark.parametrize("path", ["./x", "dir/", "/abs/p", "rel/p", "."])
    def test_as_text_is_the_path_verbatim(self, path: str) -> None:
        assert LocalStorage(path).as_text() == path


class TestStreamAsText:
    def test_iostorage_is_the_stdio_token(self) -> None:
        assert IOStorage(io.BytesIO()).as_text() == "-"

    def test_stdio_storage_inherits_the_token(self) -> None:
        assert StdioStorage().as_text() == "-"


class TestStrDelegatesToAsText:
    @pytest.mark.parametrize(
        "storage",
        [
            S3Storage("s3://bucket/key"),
            S3Storage("s3://bucket/"),
            S3Storage("s3://"),
            LocalStorage("dir/file"),
            IOStorage(io.BytesIO()),
            StdioStorage(),
        ],
    )
    def test_str_equals_as_text(self, storage: S3Storage | LocalStorage | IOStorage) -> None:
        assert str(storage) == storage.as_text()


class TestAsTextIsResolveInverse:
    """``S3.resolve(s.as_text())`` round-trips a locatable Storage."""

    @pytest.mark.parametrize(
        "url",
        ["s3://bucket/key", "s3://bucket/some/prefix/", "s3://bucket", "s3://bucket/", "s3://"],
    )
    def test_s3_round_trips_bucket_and_key(self, url: str) -> None:
        original = S3Storage(url)
        restored = S3().resolve(original.as_text())
        assert isinstance(restored, S3Storage)
        assert (restored.bucket, restored.key) == (original.bucket, original.key)

    @pytest.mark.parametrize("path", ["./x", "dir/", "/abs/p", "rel/p", "."])
    def test_local_round_trips_path(self, path: str) -> None:
        original = LocalStorage(path)
        restored = S3().resolve(original.as_text())
        assert isinstance(restored, LocalStorage)
        assert restored.path == original.path
