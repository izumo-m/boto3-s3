"""``boto3_s3.requestparams``: TransferOptions -> S3 API params (aws-cli parity).

Pins the RequestParamsMapper port: per-operation parameter sets, the
truthiness-gated omissions, the MetadataDirective auto-REPLACE rule, the
copy-source SSE-C variants, and the grants shape errors with aws-cli's exact
wording (both surface as in-pipeline ``fatal error`` rc 1).
"""

from __future__ import annotations

import pytest

from boto3_s3.exceptions import ValidationError
from boto3_s3.requestparams import (
    map_copy_object_params,
    map_delete_object_params,
    map_get_object_annotation_params,
    map_get_object_params,
    map_get_object_tagging_params,
    map_head_object_params,
    map_head_object_params_with_copy_source_sse,
    map_list_object_annotations_params,
    map_put_object_params,
    map_put_object_tagging_params,
)
from boto3_s3.types import TransferOptions


class TestPutObjectParams:
    def test_general_params_translate_to_pascal_case(self) -> None:
        options = TransferOptions(
            acl="public-read",
            storage_class="STANDARD_IA",
            website_redirect="/index.html",
            content_type="text/plain",
            cache_control="max-age=60",
            content_disposition="attachment",
            content_encoding="gzip",
            content_language="en",
            expires="2030-01-01T00:00:00Z",
        )
        assert map_put_object_params(options) == {
            "ACL": "public-read",
            "StorageClass": "STANDARD_IA",
            "WebsiteRedirectLocation": "/index.html",
            "ContentType": "text/plain",
            "CacheControl": "max-age=60",
            "ContentDisposition": "attachment",
            "ContentEncoding": "gzip",
            "ContentLanguage": "en",
            "Expires": "2030-01-01T00:00:00Z",
        }

    def test_empty_options_map_to_no_params(self) -> None:
        assert map_put_object_params(TransferOptions()) == {}

    def test_falsy_values_are_omitted(self) -> None:
        # The aws-cli gates on truthiness, so "" behaves like "not given".
        assert map_put_object_params(TransferOptions(acl="", metadata={})) == {}

    def test_metadata_is_copied(self) -> None:
        metadata = {"k1": "v1"}
        params = map_put_object_params(TransferOptions(metadata=metadata))
        assert params == {"Metadata": {"k1": "v1"}}
        metadata["k2"] = "v2"
        assert params["Metadata"] == {"k1": "v1"}

    def test_sse_and_kms_key(self) -> None:
        options = TransferOptions(sse="aws:kms", sse_kms_key_id="key-id")
        assert map_put_object_params(options) == {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": "key-id",
        }

    def test_sse_c_pair(self) -> None:
        options = TransferOptions(sse_c="AES256", sse_c_key=b"k" * 32)
        assert map_put_object_params(options) == {
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": b"k" * 32,
        }

    def test_request_payer_and_checksum_algorithm(self) -> None:
        options = TransferOptions(request_payer="requester", checksum_algorithm="CRC32")
        assert map_put_object_params(options) == {
            "RequestPayer": "requester",
            "ChecksumAlgorithm": "CRC32",
        }


class TestGetObjectParams:
    def test_only_download_relevant_params_apply(self) -> None:
        options = TransferOptions(
            acl="public-read",  # upload-side option: ignored on GetObject
            sse_c="AES256",
            sse_c_key=b"k",
            request_payer="requester",
            checksum_mode="ENABLED",
        )
        assert map_get_object_params(options) == {
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": b"k",
            "RequestPayer": "requester",
            "ChecksumMode": "ENABLED",
        }


class TestCopyObjectParams:
    def test_metadata_alone_auto_populates_replace(self) -> None:
        params = map_copy_object_params(TransferOptions(metadata={"a": "b"}))
        assert params == {"Metadata": {"a": "b"}, "MetadataDirective": "REPLACE"}

    def test_explicit_directive_wins_over_auto_replace(self) -> None:
        options = TransferOptions(metadata={"a": "b"}, metadata_directive="COPY")
        params = map_copy_object_params(options)
        assert params["MetadataDirective"] == "COPY"

    def test_directive_without_metadata(self) -> None:
        params = map_copy_object_params(TransferOptions(metadata_directive="REPLACE"))
        assert params == {"MetadataDirective": "REPLACE"}

    def test_both_sse_c_sides(self) -> None:
        options = TransferOptions(
            sse_c="AES256",
            sse_c_key=b"dest",
            sse_c_copy_source="AES256",
            sse_c_copy_source_key=b"src",
        )
        assert map_copy_object_params(options) == {
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": b"dest",
            "CopySourceSSECustomerAlgorithm": "AES256",
            "CopySourceSSECustomerKey": b"src",
        }

    def test_sse_and_kms_key(self) -> None:
        # The copy path shares _set_sse_request_params with the upload path, so a
        # server-side copy carries SSE-KMS just like a PutObject upload does.
        options = TransferOptions(sse="aws:kms", sse_kms_key_id="key-id")
        assert map_copy_object_params(options) == {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": "key-id",
        }

    def test_general_params_apply_to_copies(self) -> None:
        params = map_copy_object_params(TransferOptions(storage_class="GLACIER"))
        assert params == {"StorageClass": "GLACIER"}


