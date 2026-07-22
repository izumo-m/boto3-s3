"""Unit tests for boto3_s3.masking: redaction notation, proxy, and set_stream_logger."""

import io
import logging
import pathlib
import sys
from collections.abc import Generator
from contextlib import contextmanager

import pytest

import boto3_s3
import boto3_s3.masking as m

ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"  # 20 chars; last 4 == "MPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SIGNATURE = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
SESSION_TOKEN = "FQoGZXIvYXdzEMPLELONGSESSIONTOKENvalue1234567890abcdefABCDEF+/=="
# A 44-char base64 SSE-C customer key (raw 32-byte AES-256 key, base64-encoded).
SSE_C_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

# Importable module attributes; only SURFACE_NAMES form the documented
# tier-2 surface (the primitives are internal helpers).
MODULE_NAMES = [
    "MASK",
    "MASK_MIN_LEN",
    "MASK_REVEAL_LEN",
    "SecretMaskingFilter",
    "mask_text",
    "set_stream_logger",
]
SURFACE_NAMES = ["SecretMaskingFilter", "set_stream_logger"]


@contextmanager
def _stream_logger(
    name: str, **kwargs: object
) -> Generator[tuple[logging.Logger, io.StringIO], None, None]:
    """Set up a masked stream logger over a StringIO and restore on exit."""
    logger = logging.getLogger(name)
    before_handlers = list(logger.handlers)
    before_level = logger.level
    buf = io.StringIO()
    m.set_stream_logger(name, stream=buf, **kwargs)  # type: ignore[arg-type]
    try:
        yield logger, buf
    finally:
        for handler in list(logger.handlers):
            if handler not in before_handlers:
                logger.removeHandler(handler)
        logger.setLevel(before_level)


