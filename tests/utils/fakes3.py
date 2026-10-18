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


class RecordingCrtSender:
    """Stands in for `crtrequest.CrtRequestSender`: each request goes to the
    test client's method of the same name - so a fake client's scripts, or a
    recording client's canned responses, answer it - and is recorded."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        from botocore import xform_name

        self.calls.append((operation, dict(params)))
        return getattr(self.client, xform_name(operation))(**params)


def crt_sender_factory(
    built: list[tuple[RecordingCrtSender, Any, dict[str, Any]]],
) -> Any:
    """A stand-in for `deleter.crt_delete_sender`: a `RecordingCrtSender` over
    the client when the config names ``'crt'``, else ``None`` (botocore), with
    each build recorded as (sender, config, keyword arguments)."""

    def build(client: Any, transfer_config: Any, **kwargs: Any) -> RecordingCrtSender | None:
        if getattr(transfer_config, "preferred_transfer_client", None) != "crt":
            return None
        sender = RecordingCrtSender(client)
        built.append((sender, transfer_config, kwargs))
        return sender

    return build
