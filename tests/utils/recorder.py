"""Canned-response S3 client with call recording, for the aws-cli test ports.

The moral equivalent of aws-cli's functional-test ``parsed_responses`` /
``operations_called`` pair (aws-cli's ``tests/functional/s3/__init__.py``),
shrunk to what the ports need: a **real** boto3 client whose
``_make_api_call`` is replaced per instance, so the genuine argument-mapping
paths run (paginators translate ``PageSize`` into ``MaxKeys`` / ``MaxBuckets``,
etc.) while every call is recorded and answered from a canned list.

Differences from aws-cli's HTTP-level stubbing, relevant when porting tests:

- Responses bypass botocore's output parser, so datetime fields
  (``LastModified``, ``CreationDate``) must be ``datetime`` objects, not the
  ISO strings the aws-cli tests use.
- Calls are recorded *before* botocore's ``before-parameter-build`` handlers
  run, so parameters botocore injects on the wire - notably the automatic
  ``EncodingType: "url"`` for ``ListObjects*`` (botocore
  ``handlers.set_list_objects_encoding_type_url``, decoded symmetrically on
  the response path) - do not appear in the recorded params even though they
  are sent by both aws-cli and boto3-s3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

import boto3

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

# Exhaustion events, drained after every test by the root conftest's
# ``_fail_on_recorder_exhaustion`` fixture. The AssertionError raised at the
# call site is enough on a direct call path, but on the transfer path it is
# raised on an s3transfer worker thread where ``translate_boto_error`` folds
# it into an ordinary FAILED item - a failure-expecting test would then pass
# for the wrong reason. The fixture makes an over-called recorder loud
# regardless of where the AssertionError ended up.
exhausted_calls: list[str] = []


class ApiCall(NamedTuple):
    """One recorded S3 API call (botocore operation name + caller params)."""

    operation: str
    params: dict[str, Any]


def make_recording_client(
    parsed_responses: list[dict[str, Any] | Exception],
    *,
    region: str = "us-east-1",
    service: str = "s3",
) -> tuple[S3Client, list[ApiCall]]:
    """Build a client for *service* replaying *parsed_responses* in call order.

    Leftover responses are allowed (mirroring the aws-cli harness); a call
    beyond the last canned response fails the test. A canned entry that is an
    ``Exception`` is raised instead of returned - the aws-cli harness's
    ``http_response.status_code = 500`` cases port as a canned ``ClientError``.
    *region* feeds ``client.meta.region_name`` for commands that read it
    (``mb``'s LocationConstraint); *service* serves the non-S3 clients mv's
    path validation builds (``s3control`` / ``sts``) - the return type is
    nominally ``S3Client`` either way, deliberately (every consumer treats it
    as ``Any``). Credentials come from the fake environment installed by the
    root conftest's ``_moto_isolation`` fixture, so the client never touches
    the network before ``_make_api_call`` is intercepted. A fresh ``Session``
    (not the process-global default) keeps the fake credentials from being
    cached and leaking into the e2e suite's real client later in the same
    run.
    """
    client: Any = boto3.session.Session().client(service, region_name=region)
    calls: list[ApiCall] = []
    remaining = list(parsed_responses)

    def _canned_api_call(operation_name: str, api_params: dict[str, Any]) -> dict[str, Any]:
        calls.append(ApiCall(operation_name, dict(api_params)))
        if not remaining:
            exhausted_calls.append(f"{service}.{operation_name}")
            raise AssertionError(
                f"unexpected API call {operation_name}: parsed_responses exhausted"
            )
        response = remaining.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client._make_api_call = _canned_api_call
    return client, calls


def ops(calls: list[ApiCall]) -> list[str]:
    """Just the operation names, in call order - what most tests pin."""
    return [call.operation for call in calls]
