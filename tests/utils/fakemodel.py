"""A stand-in S3 client exposing only the service-model surface the
SDK-capability probes read, with no API calls.

`conditional_write_unsupported_reason` / `annotations_copy_unsupported_reason`
(and the CLI/library gates built on them) decide support by introspecting
``client.meta.service_model.operation_model(op).input_shape.members`` for a
member (``IfNoneMatch`` / ``AnnotationDirective``). A real client always
carries them on the current botocore, so to exercise the old-botocore path
these fakes report the probed member only for the named ops.
"""

from __future__ import annotations

from typing import Any


class _Shape:
    def __init__(self, members: set[str]) -> None:
        self.members = {name: object() for name in members}


class _OperationModel:
    def __init__(self, members: set[str]) -> None:
        self.input_shape = _Shape(members)


class _ServiceModel:
    def __init__(self, ops_with_member: set[str], member: str) -> None:
        self._ops = ops_with_member
        self._member = member

    def operation_model(self, name: str) -> _OperationModel:
        return _OperationModel({self._member} if name in self._ops else set())


class _Meta:
    def __init__(self, ops_with_member: set[str], member: str) -> None:
        self.service_model = _ServiceModel(ops_with_member, member)


class _ModelOnlyClient:
    def __init__(self, ops_with_member: set[str], member: str) -> None:
        self.meta = _Meta(ops_with_member, member)


def model_only_client(ops_with_member: set[str], member: str = "IfNoneMatch") -> Any:
    """A client whose S3 model carries *member* only on the named ops
    (e.g. ``{"PutObject"}``); an empty set models a botocore predating the
    probed feature."""
    return _ModelOnlyClient(ops_with_member, member)