class TestHeadObjectParams:
    def test_head_uses_destination_side_sse_c(self) -> None:
        options = TransferOptions(sse_c="AES256", sse_c_key=b"k", request_payer="requester")
        assert map_head_object_params(options) == {
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": b"k",
            "RequestPayer": "requester",
        }

    def test_copy_source_head_uses_copy_source_sse_c(self) -> None:
        # Heading the object being copied: its encryption key is the copy
        # *source* one, mapped onto the plain SSECustomer* parameters.
        options = TransferOptions(
            sse_c="AES256",
            sse_c_key=b"dest",
            sse_c_copy_source="AES256",
            sse_c_copy_source_key=b"src",
        )
        assert map_head_object_params_with_copy_source_sse(options) == {
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": b"src",
        }


class TestGrants:
    def test_each_permission_maps_to_its_grant_param(self) -> None:
        options = TransferOptions(
            grants=["read=id=a", "full=id=b", "readacl=id=c", "writeacl=id=d"]
        )
        assert map_put_object_params(options) == {
            "GrantRead": "id=a",
            "GrantFullControl": "id=b",
            "GrantReadACP": "id=c",
            "GrantWriteACP": "id=d",
        }

    def test_grantee_keeps_embedded_equals_signs(self) -> None:
        params = map_put_object_params(TransferOptions(grants=["read=uri=http://x/y"]))
        assert params == {"GrantRead": "uri=http://x/y"}

    def test_malformed_grant_uses_awscli_wording(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            map_put_object_params(TransferOptions(grants=["foo"]))
        assert str(excinfo.value) == "grants should be of the form permission=principal"

    def test_unknown_permission_uses_awscli_wording(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            map_copy_object_params(TransferOptions(grants=["bogus=id=x"]))
        assert str(excinfo.value) == "permission must be one of: read|readacl|writeacl|full"

    @pytest.mark.parametrize("operation", ["cp", "mv", "sync"])
    def test_grant_error_reports_the_originating_operation(self, operation: str) -> None:
        # The same upload/copy param mapping serves cp, mv and sync, so a bad
        # --grants value must blame the operation actually in flight, not "cp".
        with pytest.raises(ValidationError) as malformed:
            map_put_object_params(TransferOptions(grants=["foo"]), operation)
        assert malformed.value.operation == operation
        with pytest.raises(ValidationError) as unknown:
            map_copy_object_params(TransferOptions(grants=["bogus=id=x"]), operation)
        assert unknown.value.operation == operation

    def test_grant_error_defaults_to_cp_operation(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            map_put_object_params(TransferOptions(grants=["foo"]))
        assert excinfo.value.operation == "cp"

    def test_grants_none_or_empty_is_a_noop(self) -> None:
        # aws-cli gates on truthiness, so None / empty grants is "no grants" -
        # not a TypeError from iterating None (the library API is permissive).
        assert map_put_object_params(TransferOptions(grants=None)) == {}  # type: ignore[typeddict-item]
        assert map_put_object_params(TransferOptions(grants=[])) == {}
        assert map_copy_object_params(TransferOptions(grants=())) == {}


class TestAncillaryMappers:
    def test_request_payer_only_operations(self) -> None:
        options = TransferOptions(request_payer="requester", acl="private")
        expected = {"RequestPayer": "requester"}
        assert map_get_object_tagging_params(options) == expected
        assert map_list_object_annotations_params(options) == expected
        assert map_get_object_annotation_params(options) == expected
        assert map_put_object_tagging_params(options) == expected
        assert map_delete_object_params(options) == expected

    def test_no_request_payer_means_empty(self) -> None:
        assert map_get_object_tagging_params(TransferOptions()) == {}
        assert map_list_object_annotations_params(TransferOptions()) == {}
        assert map_get_object_annotation_params(TransferOptions()) == {}
