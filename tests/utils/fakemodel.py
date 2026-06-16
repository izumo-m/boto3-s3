"""A stand-in S3 client exposing only the service-model surface the
conditional-write probe reads, with no API calls.

``conditional_write_unsupported_reason`` (and the CLI/library --no-overwrite
gates built on it) decide support by introspecting
``client.meta.service_model.operation_model(op).input_shape.members`` for
``IfNoneMatch``. A real client always carries it on the current botocore, so to
exercise the old-botocore path these fakes report it only for the named write
ops.
"""

from __future__ import annotations

from typing import Any


class _Shape:
    def __init__(self, has_if_none_match: bool) -> None:
        self.members = {"IfNoneMatch": object()} if has_if_none_match else {}


class _OperationModel:
    def __init__(self, has_if_none_match: bool) -> None:
        self.input_shape = _Shape(has_if_none_match)


class _ServiceModel:
    def __init__(self, ops_with_if_none_match: set[str]) -> None:
        self._ops = ops_with_if_none_match

    def operation_model(self, name: str) -> _OperationModel:
        return _OperationModel(name in self._ops)


class _Meta:
    def __init__(self, ops_with_if_none_match: set[str]) -> None:
        self.service_model = _ServiceModel(ops_with_if_none_match)


class _ModelOnlyClient:
    def __init__(self, ops_with_if_none_match: set[str]) -> None:
        self.meta = _Meta(ops_with_if_none_match)


def model_only_client(ops_with_if_none_match: set[str]) -> Any:
    """A client whose S3 model carries ``IfNoneMatch`` only on the named write
    ops (e.g. ``{"PutObject"}``); an empty set models a botocore predating S3
    conditional writes."""
    return _ModelOnlyClient(ops_with_if_none_match)
