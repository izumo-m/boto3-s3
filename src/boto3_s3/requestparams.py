"""``TransferOptions`` -> S3 API request parameters (``RequestParamsMapper`` port).

A pure-function port of aws-cli's ``RequestParamsMapper``
(aws-cli's awscli/customizations/s3/utils.py): one ``map_*`` function per
S3 operation the transfer path performs, each returning a fresh dict of
PascalCase API parameters built from the snake_case ``TransferOptions``.
Falsy values are omitted exactly like aws-cli's truthiness gates - including
their quirks: a truthy SSE-C algorithm carries its dependent key / MD5
values verbatim even when falsy, as aws does.

Only the operations the transfer path actually calls are ported
(put/get/copy/head/tagging/delete, plus the annotation pair -
list/get object annotations - that ``copy_props=ALL`` staging reads);
aws-cli's CreateMultipartUpload /
UploadPart variants are ``s3transfer``'s internal concern - it splits the
submit-time extra args itself.

The grants shape is validated here with aws-cli's exact wording, but the
*call site* is the transfer submit path: aws-cli maps params per item inside
its pipeline, so a bad ``--grants`` surfaces as an in-flight fatal error
(rc 1), never as a usage error - keep mapping lazy to preserve that.
"""

from __future__ import annotations

from typing import Any

from boto3_s3.exceptions import ValidationError
from boto3_s3.types import TransferOptions

_GENERAL_PARAM_TRANSLATION = {
    "acl": "ACL",
    "storage_class": "StorageClass",
    "website_redirect": "WebsiteRedirectLocation",
    "content_type": "ContentType",
    "cache_control": "CacheControl",
    "content_disposition": "ContentDisposition",
    "content_encoding": "ContentEncoding",
    "content_language": "ContentLanguage",
    "expires": "Expires",
}

_PERMISSION_TO_PARAM = {
    "read": "GrantRead",
    "full": "GrantFullControl",
    "readacl": "GrantReadACP",
    "writeacl": "GrantWriteACP",
}


def map_put_object_params(options: TransferOptions, operation: str = "cp") -> dict[str, Any]:
    """API params for an upload (PutObject and its multipart equivalent).

    *operation* names the originating subcommand (``cp`` / ``mv`` / ``sync``) so a
    bad ``--grants`` value reports the operation actually in flight.
    """
    params: dict[str, Any] = {}
    _set_general_object_params(params, options, operation)
    _set_metadata_params(params, options)
    _set_sse_request_params(params, options)
    _set_sse_c_request_params(params, options)
    _set_request_payer_param(params, options)
    _set_checksum_algorithm_param(params, options)
    _set_no_overwrite_param(params, options)
    return params


def map_get_object_params(options: TransferOptions) -> dict[str, Any]:
    """API params for a download (GetObject)."""
    params: dict[str, Any] = {}
    _set_sse_c_request_params(params, options)
    _set_request_payer_param(params, options)
    _set_checksum_mode_param(params, options)
    return params


def map_copy_object_params(options: TransferOptions, operation: str = "cp") -> dict[str, Any]:
    """API params for an S3-to-S3 copy (CopyObject and its multipart equivalent).

    *operation* names the originating subcommand (``cp`` / ``mv`` / ``sync``) so a
    bad ``--grants`` value reports the operation actually in flight.
    """
    params: dict[str, Any] = {}
    _set_general_object_params(params, options, operation)
    _set_metadata_directive_param(params, options)
    _set_metadata_params(params, options)
    _auto_populate_metadata_directive(params)
    _set_sse_request_params(params, options)
    _set_sse_c_request_params(params, options)
    _set_sse_c_copy_source_request_params(params, options)
    _set_request_payer_param(params, options)
    _set_checksum_algorithm_param(params, options)
    _set_no_overwrite_param(params, options)
    return params


def map_head_object_params(options: TransferOptions) -> dict[str, Any]:
    """API params for the single-source HeadObject (upload/download side)."""
    params: dict[str, Any] = {}
    _set_sse_c_request_params(params, options)
    _set_request_payer_param(params, options)
    _set_checksum_mode_param(params, options)
    return params


def map_head_object_params_with_copy_source_sse(options: TransferOptions) -> dict[str, Any]:
    """HeadObject params for a copy *source* - its SSE-C headers come from
    ``sse_c_copy_source``, since the head reads the object being copied."""
    params: dict[str, Any] = {}
    algorithm = options.get("sse_c_copy_source")
    if algorithm:
        params["SSECustomerAlgorithm"] = algorithm
        params["SSECustomerKey"] = options.get("sse_c_copy_source_key")
    _set_request_payer_param(params, options)
    return params


def map_get_object_tagging_params(options: TransferOptions) -> dict[str, Any]:
    """API params for the copy-props tag read."""
    params: dict[str, Any] = {}
    _set_request_payer_param(params, options)
    return params