class TestMaskTextNotation:
    def test_access_key_id_reveals_last_four(self) -> None:
        out = m.mask_text(f"using key {ACCESS_KEY_ID} now")
        assert ACCESS_KEY_ID not in out
        assert "***MPLE" in out  # MASK + last 4, not a partial "..." reveal

    def test_signature_query_fully_masked(self) -> None:
        url = (
            "https://b.s3/k?X-Amz-Date=20260613T000000Z"
            f"&X-Amz-Expires=3600&X-Amz-Signature={SIGNATURE}"
        )
        out = m.mask_text(url)
        assert SIGNATURE not in out
        assert "X-Amz-Signature=***" in out
        assert "X-Amz-Date=20260613T000000Z" in out and "X-Amz-Expires=3600" in out

    def test_signature_botocore_auth_colon_newline_form(self) -> None:
        # botocore.auth logs ``logger.debug('Signature:\n%s', signature)``.
        out = m.mask_text(f"Signature:\n{SIGNATURE}")
        assert SIGNATURE not in out
        assert out == "Signature:\n***"

    def test_credential_header_form_reveals_akid_keeps_scope(self) -> None:
        cred = f"Credential={ACCESS_KEY_ID}/20260613/us-east-1/s3/aws4_request"
        out = m.mask_text(cred)
        assert ACCESS_KEY_ID not in out
        assert out == "Credential=***MPLE/20260613/us-east-1/s3/aws4_request"

    def test_credential_presigned_percent_encoded_scope(self) -> None:
        cred = f"X-Amz-Credential={ACCESS_KEY_ID}%2F20260613%2Fus-east-1%2Fs3%2Faws4_request"
        out = m.mask_text(cred)
        assert ACCESS_KEY_ID not in out
        assert out == "X-Amz-Credential=***MPLE%2F20260613%2Fus-east-1%2Fs3%2Faws4_request"

    def test_non_aws_shaped_credential_masks_entirely(self) -> None:
        # The tail reveal is scoped to AWS-shaped ids; a foreign key in the
        # same slot (a MinIO admin key, say) must not leak its tail.
        out = m.mask_text("Credential=minioadmin1234567890/20260613/us-east-1/s3/aws4_request")
        assert out == "Credential=***/20260613/us-east-1/s3/aws4_request"

    def test_non_aws_shaped_access_key_id_param_masks_entirely(self) -> None:
        out = m.mask_text("AWSAccessKeyId=minioadmin1234567890&Expires=60")
        assert out.startswith("AWSAccessKeyId=***")
        assert "minioadmin" not in out
        assert "7890" not in out

    def test_session_token_query_form(self) -> None:
        out = m.mask_text(f"https://b/k?X-Amz-Security-Token={SESSION_TOKEN}&X-Amz-Expires=60")
        assert SESSION_TOKEN not in out
        assert "X-Amz-Security-Token=***" in out
        assert "X-Amz-Expires=60" in out

    def test_sigv2_security_token_query_form(self) -> None:
        # botocore's SigV2 request signer (query-protocol services) puts the
        # session token in a bare `SecurityToken` parameter - no x-amz-
        # prefix (the S3 hmacv1 presigner spells it x-amz-security-token,
        # already covered above); a listing continuation token stays visible.
        out = m.mask_text(
            f"https://b/k?AWSAccessKeyId=AKIA1234567890ABCDEF&SecurityToken={SESSION_TOKEN}"
            "&ContinuationToken=abcdef123456"
        )
        assert SESSION_TOKEN not in out
        assert "SecurityToken=***" in out
        assert "ContinuationToken=abcdef123456" in out

    def test_sso_bearer_token_dict_repr_header(self) -> None:
        # botocore's `sso GetRoleCredentials` request carries the bearer token
        # in the x-amz-sso_bearer_token header, logged at DEBUG - the secret
        # that mints role credentials for every account the user can access.
        token = "aoal-verylongssobearertokenvalue123456"
        out = m.mask_text(f"{{'x-amz-sso_bearer_token': '{token}', 'Host': 'portal.sso'}}")
        assert token not in out
        assert "'x-amz-sso_bearer_token': '***" in out
        assert "'Host': 'portal.sso'" in out

    def test_sso_bearer_token_plain_header_form(self) -> None:
        token = "aoal-verylongssobearertokenvalue123456"
        out = m.mask_text(f"x-amz-sso_bearer_token:{token}")
        assert token not in out
        assert out.startswith("x-amz-sso_bearer_token:***")

    def test_sso_oidc_token_response_body(self) -> None:
        # botocore.parsers logs the CreateToken / RegisterClient response
        # bodies at DEBUG ('Response body:'): accessToken / refreshToken /
        # idToken / clientSecret are bearer-grade secrets.
        body = (
            '{"accessToken": "aoat-access123", "refreshToken": "aort-refresh456", '
            '"idToken": "aoid-id789", "clientSecret": "secret-abc", "expiresIn": 3600}'
        )
        out = m.mask_text(body)
        for secret in ("aoat-access123", "aort-refresh456", "aoid-id789", "secret-abc"):
            assert secret not in out
        assert '"accessToken": "***' in out
        assert '"expiresIn": 3600' in out

    def test_session_token_dict_repr_header(self) -> None:
        out = m.mask_text(f"{{'X-Amz-Security-Token': '{SESSION_TOKEN}', 'Host': 'b.s3'}}")
        assert SESSION_TOKEN not in out
        assert "'X-Amz-Security-Token': '***'" in out
        assert "'Host': 'b.s3'" in out

    def test_session_token_bytes_repr_header(self) -> None:
        out = m.mask_text(f"{{'X-Amz-Security-Token': b'{SESSION_TOKEN}'}}")
        assert SESSION_TOKEN not in out
        assert "'X-Amz-Security-Token': b'***'" in out

    def test_s3express_session_token_header_forms(self) -> None:
        # The S3 Express (directory bucket) flow signs every zonal request with
        # the CreateSession-minted x-amz-s3session-token - the same secret
        # grade as x-amz-security-token, in the same canonical-request and
        # dict-repr DEBUG surfaces.
        out = m.mask_text(f"x-amz-s3session-token:{SESSION_TOKEN}")
        assert SESSION_TOKEN not in out
        assert out.startswith("x-amz-s3session-token:***")
        out = m.mask_text(f"{{'X-Amz-S3session-Token': '{SESSION_TOKEN}', 'Host': 'b.s3'}}")
        assert SESSION_TOKEN not in out
        assert "'X-Amz-S3session-Token': '***'" in out
        assert "'Host': 'b.s3'" in out

    def test_sse_c_key_dict_repr_header_masked_md5_kept(self) -> None:
        # The base64 customer key is the symmetric encryption key (a true
        # secret); the companion -md5 header is a non-secret hash and is kept.
        out = m.mask_text(
            "{'x-amz-server-side-encryption-customer-key': "
            f"'{SSE_C_KEY}', "
            "'x-amz-server-side-encryption-customer-key-md5': 'aGFzaA=='}"
        )
        assert SSE_C_KEY not in out
        assert "'x-amz-server-side-encryption-customer-key': '***'" in out
        assert "'x-amz-server-side-encryption-customer-key-md5': 'aGFzaA=='" in out

    def test_sse_c_key_canonical_request_form_masked(self) -> None:
        out = m.mask_text(
            f"x-amz-server-side-encryption-customer-key:{SSE_C_KEY}\nx-amz-date:20260614T000000Z"
        )
        assert SSE_C_KEY not in out
        assert "x-amz-server-side-encryption-customer-key:***" in out
        assert "x-amz-date:20260614T000000Z" in out

    def test_sse_c_copy_source_key_masked(self) -> None:
        out = m.mask_text(
            f"{{'x-amz-copy-source-server-side-encryption-customer-key': '{SSE_C_KEY}'}}"
        )
        assert SSE_C_KEY not in out
        assert "'x-amz-copy-source-server-side-encryption-customer-key': '***'" in out

    def test_sse_c_key_s3transfer_task_kwargs_masked_md5_kept(self) -> None:
        # s3transfer logs each task's kwargs at DEBUG *before* botocore's
        # parameter build base64-encodes the key, so the boto3 parameter-name
        # form carries the raw secret - the one SSE-C surface that is not a
        # wire header.
        out = m.mask_text(
            "PutObjectTask(transfer_id=0, {'bucket': 'b', 'key': 'k', 'extra_args': "
            f"{{'SSECustomerAlgorithm': 'AES256', 'SSECustomerKey': '{SSE_C_KEY}', "
            "'SSECustomerKeyMD5': 'aGFzaA=='}) about to wait"
        )
        assert SSE_C_KEY not in out
        assert "'SSECustomerKey': '***'" in out
        assert "'SSECustomerKeyMD5': 'aGFzaA=='" in out

    def test_sse_c_copy_source_param_and_bytes_repr_masked(self) -> None:
        # A raw-bytes key logs as a bytes repr full of backslash escapes (and
        # possibly the other quote character); the mask runs to the closing
        # quote of the opener.
        out = m.mask_text(
            "'extra_args': {'CopySourceSSECustomerKey': b'\\x01\\x02raw\"byte\\'s', "
            "'CopySourceSSECustomerKeyMD5': 'aGFzaA=='}"
        )
        assert "raw" not in out
        assert "'CopySourceSSECustomerKey': b'***'" in out
        assert "'CopySourceSSECustomerKeyMD5': 'aGFzaA=='" in out

    def test_sigv2_authorization_header_signature_masked(self) -> None:
        # Legacy SigV2 header `AWS <id>:<sig>` (signature_version='s3'): the
        # signature after the colon is a secret; the id is tail-revealed.
        out = m.mask_text(f"{{'Authorization': 'AWS {ACCESS_KEY_ID}:{SIGNATURE}'}}")
        assert SIGNATURE not in out
        assert "AWS ***MPLE:***" in out

    def test_sigv4_header_not_touched_by_sigv2_rule(self) -> None:
        # `AWS4-HMAC-SHA256 ...` must not match the SigV2 `AWS <id>:` shape.
        header = (
            f"Authorization: AWS4-HMAC-SHA256 Credential={ACCESS_KEY_ID}"
            f"/20260613/us-east-1/s3/aws4_request, Signature={SIGNATURE}"
        )
        out = m.mask_text(header)
        assert "Credential=***MPLE" in out and "Signature=***" in out

    def test_sts_response_body_xml_credentials_masked(self) -> None:
        out = m.mask_text(
            "b'<AssumeRoleResult><Credentials>"
            f"<AccessKeyId>{ACCESS_KEY_ID}</AccessKeyId>"
            f"<SecretAccessKey>{SECRET_KEY}</SecretAccessKey>"
            f"<SessionToken>{SESSION_TOKEN}</SessionToken>"
            "</Credentials></AssumeRoleResult>'"
        )
        assert SECRET_KEY not in out and SESSION_TOKEN not in out
        assert "<SecretAccessKey>***</SecretAccessKey>" in out
        assert "<SessionToken>***</SessionToken>" in out

    def test_sts_response_body_json_credentials_masked(self) -> None:
        out = m.mask_text(
            f'{{"Credentials": {{"SecretAccessKey": "{SECRET_KEY}", '
            f'"SessionToken": "{SESSION_TOKEN}"}}}}'
        )
        assert SECRET_KEY not in out and SESSION_TOKEN not in out
        assert '"SecretAccessKey": "***"' in out
        assert '"SessionToken": "***"' in out

    def test_imds_parsed_credentials_dict_repr_masked(self) -> None:
        # botocore's instance-metadata fetcher logs the *parsed* credentials
        # dict when a response misses a required field - a Python dict repr,
        # so single quotes and the metadata-service key name ``Token``.
        out = m.mask_text(
            "Error response received when retrieving credentials: "
            f"{{'AccessKeyId': '{ACCESS_KEY_ID}', 'SecretAccessKey': '{SECRET_KEY}', "
            f"'Token': '{SESSION_TOKEN}', 'Code': 'Success'}}."
        )
        assert SECRET_KEY not in out and SESSION_TOKEN not in out
        assert "'SecretAccessKey': '***" in out
        assert "'Token': '***" in out

    def test_ecs_metadata_json_body_token_masked(self) -> None:
        # The container-metadata fetcher logs the raw response body on
        # malformed JSON; the session token rides under ``Token`` there.
        out = m.mask_text(
            f'Unable to parse JSON returned from ECS metadata: {{"AccessKeyId": '
            f'"{ACCESS_KEY_ID}", "SecretAccessKey": "{SECRET_KEY}", '
            f'"Token": "{SESSION_TOKEN}"'
        )
        assert SECRET_KEY not in out and SESSION_TOKEN not in out
        assert '"Token": "***' in out

    def test_listing_continuation_tokens_stay_visible(self) -> None:
        # The quote anchored to the key name keeps pagination tokens (which
        # merely end in ...Token) out of the credential mask.
        text = '{"NextContinuationToken": "1dpEcSGKtHnzkiE", "ContinuationToken": "3qmQzXfE"}'
        assert m.mask_text(text) == text

    def test_signature_mismatch_byte_dump_masked(self) -> None:
        # A SignatureDoesNotMatch (403) body echoes the canonical request as a hex
        # byte-dump; bytes.fromhex would reconstruct the x-amz-security-token line,
        # so the text-form masks never see it. The whole dump must be masked.
        hex_dump = ("x-amz-security-token:" + SESSION_TOKEN).encode().hex(" ")
        body = (
            "<Error><Code>SignatureDoesNotMatch</Code>"
            f"<CanonicalRequestBytes>{hex_dump}</CanonicalRequestBytes>"
            f"<StringToSignBytes>{hex_dump}</StringToSignBytes></Error>"
        )
        out = m.mask_text(body)
        assert hex_dump not in out  # the reversible dump is gone
        assert "<CanonicalRequestBytes>***</CanonicalRequestBytes>" in out
        assert "<StringToSignBytes>***</StringToSignBytes>" in out

    def test_signature_mismatch_signature_provided_masked(self) -> None:
        # A SignatureDoesNotMatch (403) body echoes the client's signature back in
        # <SignatureProvided>; the header/query-form signature mask never sees this
        # XML element, so it is masked here. The access key id in the same body is
        # still tail-revealed by the standalone id regex.
        body = (
            "<Error><Code>SignatureDoesNotMatch</Code>"
            f"<AWSAccessKeyId>{ACCESS_KEY_ID}</AWSAccessKeyId>"
            f"<SignatureProvided>{SIGNATURE}</SignatureProvided></Error>"
        )
        out = m.mask_text(body)
        assert SIGNATURE not in out
        assert "<SignatureProvided>***</SignatureProvided>" in out
        assert "<AWSAccessKeyId>***MPLE</AWSAccessKeyId>" in out

    def test_web_identity_token_request_dict_repr_masked(self) -> None:
        # The web-identity flow (role_arn + web_identity_token_file, EKS IRSA)
        # sends a raw JWT to the unsigned AssumeRoleWithWebIdentity API; botocore
        # logs the request_dict at DEBUG. The token, with the RoleArn on the same
        # line, mints role credentials - a bearer-grade request secret.
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0ifQ.c2lnbmF0dXJldmFsdWU"
        line = (
            "Making request for AssumeRoleWithWebIdentity with params: "
            "{'Action': 'AssumeRoleWithWebIdentity', "
            "'RoleArn': 'arn:aws:iam::123456789012:role/demo', "
            f"'WebIdentityToken': '{jwt}'}}"
        )
        out = m.mask_text(line)
        assert jwt not in out
        assert "'WebIdentityToken': '***'" in out
        assert "'RoleArn': 'arn:aws:iam::123456789012:role/demo'" in out

    def test_web_identity_token_query_body_masked(self) -> None:
        # The urlencoded query-protocol body form (defensive coverage).
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0ifQ.c2lnbmF0dXJldmFsdWU"
        body = f"Action=AssumeRoleWithWebIdentity&WebIdentityToken={jwt}&Version=2011-06-15"
        out = m.mask_text(body)
        assert jwt not in out
        assert "WebIdentityToken=***" in out
        assert "Version=2011-06-15" in out

    def test_saml_assertion_request_masked(self) -> None:
        # AssumeRoleWithSAML's SAMLAssertion is the same-shape unsigned-API request
        # secret as the web-identity token and is masked alongside it.
        assertion = "PHNhbWxwOlJlc3BvbnNlPmFzc2VydGlvbmJhc2U2NGJsb2J2YWx1ZQ=="
        out = m.mask_text(f"{{'SAMLAssertion': '{assertion}'}}")
        assert assertion not in out
        assert "'SAMLAssertion': '***'" in out

    def test_long_non_url_run_does_not_hang(self) -> None:
        # Guards against ReDoS in the proxy-URL regex: a long contiguous
        # scheme-class run that never reaches "://" must mask in linear time.
        import time

        start = time.monotonic()
        m.mask_text("a.b+c-" * 50000)
        assert time.monotonic() - start < 2.0

    def test_sigv4_authorization_header_components(self) -> None:
        header = (
            f"Authorization: AWS4-HMAC-SHA256 Credential={ACCESS_KEY_ID}"
            "/20260613/us-east-1/s3/aws4_request, "
            f"SignedHeaders=host;x-amz-date, Signature={SIGNATURE}"
        )
        out = m.mask_text(header)
        assert ACCESS_KEY_ID not in out and SIGNATURE not in out
        assert "Credential=***MPLE" in out
        assert "Signature=***" in out
        assert "SignedHeaders=host;x-amz-date" in out

    def test_botocore_prepared_request_repr(self) -> None:
        # The real leak: botocore endpoint.py logs AWSPreparedRequest.__repr__,
        # which renders the signed headers dict at DEBUG.
        record = (
            "<AWSPreparedRequest stream_output=False, method=PUT, "
            "url=https://b.s3.amazonaws.com/k, headers={'User-Agent': 'aws-cli', "
            f"'Authorization': 'AWS4-HMAC-SHA256 Credential={ACCESS_KEY_ID}"
            "/20260613/us-east-1/s3/aws4_request, SignedHeaders=host, "
            f"Signature={SIGNATURE}', 'X-Amz-Security-Token': '{SESSION_TOKEN}'}}>"
        )
        out = m.mask_text(record)
        for secret in (ACCESS_KEY_ID, SIGNATURE, SESSION_TOKEN):
            assert secret not in out
        assert "Credential=***MPLE/20260613/us-east-1/s3/aws4_request" in out
        assert "Signature=***" in out
        assert "'X-Amz-Security-Token': '***'" in out

    def test_text_without_secrets_unchanged(self) -> None:
        text = "GET /bucket/key.txt - size=1024 storage_class=STANDARD"
        assert m.mask_text(text) == text


