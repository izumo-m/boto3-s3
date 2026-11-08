"""The single-object HEAD route (`producers.head_single`): what its request
raises, and the timestamp's reading.

aws-cli converts every timestamp an S3 response carries to the local zone as it
reads the response, and for a single source it does so in `_list_single_object`
- a slot the listing routes never pass through. A stamp the local calendar
cannot hold therefore ends a single-object `cp` / `mv` there, before any bytes
move (measured against the pinned `aws` through a 127.0.0.1 fake serving a
year-9999 `Last-Modified` under `TZ=Asia/Tokyo`: rc 1 for `cp s3://bkt/k ./f`,
for its `--dryrun`, and for `mv --dryrun s3://bkt/k s3://bkt2/k`). Here the
UTC value is kept and the run goes on (docs/cli/aws-differences.md).

The zone helper is the S3 backend suite's (same POSIX-only skip).
"""

from __future__ import annotations

import errno
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3 import S3, Boto3S3Error, OpOutcome, OpResult, S3Storage
from tests.lib.test_s3storage import _local_zone, _needs_tzset
from tests.utils.fakes3 import get_response, head_response
from tests.utils.recorder import make_recording_client, ops

if TYPE_CHECKING:
    from pathlib import Path

_SYNC = TransferConfig(use_threads=False)
_FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


class TestSingleObjectHeadRaisingOutsideTheBotoFamily:
    """What the HEAD raises from inside botocore is the request failing.

    A refreshing ``credential_process`` whose program is gone kills the
    single-source HEAD with a ``FileNotFoundError`` from botocore's ``Popen``.
    It is the base ``Boto3S3Error`` carrying it, attributed like every other
    failed HEAD, so ``except Boto3S3Error`` catches it.
    """

    def test_it_is_the_base_error_attributed_to_the_operation(self, tmp_path: Path) -> None:
        boom = FileNotFoundError(errno.ENOENT, "No such file or directory", "/bin/helper")
        client, calls = make_recording_client([boom])
        with pytest.raises(Boto3S3Error) as excinfo:
            S3().cp(
                S3Storage("s3://b/d/a.txt", client=client),
                str(tmp_path / "out.bin"),
                transfer_config=_SYNC,
            )
        error = excinfo.value
        assert type(error) is Boto3S3Error
        assert error.__cause__ is boom
        assert (error.operation, error.bucket, error.key) == ("cp", "b", "d/a.txt")
        assert ops(calls) == ["HeadObject"]


@_needs_tzset
class TestSingleObjectStampRepresentability:
    def test_a_single_download_transfers(self, tmp_path: Path) -> None:
        # aws-cli dies at the HEAD in this zone; the stamp is a valid UTC value,
        # so the download goes on and the entry keeps it as it came.
        client, calls = make_recording_client(
            [head_response(LastModified=_FAR_FUTURE), get_response()]
        )
        dest = tmp_path / "out.bin"
        results: list[OpResult] = []
        with _local_zone("Asia/Tokyo"):
            S3().cp(
                S3Storage("s3://b/d/a.txt", client=client),
                str(dest),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert ops(calls) == ["HeadObject", "GetObject"]
        assert dest.read_bytes() == b"payload"
        assert OpOutcome.SUCCEEDED in [r.outcome for r in results]
        succeeded = [r for r in results if r.outcome is OpOutcome.SUCCEEDED]
        assert succeeded[0].src_info is not None
        assert succeeded[0].src_info.mtime == _FAR_FUTURE

    def test_a_single_copy_source_dry_run_is_recorded(self) -> None:
        client, calls = make_recording_client([head_response(LastModified=_FAR_FUTURE)])
        results: list[OpResult] = []
        with _local_zone("Asia/Tokyo"):
            S3().mv(
                S3Storage("s3://b/d/a.txt", client=client),
                S3Storage("s3://b2/d/a.txt", client=client),
                dryrun=True,
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert ops(calls) == ["HeadObject"]
        assert [r.outcome for r in results] == [OpOutcome.DRYRUN]

    def test_a_single_delete_never_asks_for_the_stamp(self) -> None:
        # aws-cli's `_list_single_object` returns `{'Size': None, 'LastModified':
        # None}` for a delete without contacting S3, so the blind single delete
        # has no stamp to reject: it succeeds against an object whose
        # LastModified would have killed every other command.
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        with _local_zone("Asia/Tokyo"):
            S3().rm(S3Storage("s3://b/d/a.txt", client=client), on_result=results.append)
        assert ops(calls) == ["DeleteObject"]
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]
