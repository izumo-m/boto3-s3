"""A stand-in S3 client exposing only the service-model surface the
SDK-capability probes read, with no API calls, plus the same surface as a
``meta`` a hand-rolled fake client can carry.

`conditional_write_unsupported_reason` / `annotations_copy_unsupported_reason`
(with the CLI and library gates built on those two) and the ``ListBuckets``
filter gate decide support by introspecting
``client.meta.service_model.operation_model(op).input_shape.members`` for a
member (``IfNoneMatch`` / ``AnnotationDirective`` / ``Prefix`` /
``BucketRegion``). A real client always carries them on the current botocore,
so to exercise the old-botocore path these fakes report only the members they
are told to.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any


class _Shape:
    def __init__(self, members: Collection[str]) -> None:
        self.members = {name: object() for name in members}


class _OperationModel:
    def __init__(self, members: Collection[str] | None) -> None:
        # None models a botocore old enough to give the operation no input
        # shape at all, which the probes read as "no members".
        self.input_shape = _Shape(members) if members is not None else None


class _ServiceModel:
    def __init__(self, members_by_op: Mapping[str, Collection[str] | None]) -> None:
        self._members_by_op = members_by_op

    def operation_model(self, name: str) -> _OperationModel:
        return _OperationModel(self._members_by_op.get(name, set()))


class _Meta:
    def __init__(self, members_by_op: Mapping[str, Collection[str] | None]) -> None:
        self.service_model = _ServiceModel(members_by_op)


class _ModelOnlyClient:
    def __init__(self, members_by_op: Mapping[str, Collection[str] | None]) -> None:
        self.meta = _Meta(members_by_op)


def model_meta(members_by_op: Mapping[str, Collection[str] | None]) -> Any:
    """A ``client.meta`` stand-in whose operation models carry exactly the
    named input members (e.g. ``{"ListBuckets": {"Prefix"}}``); an op mapped to
    ``None`` has no input shape, and an unnamed op has no members. For a fake
    client that answers real calls and must also satisfy a capability probe."""
    return _Meta(members_by_op)


def model_only_client(ops_with_member: set[str], member: str = "IfNoneMatch") -> Any:
    """A client whose S3 model carries *member* only on the named ops
    (e.g. ``{"PutObject"}``); an empty set models a botocore predating the
    probed feature."""
    return _ModelOnlyClient({op: {member} for op in ops_with_member})
