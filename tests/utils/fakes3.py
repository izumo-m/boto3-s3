"""Canned S3 response/error builders shared by the fake-client test tiers.

Companions to `tests.utils.recorder`: these values feed
`make_recording_client` scripts (or hand-rolled fake clients), so datetime
fields are real `datetime` objects (see the recorder's module docstring for
why).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

# One fixed LastModified for canned listings and heads. Tests only ever rely
# on relations between timestamps (equal / shifted by a timedelta / older than
# a file written during the test), never on this absolute value.
MTIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def client_error(
    code: str, status: int, operation: str = "Operation", *, message: str = "stub"
) -> ClientError:
    """A `ClientError` as botocore raises it: error code and message plus the
    HTTP status, attributed to `operation`. `message` only matters to the few
    tests that assert the full printed error text."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


def head_response(**extra: Any) -> dict[str, Any]:
    """A minimal HeadObject response for a 7-byte object; `extra` overlays it."""
    return {"ContentLength": 7, "LastModified": MTIME, "ETag": '"abc"', **extra}


def get_response(body: bytes = b"payload") -> dict[str, Any]:
    """A minimal GetObject response whose streaming body yields `body`."""
    return {"Body": io.BytesIO(body), "ContentLength": len(body), "ETag": '"abc"'}


def listing(*entries: tuple[str, int]) -> dict[str, Any]:
    """A ListObjectsV2 page of `(key, size)` objects, all stamped `MTIME`."""
    return {
        "Contents": [
            {"Key": key, "Size": size, "LastModified": MTIME, "ETag": '"e"'}
            for key, size in entries
        ]
    }