class TestMaskTextProxy:
    def test_proxy_url_userinfo_masked(self) -> None:
        out = m.mask_text("proxy https://myuser:s3cr3tpw@proxy.example.com:8080 set")
        assert out == "proxy https://***:***@proxy.example.com:8080 set"

    def test_proxy_url_userinfo_without_password(self) -> None:
        out = m.mask_text("proxy http://onlyuser@proxy.internal:3128 set")
        assert out == "proxy http://***@proxy.internal:3128 set"

    def test_proxy_url_empty_password_still_masks(self) -> None:
        # botocore's mask_proxy_url masks the username of ``user:@`` too.
        out = m.mask_text("proxy https://myuser:@proxy.example.com:8080 set")
        assert out == "proxy https://***:***@proxy.example.com:8080 set"

    def test_proxy_url_empty_username_still_masks(self) -> None:
        out = m.mask_text("proxy https://:s3cr3tpw@proxy.example.com set")
        assert out == "proxy https://***:***@proxy.example.com set"

    def test_bare_userinfo_marker_is_kept(self) -> None:
        assert m.mask_text("https://@proxy.internal/x") == "https://@proxy.internal/x"

    def test_matches_botocore_mask_proxy_url(self) -> None:
        # Parity proof: same notation as the only masking precedent in botocore.
        from botocore.httpsession import mask_proxy_url

        url = "https://myuser:s3cr3tpassword@proxy.internal:3128"
        assert m.mask_text(url) == mask_proxy_url(url)

    def test_proxy_authorization_dict_repr(self) -> None:
        out = m.mask_text("{'Proxy-Authorization': 'Basic dXNlcjpwYXNzd29yZA=='}")
        assert "dXNlcjpwYXNzd29yZA==" not in out
        assert "'Proxy-Authorization': '***'" in out

    def test_proxy_authorization_colon_form(self) -> None:
        out = m.mask_text("Proxy-Authorization: Basic dXNlcjpwYXNzd29yZA==\\r\\n")
        assert "dXNlcjpwYXNzd29yZA==" not in out
        assert "Proxy-Authorization: ***" in out