def map_put_object_tagging_params(options: TransferOptions) -> dict[str, Any]:
    """API params for the post-copy tag write (oversized tag sets)."""
    params: dict[str, Any] = {}
    _set_request_payer_param(params, options)
    return params


def map_list_object_annotations_params(options: TransferOptions) -> dict[str, Any]:
    """API params for the pre-copy annotation listing."""
    params: dict[str, Any] = {}
    _set_request_payer_param(params, options)
    return params


def map_get_object_annotation_params(options: TransferOptions) -> dict[str, Any]:
    """API params for a pre-copy annotation payload read."""
    params: dict[str, Any] = {}
    _set_request_payer_param(params, options)
    return params


def map_delete_object_params(options: TransferOptions) -> dict[str, Any]:
    """API params for the copy-props rollback delete (and mv's source delete)."""
    params: dict[str, Any] = {}
    _set_request_payer_param(params, options)
    return params


def _set_general_object_params(
    params: dict[str, Any], options: TransferOptions, operation: str
) -> None:
    opts: Any = options  # TypedDict.get with a variable key needs a loose view
    for option_name, param_name in _GENERAL_PARAM_TRANSLATION.items():
        value = opts.get(option_name)
        if value:
            params[param_name] = value
    _set_grant_params(params, options, operation)


def _set_grant_params(params: dict[str, Any], options: TransferOptions, operation: str) -> None:
    # aws-cli gates on truthiness (`if cli_params.get('grants'):`), so a None /
    # empty value is a no-op; `.get("grants", ())` would default only on a
    # missing key and then iterate an explicit None, so coalesce with `or ()`.
    for grant in options.get("grants") or ():
        try:
            permission, grantee = grant.split("=", 1)
        except ValueError:
            raise ValidationError(
                "grants should be of the form permission=principal", operation=operation
            ) from None
        params[_permission_to_param(permission, operation)] = grantee


def _permission_to_param(permission: str, operation: str) -> str:
    param = _PERMISSION_TO_PARAM.get(permission)
    if param is None:
        raise ValidationError(
            "permission must be one of: read|readacl|writeacl|full", operation=operation
        )
    return param


def _set_metadata_params(params: dict[str, Any], options: TransferOptions) -> None:
    metadata = options.get("metadata")
    if metadata:
        params["Metadata"] = dict(metadata)


def _set_metadata_directive_param(params: dict[str, Any], options: TransferOptions) -> None:
    directive = options.get("metadata_directive")
    if directive:
        params["MetadataDirective"] = directive


def _auto_populate_metadata_directive(params: dict[str, Any]) -> None:
    # Replacing the metadata without saying how to treat the rest implies
    # REPLACE (aws-cli's _auto_populate_metadata_directive).
    if params.get("Metadata") and not params.get("MetadataDirective"):
        params["MetadataDirective"] = "REPLACE"


def _set_sse_request_params(params: dict[str, Any], options: TransferOptions) -> None:
    sse = options.get("sse")
    if sse:
        params["ServerSideEncryption"] = sse
    kms_key_id = options.get("sse_kms_key_id")
    if kms_key_id:
        params["SSEKMSKeyId"] = kms_key_id


def _set_sse_c_request_params(params: dict[str, Any], options: TransferOptions) -> None:
    algorithm = options.get("sse_c")
    if algorithm:
        params["SSECustomerAlgorithm"] = algorithm
        params["SSECustomerKey"] = options.get("sse_c_key")


def _set_sse_c_copy_source_request_params(params: dict[str, Any], options: TransferOptions) -> None:
    algorithm = options.get("sse_c_copy_source")
    if algorithm:
        params["CopySourceSSECustomerAlgorithm"] = algorithm
        params["CopySourceSSECustomerKey"] = options.get("sse_c_copy_source_key")


def _set_request_payer_param(params: dict[str, Any], options: TransferOptions) -> None:
    request_payer = options.get("request_payer")
    if request_payer:
        params["RequestPayer"] = request_payer


def _set_checksum_mode_param(params: dict[str, Any], options: TransferOptions) -> None:
    checksum_mode = options.get("checksum_mode")
    if checksum_mode:
        params["ChecksumMode"] = checksum_mode


def _set_checksum_algorithm_param(params: dict[str, Any], options: TransferOptions) -> None:
    checksum_algorithm = options.get("checksum_algorithm")
    if checksum_algorithm:
        params["ChecksumAlgorithm"] = checksum_algorithm


def _set_no_overwrite_param(params: dict[str, Any], options: TransferOptions) -> None:
    # The conditional-write form S3 supports: write only when no object
    # exists under the key (aws-cli's _set_no_overwrite_param).
    if options.get("no_overwrite"):
        params["IfNoneMatch"] = "*"


# Package-internal: the mappers are consumed by producers/transfer only and
# carry no documented surface (docs/imports.md).
__all__: list[str] = []
