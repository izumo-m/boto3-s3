"""A stand-in S3 client exposing only the service-model surface the
SDK-capability probes read, with no API calls.

`conditional_write_unsupported_reason` / `annotations_copy_unsupported_reason`
(with the library gates built on those two) and the checksum-algorithm gate
decide support by introspecting
``client.meta.service_model.operation_model(op).input_shape.members`` for a
member (``IfNoneMatch`` / ``AnnotationDirective`` / ``Checksum<ALG>``). A real
client always carries them on the current botocore, so to exercise the
old-botocore path these fakes report only the members they are told to.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any


class _Shape:
    def __init__(self, members: Collection[str]) -> None:
        self.members: dict[str, Any] = {name: object() for name in members}


class _OperationModel:
    def __init__(self, members: Collection[str]) -> None:
        self.input_shape = _Shape(members)


class _ServiceModel:
    def __init__(self, members_by_op: Mapping[str, Collection[str]]) -> None:
        self._members_by_op = members_by_op

    def operation_model(self, name: str) -> _OperationModel:
        return _OperationModel(self._members_by_op.get(name, set()))


class _Meta:
    def __init__(self, members_by_op: Mapping[str, Collection[str]]) -> None:
        self.service_model = _ServiceModel(members_by_op)


class _ModelOnlyClient:
    def __init__(self, members_by_op: Mapping[str, Collection[str]]) -> None:
        self.meta = _Meta(members_by_op)


def model_client(members_by_op: Mapping[str, Collection[str]]) -> Any:
    """A client whose S3 model carries exactly the named input members per op
    (e.g. ``{"PutObject": {"ChecksumCRC32"}}``; an unnamed op has no members),
    with no API calls."""
    return _ModelOnlyClient(members_by_op)


def model_only_client(ops_with_member: set[str], member: str = "IfNoneMatch") -> Any:
    """A client whose S3 model carries *member* only on the named ops
    (e.g. ``{"PutObject"}``); an empty set models a botocore predating the
    probed feature."""
    return _ModelOnlyClient({op: {member} for op in ops_with_member})
