"""The mtime a download stamps: whole seconds, the way aws-cli stamps them.

aws-cli passes its source timestamp through `timetuple()` / `time.mktime()`,
which keeps only whole seconds. Against an endpoint that reports a sub-second
`LastModified` (MinIO and other S3-compatible servers; real S3 lists whole
seconds) the difference is user-visible: a stamp that kept the fraction matches
the object exactly, so `--exact-timestamps` converges here and re-downloads on
aws forever.

Oracle for the expected stamp: probes/synctime/p03-stamp-then-exact-timestamps.sh
against MinIO, where a `LastModified` of 2026-08-12T16:00:15.454000+00:00 left
aws 2.36.1 with `stat -c %.9Y` = 1786550415.000000000. The epoch second is
`date -u -d 2026-08-12T16:00:15Z +%s`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from boto3.s3.transfer import TransferConfig

from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.types import TransferType
from tests.utils.fakes3 import get_response, head_response
from tests.utils.recorder import make_recording_client, ops

_SYNC = TransferConfig(use_threads=False)

_SUB_SECOND = datetime(2026, 8, 12, 16, 0, 15, 454000, tzinfo=timezone.utc)
_WHOLE_SECOND = 1786550415.0


class TestDownloadStampsWholeSeconds:
    def test_single_download(self, tmp_path: Path) -> None:
        client, calls = make_recording_client(
            [head_response(LastModified=_SUB_SECOND), get_response()]
        )
        dest = tmp_path / "out.bin"
        S3().cp(S3Storage("s3://b/d/a.txt", client=client), str(dest), transfer_config=_SYNC)
        assert ops(calls) == ["HeadObject", "GetObject"]
        assert os.stat(dest).st_mtime == _WHOLE_SECOND

    def test_recursive_download(self, tmp_path: Path) -> None:
        # The listing route carries its own mtime (the object's LastModified as
        # ListObjectsV2 reported it), so it needs its own pin.
        listing = {
            "Contents": [
                {"Key": "pre/a.txt", "Size": 7, "LastModified": _SUB_SECOND, "ETag": '"abc"'}
            ]
        }
        client, calls = make_recording_client([listing, get_response()])
        S3().cp(
            S3Storage("s3://b/pre", client=client),
            str(tmp_path / "out"),
            recursive=True,
            transfer_config=_SYNC,
        )
        assert ops(calls) == ["ListObjectsV2", "GetObject"]
        assert os.stat(tmp_path / "out" / "a.txt").st_mtime == _WHOLE_SECOND

    def test_a_pre_epoch_timestamp_loses_its_fraction_downwards(self, tmp_path: Path) -> None:
        # aws-cli drops the sub-second *field* rather than rounding the epoch
        # value towards zero, so a negative timestamp lands on the second below,
        # not the one above.
        dest = tmp_path / "old.bin"
        dest.write_bytes(b"x")
        client, _ = make_recording_client([])
        transferrer = Transferrer(TransferType.DOWNLOAD, client)
        transferrer._stamp_mtime(
            TransferItem(
                compare_key="old.bin",
                dest_path=str(dest),
                mtime=datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=timezone.utc),
            )
        )
        assert os.stat(dest).st_mtime == -1.0
