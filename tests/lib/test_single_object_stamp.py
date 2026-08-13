"""The single-object HEAD route's timestamp check (`producers.head_single`).

aws-cli converts every timestamp an S3 response carries to the local zone as it
reads the response, and for a single source it does so in `_list_single_object`
- a slot the listing routes never pass through. A stamp the local calendar
cannot hold therefore ends a single-object `cp` / `mv` exactly as it ends a
recursive one, before any bytes move. Measured against the pinned aws through a
127.0.0.1 fake serving a year-9999 `Last-Modified` under `TZ=Asia/Tokyo`:
`fatal error: date value out of range` at rc 1 for `cp s3://bkt/k ./f`, for its
`--dryrun`, and for `mv --dryrun s3://bkt/k s3://bkt2/k`, while
`rm s3://bkt/k` stays rc 0 with its `delete:` line - a single blind delete
sends no HeadObject at all, so it has no stamp to judge.

Which stamps qualify depends on the zone, so each case pins one; the zone
helper is the S3 backend suite's (same conversion, same POSIX-only skip).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3 import S3, OpOutcome, OpResult, S3Storage
from tests.lib.test_s3storage import _local_zone, _needs_tzset
from tests.utils.fakes3 import get_response, head_response
from tests.utils.recorder import make_recording_client, ops

if TYPE_CHECKING:
    from pathlib import Path

_SYNC = TransferConfig(use_threads=False)
_FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


@_needs_tzset
class TestSingleObjectStampRepresentability:
    def test_a_single_download_is_rejected_before_the_get(self, tmp_path: Path) -> None:
        client, calls = make_recording_client(
            [head_response(LastModified=_FAR_FUTURE), get_response()]
        )
        dest = tmp_path / "out.bin"
        with _local_zone("Asia/Tokyo"):
            with pytest.raises(OverflowError, match="date value out of range"):
                S3().cp(
                    S3Storage("s3://b/d/a.txt", client=client), str(dest), transfer_config=_SYNC
                )
        # The HEAD is where aws dies: the object body is never asked for, and
        # nothing is written where the download would have landed.
        assert ops(calls) == ["HeadObject"]
        assert not dest.exists()

    def test_a_dryrun_is_rejected_too(self, tmp_path: Path) -> None:
        # aws reads the stamp while resolving the source, which a dryrun does
        # in full - so the run dies before the record it would have printed.
        client, _ = make_recording_client([head_response(LastModified=_FAR_FUTURE)])
        results: list[OpResult] = []
        with _local_zone("Asia/Tokyo"):
            with pytest.raises(OverflowError, match="date value out of range"):
                S3().cp(
                    S3Storage("s3://b/d/a.txt", client=client),
                    str(tmp_path / "out.bin"),
                    dryrun=True,
                    on_result=results.append,
                )
        assert results == []

    def test_a_single_copy_source_is_rejected(self) -> None:
        # The copy route heads its source through the same producer (with the
        # copy-source parameters), so `mv --dryrun s3://... s3://...` dies here
        # as well - measured on aws.
        client, calls = make_recording_client([head_response(LastModified=_FAR_FUTURE)])
        with _local_zone("Asia/Tokyo"):
            with pytest.raises(OverflowError, match="date value out of range"):
                S3().mv(
                    S3Storage("s3://b/d/a.txt", client=client),
                    S3Storage("s3://b2/d/a.txt", client=client),
                    dryrun=True,
                    transfer_config=_SYNC,
                )
        assert ops(calls) == ["HeadObject"]

    def test_the_same_stamp_transfers_where_the_zone_can_hold_it(self, tmp_path: Path) -> None:
        # Nothing is rejected on its face: the conversion is what fails, so a
        # zone that can hold the stamp downloads it - and the value kept on the
        # entry stays UTC per the `FileInfo.mtime` contract.
        client, calls = make_recording_client(
            [head_response(LastModified=_FAR_FUTURE), get_response()]
        )
        dest = tmp_path / "out.bin"
        results: list[OpResult] = []
        with _local_zone("UTC"):
            S3().cp(
                S3Storage("s3://b/d/a.txt", client=client),
                str(dest),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert ops(calls) == ["HeadObject", "GetObject"]
        assert dest.read_bytes() == b"payload"
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]
        assert results[0].src_info is not None
        assert results[0].src_info.mtime == _FAR_FUTURE

    def test_a_single_delete_never_asks_for_the_stamp(self) -> None:
        # aws's `_list_single_object` returns `{'Size': None, 'LastModified':
        # None}` for a delete without contacting S3, so the blind single delete
        # has no stamp to reject: it succeeds against an object whose
        # LastModified would have killed every other command.
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        with _local_zone("Asia/Tokyo"):
            S3().rm(S3Storage("s3://b/d/a.txt", client=client), on_result=results.append)
        assert ops(calls) == ["DeleteObject"]
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]
