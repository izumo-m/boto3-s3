"""Pure secret-masking primitives and the masked debug-logging entry point.

``mask_text`` and ``SecretMaskingFilter`` redact credential-bearing text;
``set_stream_logger`` is the boto3-faithful entry that attaches a stream handler
(carrying the masking filter, when ``mask_secrets``) so a caller enabling debug
output never leaks signatures, access keys, session tokens, SSO bearer /
sso-oidc tokens, web-identity / SAML STS request tokens, SSE-C keys,
STS-response credentials, the hex byte-dumps a signature-mismatch error body
echoes, or proxy credentials. The
module is pure stdlib - it imports no ``boto3`` / ``botocore`` / ``s3transfer``
- so the CLI can import it on the ``--debug`` path without breaking the import
contract (docs/imports.md).

The credential leak under ``--debug`` flows through the Python ``logging``
system (botocore logs the signed ``AWSPreparedRequest`` - Authorization /
Signature / X-Amz-Security-Token - and parsed response bodies, and s3transfer
logs each task's kwargs including ``extra_args`` with the raw ``SSECustomerKey``,
all at DEBUG), so masking lives in a logging filter on the handler, not in an
``http.client`` patch (the wire dump only
appears when ``http.client.HTTPConnection.debuglevel`` is raised, which this
project never does).

Replacement notation follows the only masking precedent in aws-cli / boto3,
``botocore.httpsession.mask_proxy_url`` (``***``): every secret value is
replaced with ``***``. The sole exception is the AWS Access Key ID, whose last
four characters are revealed (``***MPLE``) so a reader can tell which account
issued the request - the credential scope, parameter names, and the proxy host
are preserved.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TextIO

# Marker substituted for a masked value (matches `mask_proxy_url`).
MASK = "***"

# Minimum length for masking. Shorter Access Key IDs are fully masked, while
# shorter `extra_secrets` literals are ignored to avoid blanking broad log spans.
MASK_MIN_LEN = 16

# Access Key ID trailing characters left visible for account identification.
MASK_REVEAL_LEN = 4

# Value terminator for matches taken from URLs, headers, and the repr() of a
# headers dict: a run of characters that are not whitespace, a separator, or a
# quote. Stops at whitespace, the leading backslash of an escaped ``\r\n``, the
# ``&`` query boundary, the ``,`` Authorization-component boundary, and the
# ``'`` / ``"`` that close a value inside a dict repr.
_VALUE = r"[^\s\\&,'\"]+"

# Access Key ID literal (AWS long-term ``AKIA`` / temporary ``ASIA``).
_ACCESS_KEY_ID_RE = re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")

# Signature values (full mask): the SigV4 query parameter, the ``Signature=``
# component of an Authorization header, and the standalone ``Signature:\n<hex>``
# line botocore.auth logs at DEBUG (the separator may be ``=`` or ``:`` and may
# be followed by whitespace, including the newline before the value).
_SIGNATURE_RE = re.compile(
    rf"(?P<key>(?:X-Amz-Signature|Signature)\s*[:=]\s*)(?P<val>{_VALUE})",
    re.IGNORECASE,
)

# Leading Access Key ID of a Credential value (``Credential=`` /
# ``X-Amz-Credential=``); the signing scope after the ``/`` (header form) or the
# percent-encoded ``%2F`` (presigned-query form) is non-secret and kept, so the
# value stops at ``/`` and ``%``.
_CREDENTIAL_RE = re.compile(
    r"(?P<key>(?:X-Amz-Credential|Credential)=)(?P<val>[^\s\\&,/'\"%]+)",
    re.IGNORECASE,
)
# SigV2 ``AWSAccessKeyId=`` query parameter (id with no scope).
_AWS_ACCESS_KEY_ID_PARAM_RE = re.compile(
    rf"(?P<key>AWSAccessKeyId=)(?P<val>{_VALUE})",
    re.IGNORECASE,
)

# Session token (full mask): the SigV4 query parameter, the request-header colon
# form, and the dict-repr header form botocore logs in
# ``AWSPreparedRequest.__repr__`` (``'X-Amz-Security-Token': '...'``). ``key``
# swallows the name, the ``:`` / ``=`` separator, and any opening quote so only
# the value is replaced.
_TOKEN_VALUE = r"[^\s'\"&,\\}]+"
_SECURITY_TOKEN_RE = re.compile(
    rf"(?P<key>x-amz-security-token['\"]?\s*[:=]\s*(?:b?['\"])?)(?P<val>{_TOKEN_VALUE})",
    re.IGNORECASE,
)

# SSO bearer token (full mask): botocore's ``sso GetRoleCredentials`` request
# carries it in the ``x-amz-sso_bearer_token`` header, logged at DEBUG in both
# the request-dict and AWSPreparedRequest-repr forms. The token mints role
# credentials for every account/role the user can access - the highest-value
# secret on the SSO auth path. Same key/value shape as the security-token RE.
_SSO_BEARER_TOKEN_RE = re.compile(
    rf"(?P<key>x-amz-sso_bearer_token['\"]?\s*[:=]\s*(?:b?['\"])?)(?P<val>{_TOKEN_VALUE})",
    re.IGNORECASE,
)

# sso-oidc token-endpoint bodies (full mask): a token refresh logs the
# CreateToken response (``accessToken`` / ``refreshToken`` / ``idToken``) and
# client registration logs ``clientSecret`` (RegisterClient) - each a
# bearer-grade secret - via botocore.parsers' DEBUG ``Response body:`` line
# (these services are JSON-only, so no XML twin is needed).
_SSO_OIDC_BODY_JSON_RE = re.compile(
    r'(?P<key>"(?:accessToken|refreshToken|idToken|clientSecret)"\s*:\s*")(?P<val>[^"]+)',
    re.IGNORECASE,
)

# AssumeRoleWithWebIdentity / AssumeRoleWithSAML request tokens (full mask). The
# web-identity flow (role_arn + web_identity_token_file, EKS IRSA) exchanges a
# raw JWT for role credentials through the *unsigned* AssumeRoleWithWebIdentity
# API, so the token alone - with the RoleArn botocore logs on the same DEBUG line
# - mints credentials (a bearer-grade request secret). botocore logs the
# request_dict at DEBUG ("Making request ... with params: %s"); STS is
# query-protocol, so the body renders as a dict (``'WebIdentityToken': '<jwt>'``)
# and, defensively, the urlencoded query form (``WebIdentityToken=<jwt>``).
# AssumeRoleWithSAML's SAMLAssertion is the same-shape secret and rides along.
_WEB_IDENTITY_TOKEN_RE = re.compile(
    r"(?P<key>['\"]?(?:WebIdentityToken|SAMLAssertion)['\"]?\s*[:=]\s*(?:b?['\"])?)"
    rf"(?P<val>{_TOKEN_VALUE})",
    re.IGNORECASE,
)

# SSE-C customer key (full mask): the base64 customer key is the symmetric
# encryption key (a true secret). botocore puts it in the signed request header
# ``x-amz-server-side-encryption-customer-key`` (and the copy-source variant),
# logged at DEBUG in both the dict-repr header form and the canonical-request
# ``name:value`` form. The negative lookahead leaves the companion ``-md5``
# header (a non-secret hash) untouched. Same key/value shape as the token RE.
_SSE_C_KEY_RE = re.compile(
    r"(?P<key>x-amz-(?:copy-source-)?server-side-encryption-customer-key(?!-md5)"
    rf"['\"]?\s*[:=]\s*(?:b?['\"])?)(?P<val>{_TOKEN_VALUE})",
    re.IGNORECASE,
)

# The same secret in its boto3 API-parameter form: s3transfer logs every task's
# kwargs at DEBUG (``s3transfer.tasks`` / ``s3transfer.futures``, e.g.
# ``PutObjectTask(... 'extra_args': {'SSECustomerKey': '<raw key>', ...})``)
# *before* botocore's parameter build base64-encodes the key - the one SSE-C
# surface that is not a wire header. The value may be a str or bytes repr, so
# the match runs to the unescaped quote that closes the opener (backslash
# escapes inside a bytes repr are consumed). ``'SSECustomerKeyMD5'`` cannot
# match: the name must be immediately closed by its quote.
_SSE_C_PARAM_RE = re.compile(
    r"(?P<key>['\"](?:CopySource)?SSECustomerKey['\"]\s*:\s*b?(?P<q>['\"]))"
    r"(?P<val>(?:\\.|(?!(?P=q))[^\\])+)"
)

# SigV2 (HmacV1) Authorization header ``AWS <access-key-id>:<signature>`` (legacy
# signature_version='s3'; non-default for the library, never for the CLI which
# pins s3v4). Mask the signature after the colon; the access key id in the kept
# prefix is tail-revealed by ``_ACCESS_KEY_ID_RE`` afterwards. SigV4's
# ``AWS4-HMAC-SHA256 ...`` does not match (no space before the digit).
_SIGV2_AUTH_HEADER_RE = re.compile(
    rf"(?P<key>AWS (?:AKIA|ASIA)[0-9A-Z]{{16}}:)(?P<val>{_TOKEN_VALUE})"
)

# STS response-body temporary credentials. botocore logs the parsed response body
# at DEBUG (``Response body:``), so an AssumeRole / GetSessionToken / web-identity
# response leaks its ``SecretAccessKey`` and ``SessionToken`` (the temporary
# secret key + token). Covered in both XML (``<SecretAccessKey>...``) and JSON
# (``"SecretAccessKey": "..."``) forms; ``AccessKeyId`` is left for the standalone
# id regex to tail-reveal.
_STS_BODY_XML_CRED_RE = re.compile(
    r"(?P<key><(?:SecretAccessKey|SessionToken)>)(?P<val>[^<]+)", re.IGNORECASE
)
_STS_BODY_JSON_CRED_RE = re.compile(
    r'(?P<key>"(?:SecretAccessKey|SessionToken)"\s*:\s*")(?P<val>[^"]+)', re.IGNORECASE
)

# Hex byte-dumps in an error response body (full mask). A ``SignatureDoesNotMatch``
# (HTTP 403) response echoes the request's canonical form back as
# ``<CanonicalRequestBytes>`` / ``<StringToSignBytes>`` - a space-separated hex
# dump that ``bytes.fromhex`` reconstructs verbatim, INCLUDING the
# ``x-amz-security-token`` / ``x-amz-server-side-encryption-customer-key`` header
# lines. The text-form masks above never see the hex twin, so mask the whole dump
# (botocore.parsers logs error bodies at DEBUG too, not just 2xx).
_BYTE_DUMP_RE = re.compile(
    r"(?P<key><(?:CanonicalRequestBytes|StringToSignBytes)>)(?P<val>[^<]*)", re.IGNORECASE
)

# The client signature echoed back in a SignatureDoesNotMatch (403) body as
# ``<SignatureProvided>`` (beside the byte-dumps above). The header/query-form
# _SIGNATURE_RE matches ``Signature=`` / ``Signature:`` only, never this XML
# element, so the echoed signature is masked here.
_SIGNATURE_PROVIDED_RE = re.compile(r"(?P<key><SignatureProvided>)(?P<val>[^<]*)", re.IGNORECASE)

# Proxy URL credentials (``scheme://user:pass@host`` -> ``scheme://***:***@host``),
# the exact shape ``botocore.httpsession.mask_proxy_url`` masks. The scheme body
# is length-capped ({0,31}): an unbounded greedy run backtracks O(n^2) over any
# long contiguous ``[A-Za-z0-9+.-]`` span that never reaches ``://`` (a ReDoS on
# large DEBUG records, e.g. a long ``--metadata`` value in a logged request),
# and real URL schemes are short (RFC 3986). Python 3.10 has no atomic groups.
_PROXY_URL_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://)"
    r"(?P<user>[^/?#\s:@]+)(?::(?P<pass>[^/?#\s@]+))?@"
)

# Proxy-Authorization header value (defensive: it surfaces only in an
# ``http.client`` wire dump, which this project does not emit). The quoted form
# is the dict repr; the plain form is the raw ``name: value`` header line.
_PROXY_AUTH_QUOTED_RE = re.compile(
    r"(?P<key>['\"]?Proxy-Authorization['\"]?\s*[:=]\s*b?['\"])[^'\"]*",
    re.IGNORECASE,
)
_PROXY_AUTH_PLAIN_RE = re.compile(
    r"(?P<key>Proxy-Authorization\s*:\s*)[^\r\n\\]*",
    re.IGNORECASE,
)


def _reveal_access_key(value: str) -> str:
    """Mask an Access Key ID, leaving its last ``MASK_REVEAL_LEN`` chars visible."""
    if len(value) < MASK_MIN_LEN:
        return MASK
    return MASK + value[-MASK_REVEAL_LEN:]


def mask_text(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """Return *text* with credential-bearing substrings masked.

    Every secret is replaced with ``***`` (the ``mask_proxy_url`` notation)
    except the AWS Access Key ID, whose last four characters are revealed
    (``***MPLE``). Parameter / component names, the Credential signing scope,
    and the proxy host are preserved. Patterns cover both the URL / query form
    and botocore's dict-repr header form. Pure function - no
    ``boto3`` / ``botocore`` / ``s3transfer``.
    """
    text = _SECURITY_TOKEN_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _SSO_BEARER_TOKEN_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _SSO_OIDC_BODY_JSON_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _WEB_IDENTITY_TOKEN_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _SSE_C_KEY_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _SSE_C_PARAM_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _STS_BODY_XML_CRED_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _STS_BODY_JSON_CRED_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _BYTE_DUMP_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _SIGNATURE_PROVIDED_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _SIGNATURE_RE.sub(lambda m: m.group("key") + MASK, text)
    # Before _ACCESS_KEY_ID_RE so the kept ``AWS <id>:`` prefix is tail-revealed.
    text = _SIGV2_AUTH_HEADER_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _CREDENTIAL_RE.sub(lambda m: m.group("key") + _reveal_access_key(m.group("val")), text)
    text = _AWS_ACCESS_KEY_ID_PARAM_RE.sub(
        lambda m: m.group("key") + _reveal_access_key(m.group("val")), text
    )
    text = _ACCESS_KEY_ID_RE.sub(lambda m: _reveal_access_key(m.group(0)), text)
    text = _PROXY_URL_RE.sub(
        lambda m: m.group("scheme") + MASK + (":" + MASK if m.group("pass") else "") + "@",
        text,
    )
    text = _PROXY_AUTH_QUOTED_RE.sub(lambda m: m.group("key") + MASK, text)
    text = _PROXY_AUTH_PLAIN_RE.sub(lambda m: m.group("key") + MASK, text)
    for secret in extra_secrets:
        if secret and len(secret) >= MASK_MIN_LEN:
            text = text.replace(secret, MASK)
    return text


# Default formatter, reused only to render an exception traceback to text (its
# ``formatException`` is the same one every stdlib handler uses) so the filter can
# mask it before a handler appends the raw form.
_EXC_FORMATTER = logging.Formatter()


class SecretMaskingFilter(logging.Filter):
    """``logging.Filter`` that masks credential-bearing text in log records.

    Rewrites each record's final formatted message via ``mask_text`` (and,
    when the record carries one, its exception traceback) and clears its args so
    the masked text is not re-formatted. Belongs on the
    *handler* (not the logger): records propagated up from child loggers such as
    ``botocore.auth`` reach an ancestor only through its handlers, so a
    handler-level filter is the one that sees them. Always admits the record
    (masking is its only job; visibility is decided by level and handlers) and
    never raises if a record fails to format.
    """

    def __init__(self, *, extra_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._extra_secrets: tuple[str, ...] = tuple(extra_secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = mask_text(message, extra_secrets=self._extra_secrets)
        record.args = ()
        # An attached exception is appended by the handler's formatter from
        # record.exc_text (derived from exc_info if unset), a channel mask_text
        # never sees otherwise. Pre-derive and mask it, so a secret embedded in an
        # exception message is redacted too. Formatters reuse a non-empty exc_text.
        if record.exc_info and not record.exc_text:
            record.exc_text = _EXC_FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask_text(record.exc_text, extra_secrets=self._extra_secrets)
        return True


def set_stream_logger(
    name: str = "boto3_s3",
    level: int = logging.DEBUG,
    format_string: str | None = None,
    *,
    stream: TextIO | None = None,
    mask_secrets: bool = True,
    extra_secrets: Iterable[str] = (),
) -> None:
    """Attach a stream handler for *name* at *level* - boto3-faithful, masked.

    Mirrors ``boto3.set_stream_logger`` (same first three positional parameters
    and default format) but, when *mask_secrets* is true (the default),
    attaches a ``SecretMaskingFilter`` to the handler so the
    credential-bearing records botocore emits at DEBUG (signed request headers,
    signatures, session tokens, SSO bearer / sso-oidc tokens, SSE-C keys,
    STS-response credentials, proxy URLs) are redacted before they reach the
    stream. ``boto3`` / ``botocore`` warn that their own debug logging leaks
    these verbatim; this is the safe entry point.

    Extra keyword-only parameters beyond boto3's signature: *stream* (defaults
    to ``sys.stderr``, like ``logging.StreamHandler``), *mask_secrets*, and
    *extra_secrets* (literal values at least ``MASK_MIN_LEN`` characters long,
    masked wherever they appear; shorter values are skipped so a stray short
    string cannot blank out swaths of the log).
    """
    if format_string is None:
        format_string = "%(asctime)s %(name)s [%(levelname)s] %(message)s"
    logger = logging.getLogger(name)
    logger.setLevel(level)
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(format_string))
    if mask_secrets:
        handler.addFilter(SecretMaskingFilter(extra_secrets=extra_secrets))
    logger.addHandler(handler)


__all__ = [
    "MASK",
    "MASK_MIN_LEN",
    "MASK_REVEAL_LEN",
    "SecretMaskingFilter",
    "mask_text",
    "set_stream_logger",
]
