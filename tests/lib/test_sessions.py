"""`boto3_s3.sessions`: the fast timestamp parser and the tuned Session factory.

The parser contract is value-equality with botocore's `parse_timestamp` for
every input both accept (only the tzinfo class may differ); the factory
contract is plain-`boto3.Session` semantics plus the parser default, with the
process default session never consulted or touched.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.utils import parse_timestamp

import boto3_s3
from boto3_s3.sessions import fast_parse_timestamp, session

# ISO 8601 forms both parsers accept: S3's wire form first, then the broader
# shapes fromisoformat handles (which must stay value-equal to dateutil's).
_ISO_FORMS = [
    "2100-01-01T00:00:00.000Z",
    "2026-07-14T11:57:42.000Z",
    "2026-07-14T11:57:42.123456Z",
    "2026-07-14T11:57:42Z",
    "2026-07-14T11:57:42+00:00",
    "2026-07-14T11:57:42+02:00",
    "2026-07-14T11:57:42-07:30",
    "2026-07-14T11:57:42",
    "2026-07-14 11:57:42",
    "2026-07-14",
]

# Non-ISO forms the fast path must hand to botocore untouched.
_FALLBACK_FORMS: list[Any] = [
    "Wed, 12 Oct 2009 17:50:00 GMT",
    "2026-07-14T11:57:42z",  # lowercase suffix: S3 never sends it; delegated
    1234567890,
    1234567890.5,
    "1234567890",
]


class TestFastParseTimestamp:
    @pytest.mark.parametrize("value", _ISO_FORMS)
    def test_iso_forms_match_botocore_values(self, value: str) -> None:
        ours = fast_parse_timestamp(value)
        botocores = parse_timestamp(value)
        assert ours == botocores
        assert (ours.tzinfo is None) == (botocores.tzinfo is None)

    def test_s3_wire_form_takes_the_c_path(self) -> None:
        # The tzinfo class is the observable difference between the two paths:
        # fromisoformat stamps datetime.timezone, dateutil its own tzutc.
        parsed = fast_parse_timestamp("2026-07-14T11:57:42.000Z")
        assert parsed.tzinfo == timezone.utc
        assert isinstance(parsed.tzinfo, timezone)

    def test_offsets_convert_like_botocore(self) -> None:
        parsed = fast_parse_timestamp("2026-07-14T11:57:42+02:00")
        assert parsed.utcoffset() == timedelta(hours=2)
        assert parsed == parse_timestamp("2026-07-14T11:57:42+02:00")

    @pytest.mark.parametrize("value", _FALLBACK_FORMS)
    def test_non_iso_forms_delegate_to_botocore(self, value: Any) -> None:
        assert fast_parse_timestamp(value) == parse_timestamp(value)

    def test_digit_only_strings_stay_on_botocore_epoch_semantics(self) -> None:
        # "20200101" is epoch seconds to botocore, but Python 3.11+'s
        # fromisoformat would read it as the basic-format date 2020-01-01;
        # the fast path's requires-a-dash guard keeps such inputs on
        # botocore's interpretation on every Python version.
        assert fast_parse_timestamp("20200101") == parse_timestamp("20200101")

    def test_invalid_input_raises_like_botocore(self) -> None:
        with pytest.raises(ValueError):
            parse_timestamp("not a timestamp")
        with pytest.raises(ValueError):
            fast_parse_timestamp("not a timestamp")


_LIST_BODY = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Name>b</Name><Prefix></Prefix><KeyCount>1</KeyCount>"
    b"<MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>"
    b"<Contents><Key>k</Key>"
    b"<LastModified>2026-07-14T11:57:42.000Z</LastModified>"
    b'<ETag>"e"</ETag><Size>1</Size>'
    b"<StorageClass>STANDARD</StorageClass></Contents>"
    b"</ListBucketResult>"
)


class _RawBody(io.BytesIO):
    def stream(self, **_kwargs: Any) -> Any:
        data = self.read()
        while data:
            yield data
            data = self.read()


def _stubbed_client(sess: boto3.session.Session) -> Any:
    """An S3 client answering ListObjectsV2 off the wire (before-send)."""
    client = sess.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="http://bench-stub.invalid:9",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

    def respond(request: Any, **_kwargs: Any) -> AWSResponse:
        headers = {"Content-Length": str(len(_LIST_BODY)), "Content-Type": "application/xml"}
        return AWSResponse(
            url=request.url, status_code=200, headers=headers, raw=_RawBody(_LIST_BODY)
        )

    client.meta.events.register("before-send.s3", respond)
    return client


class TestSessionFactory:
    def test_clients_parse_listing_timestamps_via_the_fast_path(self) -> None:
        client = _stubbed_client(session())
        parsed = client.list_objects_v2(Bucket="b")
        mtime = parsed["Contents"][0]["LastModified"]
        assert mtime == datetime(2026, 7, 14, 11, 57, 42, tzinfo=timezone.utc)
        # timezone.utc proves fast_parse_timestamp ran: botocore's own parser
        # returns dateutil's tzutc here.
        assert isinstance(mtime.tzinfo, timezone)

    def test_a_plain_boto3_session_is_untouched(self) -> None:
        parsed = _stubbed_client(boto3.session.Session()).list_objects_v2(Bucket="b")
        mtime = parsed["Contents"][0]["LastModified"]
        assert mtime == datetime(2026, 7, 14, 11, 57, 42, tzinfo=timezone.utc)
        assert not isinstance(mtime.tzinfo, timezone)

    def test_kwargs_forward_to_boto3_session(self) -> None:
        tuned = session(region_name="eu-west-1")
        assert tuned.region_name == "eu-west-1"

    def test_default_session_is_not_touched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
        session()
        assert boto3.DEFAULT_SESSION is None

    def test_root_exports_resolve(self) -> None:
        assert boto3_s3.session is session
        assert boto3_s3.fast_parse_timestamp is fast_parse_timestamp