class TestMaskTextExtraSecrets:
    def test_extra_secrets_masked_everywhere(self) -> None:
        out = m.mask_text(f"key={SECRET_KEY} again {SECRET_KEY}", extra_secrets=[SECRET_KEY])
        assert SECRET_KEY not in out
        assert out == f"key={m.MASK} again {m.MASK}"

    def test_short_extra_secret_not_masked(self) -> None:
        # Below MASK_MIN_LEN: a literal too short to mask safely is left alone.
        out = m.mask_text("token=abc", extra_secrets=["abc"])
        assert out == "token=abc"


class TestSecretMaskingFilter:
    def _record(self, msg: str, *args: object) -> logging.LogRecord:
        return logging.LogRecord("boto3_s3.test", logging.DEBUG, __file__, 1, msg, args, None)

    def test_record_embedding_signature_is_masked(self) -> None:
        f = m.SecretMaskingFilter()
        record = self._record(f"signed: X-Amz-Signature={SIGNATURE}")
        assert f.filter(record) is True
        assert SIGNATURE not in record.getMessage()
        assert "X-Amz-Signature=***" in record.getMessage()

    def test_secret_in_args_masked_after_formatting(self) -> None:
        f = m.SecretMaskingFilter()
        record = self._record("request: %s", f"https://b/k?X-Amz-Signature={SIGNATURE}")
        f.filter(record)
        assert SIGNATURE not in record.getMessage()
        assert "X-Amz-Signature=***" in record.getMessage()

    def test_extra_secrets_applied(self) -> None:
        f = m.SecretMaskingFilter(extra_secrets=[SECRET_KEY])
        record = self._record("creds=%s", SECRET_KEY)
        f.filter(record)
        assert SECRET_KEY not in record.getMessage()

    def test_exception_traceback_is_masked(self) -> None:
        # A record logged with exc_info: the handler appends the traceback from
        # record.exc_text, a channel the message mask never sees. A secret in the
        # exception message must be masked there too.
        f = m.SecretMaskingFilter()
        try:
            raise ValueError(f"failed https://b/k?X-Amz-Signature={SIGNATURE}")
        except ValueError:
            record = logging.LogRecord(
                "boto3_s3.test", logging.DEBUG, __file__, 1, "boom", (), sys.exc_info()
            )
        assert f.filter(record) is True
        assert record.exc_text is not None
        assert SIGNATURE not in record.exc_text
        assert "X-Amz-Signature=***" in record.exc_text

    def test_filter_admits_all_records(self) -> None:
        f = m.SecretMaskingFilter()
        assert f.filter(self._record("anything")) is True

    def test_record_without_secret_unchanged(self) -> None:
        f = m.SecretMaskingFilter()
        record = self._record("listing key %s", "dir/file.txt")
        f.filter(record)
        assert record.getMessage() == "listing key dir/file.txt"

    def test_bad_format_does_not_raise(self) -> None:
        f = m.SecretMaskingFilter()
        record = self._record("broken %s %s", "only-one")
        assert f.filter(record) is True


class TestSetStreamLogger:
    def test_attaches_handler_at_level(self) -> None:
        with _stream_logger("test.boto3_s3.attach", level=logging.INFO) as (logger, _buf):
            assert logger.level == logging.INFO
            handlers = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]
            assert handlers and handlers[-1].level == logging.INFO

    def test_masks_secrets_by_default(self) -> None:
        with _stream_logger("test.boto3_s3.mask_on") as (logger, buf):
            logger.debug("signing X-Amz-Signature=%s done", SIGNATURE)
        out = buf.getvalue()
        assert SIGNATURE not in out
        assert "X-Amz-Signature=***" in out

    def test_mask_secrets_false_leaves_raw(self) -> None:
        with _stream_logger("test.boto3_s3.mask_off", mask_secrets=False) as (logger, buf):
            logger.debug("signing X-Amz-Signature=%s done", SIGNATURE)
        assert f"X-Amz-Signature={SIGNATURE}" in buf.getvalue()

    def test_child_logger_record_is_masked(self) -> None:
        # The design point: a filter on the handler masks records propagated up
        # from child loggers (e.g. botocore.auth), which logger-level filters miss.
        with _stream_logger("test.boto3_s3.parent") as (_parent, buf):
            logging.getLogger("test.boto3_s3.parent.child").debug(
                "token X-Amz-Security-Token=%s", SESSION_TOKEN
            )
        out = buf.getvalue()
        assert SESSION_TOKEN not in out
        assert "X-Amz-Security-Token=***" in out

    def test_default_name_and_format(self) -> None:
        with _stream_logger("boto3_s3") as (logger, buf):
            # Default name is "boto3_s3"; default format carries name and level.
            assert logging.getLogger("boto3_s3") is logger
            logger.debug("hello")
        out = buf.getvalue()
        assert "boto3_s3" in out and "[DEBUG]" in out and "hello" in out


class TestPublicModuleSurface:
    @pytest.mark.parametrize("name", MODULE_NAMES)
    def test_import_from_masking_module(self, name: str) -> None:
        assert hasattr(m, name)

    def test_module_all_lists_exactly_the_names(self) -> None:
        assert sorted(m.__all__) == sorted(SURFACE_NAMES)

    def test_set_stream_logger_is_public_top_level(self) -> None:
        assert "set_stream_logger" in boto3_s3.__all__
        assert boto3_s3.set_stream_logger is m.set_stream_logger

    @pytest.mark.parametrize(
        "name", ["MASK", "MASK_MIN_LEN", "MASK_REVEAL_LEN", "SecretMaskingFilter", "mask_text"]
    )
    def test_primitives_stay_internal_to_masking(self, name: str) -> None:
        # Only set_stream_logger is re-exported; the primitives are not top-level.
        assert name not in boto3_s3.__all__


class TestBoto3Independence:
    def test_masking_source_has_no_backend_imports(self) -> None:
        source = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        for backend in ("boto3", "botocore", "s3transfer"):
            assert f"import {backend}" not in source
            assert f"from {backend}" not in source
