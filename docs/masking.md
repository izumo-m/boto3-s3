# Credential Masking Design for Debug Logs

`boto3-s3` masks, by default, any credentials that flow into debug logs
(`--debug` / `boto3_s3.set_stream_logger`). This is a **safety feature this
project adds beyond aws-cli / boto3**, not a parity item (aws-cli / boto3 mask
nothing other than the proxy URL via `mask_proxy_url`, and boto3 merely warns in
its docstring "do not use in production"). This document is the source of truth
for that design. The implementation lives in `src/boto3_s3/masking.py`, the CLI
wiring in `cli/src/boto3_s3_cli/cli.py`, and the startup-cost contract in
[`imports.md`](./imports.md).

## 1. Purpose and positioning

- The default policy is to **mask** (`set_stream_logger(mask_secrets=True)` is the
  default). The policy is expressed through the arguments of a module function;
  there is no instance-level setting like `Config` (since `logging` is
  process-global, masking is also configured globally, once).
- The library is `boto3`-faithful: the entry point `set_stream_logger` has the
  same first three positional arguments as `boto3.set_stream_logger`
  (`name='boto3_s3'` / `level=DEBUG` / `format_string`), and its own arguments
  (`stream` / `mask_secrets` / `extra_secrets`) are only appended as keyword-only
  at the end.
- `masking.py` is pure stdlib (it does not import `boto3` / `botocore` /
  `s3transfer`). The CLI may import it on the `--debug` path (this does not breach
  the startup-cost contract).

## 2. Leak sources (why a logger filter)

Under `--debug`, credentials actually appear in **botocore's logger output**, not
in the `http.client` wire dump (verified against live botocore).

- In `botocore/endpoint.py`, `logger.debug("Sending http request: %s", request)`
  has a `request` that is an `AWSPreparedRequest`. Its `__repr__` renders
  `headers=` as the `HTTPHeaders` text form - plain `Header: value` lines - so the
  signed `Authorization: AWS4-HMAC-SHA256 Credential=AKIA.../..., Signature=...` and
  the `X-Amz-Security-Token: ...` header are emitted in full (older botocore
  rendered a Python dict repr; the patterns cover both).
- `botocore/auth.py` emits `CanonicalRequest:\n<...>`, `StringToSign:\n<...>`, and
  `Signature:\n<hex>` at DEBUG level (the first two include the signed headers,
  i.e. `x-amz-security-token`). On the S3 Express (directory bucket) flow the
  same surfaces carry `x-amz-s3session-token` instead - the CreateSession-minted
  session token - masked by the same pattern; the CreateSession *response body*
  that mints it is the ordinary `<SessionToken>` / `'SessionToken':` shape
  already covered below.
- An SSE-C transfer (`cp/mv/sync --sse-c --sse-c-key ...`) carries the base64
  customer key - the symmetric encryption key itself - in the signed
  `x-amz-server-side-encryption-customer-key` header (and the copy-source
  variant), so it surfaces in **both** the dict repr and the canonical request.
- `botocore/parsers.py` logs `Response body:\n%r` at DEBUG. Under a temporary-
  credential profile (assume-role / web-identity / session-token), credential
  resolution issues an STS call whose response body carries the temporary
  `SecretAccessKey` and `SessionToken` (the credentials themselves), in XML or
  JSON. These are masked in both encodings. The instance/container metadata
  fetchers (`botocore/utils.py`) add two more DEBUG shapes of the same secrets:
  the parsed-credentials *dict repr* (single quotes) when a response misses a
  required field, and the raw IMDS/ECS JSON body on malformed JSON, where the
  session token rides under the key `Token`. The key/value pattern is therefore
  quote-agnostic and includes `Token` (the quote anchored to the key name keeps
  `ContinuationToken` / `NextToken` visible).
- The same `Response body:` DEBUG line fires for **error** responses. A
  `SignatureDoesNotMatch` (HTTP 403) body echoes the request's canonical form back
  as `<CanonicalRequestBytes>` / `<StringToSignBytes>` - a space-separated hex dump
  that `bytes.fromhex` reconstructs verbatim, *including* the signed
  `x-amz-security-token` / `x-amz-server-side-encryption-customer-key` header lines.
  The text-form patterns never see the hex twin, so the whole dump is masked.

These all go through Python `logging`, so a **`logging.Filter` attached to the
handler** can capture and mask all of them. The `http.client` wire dump
(`send: b'...'`) appears only when `http.client.HTTPConnection.debuglevel >= 1`,
but neither botocore, nor aws, nor this project raises debuglevel, so it does not
appear under `--debug`. We therefore do not handle a monkeypatch of `http.client`
(it would be a no-op).

## 3. Design

### 3.1 `set_stream_logger` (the public entry point)

It faithfully reproduces `boto3.set_stream_logger` while, when `mask_secrets=True`
(the default), attaching a `SecretMaskingFilter` to the handler. This makes
"enabling debug" and "configuring masking" the same entry point. The default
format is identical to boto3
(`"%(asctime)s %(name)s [%(levelname)s] %(message)s"`), and the default `stream`
is `sys.stderr`.

### 3.2 `SecretMaskingFilter` is attached on the handler side

The filter is attached to the **handler**, not the logger. In Python logging, when
a record from a child logger (e.g. `botocore.auth`) propagates to an ancestor, it
passes only the **filters of the ancestor's handlers**, **not the filters of the
ancestor logger**. Hence the only thing that can catch records from
`botocore.auth` and the like is the filter on the handler attached to the
`botocore` logger. The filter rewrites the record's final formatted message via
`mask_text` and clears `record.args` (so that re-formatting cannot bring the raw
values back). It also masks any attached exception traceback (`record.exc_text`,
which the handler's formatter appends from `exc_info`), so a secret embedded in an
exception message is redacted on that channel too, not only in the message.

### 3.3 The `http.client` wire dump is not handled

As in section 2, the `http.client` wire dump does not appear under the default
`--debug`. If a wire-dump output option is added in the future, the monkeypatch-
style redactor is reintroduced at that point (binding the global install/uninstall
to the ON/OFF of debug rather than to a per-operation scope).

## 4. Replacement notation (parity)

**Secret values are replaced with `***`**, following the one masking mechanism
aws-cli / boto3 do have: `botocore.httpsession.mask_proxy_url` (`mask = '*' * 3`,
replacing the userinfo with `***`). Non-secret structure (parameter names, the
credential scope, the proxy host) is preserved.

The only exception is the **AWS Access Key ID**: to allow distinguishing which
account issued a request, **the last 4 characters are kept** (`***MPLE`).

### 4.1 Target patterns

We pick up the URL/query forms, the plain `Header: value` line form (how
botocore's `AWSPreparedRequest` repr renders headers), and the **dict-repr form**
that s3transfer logs for a task's kwargs (`'X-Amz-Security-Token': '...'` /
`'SSECustomerKey': '...'`, including `b'...'`).

| Target | Example (input -> output) | Notation |
|---|---|---|
| Access Key ID (`AKIA`/`ASIA`, leading `Credential=`/`X-Amz-Credential=`, `AWSAccessKeyId=`) | `Credential=AKIA...MPLE/2026...` -> `Credential=***MPLE/2026...` | `***` + last 4. Scope (after `/` or `%2F`) preserved |
| Signature (`X-Amz-Signature=` / `Signature=` / `Signature:\n`) | `Signature=abcd...` -> `Signature=***` | `***` |
| SigV2 Authorization header (`AWS <access-key-id>:<signature>`, legacy `signature_version='s3'`) | `AWS AKIA...MPLE:frJI...` -> `AWS ***MPLE:***` | `***` (id tail-revealed) |
| Session token (`X-Amz-Security-Token=` / `X-Amz-Security-Token:` / `'X-Amz-Security-Token': '...'`, case-insensitive) | `'X-Amz-Security-Token': 'FQo...'` -> `'X-Amz-Security-Token': '***'` | `***` |
| SSO bearer token (`x-amz-sso_bearer_token` header, dict / colon form - the `sso GetRoleCredentials` request botocore logs at DEBUG; the token mints role credentials for every account/role the user can access) | `'x-amz-sso_bearer_token': 'aoal-...'` -> `'x-amz-sso_bearer_token': '***'` | `***` |
| sso-oidc token-endpoint bodies (`"accessToken"` / `"refreshToken"` / `"idToken"` / `"clientSecret"` in a CreateToken / RegisterClient `Response body:` line) | `"accessToken": "aoat-..."` -> `"accessToken": "***"` | `***` |
| STS / metadata-service response-body credentials (`<SecretAccessKey>`/`<SessionToken>` XML; quote-agnostic `SecretAccessKey`/`SessionToken`/`Token` key-value in JSON bodies and the metadata fetchers' dict reprs) | `<SecretAccessKey>wJal...</SecretAccessKey>` -> `<SecretAccessKey>***</SecretAccessKey>`, `'Token': 'FQo...'` -> `'Token': '***'` | `***`. `ContinuationToken` / `NextToken` are kept (quote-anchored key) |
| Signature-mismatch byte-dump (`<CanonicalRequestBytes>` / `<StringToSignBytes>` in a `SignatureDoesNotMatch` response body - a hex dump that reconstructs the signed headers) | `<CanonicalRequestBytes>78 2d 61 ...</CanonicalRequestBytes>` -> `<CanonicalRequestBytes>***</CanonicalRequestBytes>` | `***` (whole dump) |
| SSE-C customer key (`x-amz-server-side-encryption-customer-key` and the `copy-source` variant, dict / colon form, case-insensitive) | `'x-amz-server-side-encryption-customer-key': '<b64>'` -> `'...customer-key': '***'` | `***`. The `-md5` companion header (a non-secret hash) is kept |
| SSE-C customer key, boto3 parameter form (`'SSECustomerKey'` / `'CopySourceSSECustomerKey'` in a logged kwargs dict - s3transfer logs each task's `extra_args` at DEBUG *before* botocore base64-encodes the key) | `'SSECustomerKey': '<raw key>'` -> `'SSECustomerKey': '***'` (str or bytes repr) | `***`. The `...KeyMD5` companion is kept |
| Proxy URL userinfo | `https://user:pass@proxy:8080` -> `https://***:***@proxy:8080` | `***:***` (per `mask_proxy_url`) |
| `Proxy-Authorization` (dict / colon form, defensive) | `Proxy-Authorization: Basic ...` -> `Proxy-Authorization: ***` | `***` |
| `extra_secrets` (caller-specified actual values, at least `MASK_MIN_LEN`) | each occurrence -> `***` | `***` |

## 5. Wiring (library / CLI)

- **library**: `boto3_s3.set_stream_logger(name, level, ..., mask_secrets=True)` is
  exposed (its home module is `boto3_s3.masking`, re-exposed through the 3-layer
  export). Users get masked debug logs by default, with the same ergonomics as
  boto3.
- **CLI**: on `--debug`, `cli._enable_debug_logging` calls
  `set_stream_logger(name, DEBUG, stream=sys.stderr, mask_secrets=True)` for each
  logger in `_DEBUG_LOGGERS` (`boto3_s3` / `boto3_s3_cli` / `botocore` / `boto3` /
  `s3transfer`).
  The loggers are narrowed for noise control (attaching to root would flood the
  output with debug from unrelated libraries). **`urllib3` is excluded**: it only
  produces connection-pool logs and emits no credentials. Since the CLI exits
  after a single run, no teardown is needed.

## 6. Contract and degradation

- Masking takes effect when debug is enabled through a boto3_s3 entry point
  (`set_stream_logger` / CLI `--debug`). If a user enables it **directly** via the
  raw `boto3.set_stream_logger('botocore')`, that handler is not one we created, so
  it is not masked (we do not rewrite other parties' handlers). This is the line we
  draw as a boto3-faithful superset: "add a safe path, but do not break boto3's
  path."
- This is not a parity item (aws-cli / boto3 do not mask). It is independent of
  the exit code charter.

## 7. Tests

- `tests/lib/test_masking.py`: the replacement notation (`***` / `***MPLE`), proxy
  (contrasting output against `mask_proxy_url` for a match), the header-line and
  dict-repr forms and the `Signature:\n` form, the signature-mismatch byte-dump,
  the exception-traceback channel, `set_stream_logger` (handler attachment,
  toggling `mask_secrets`, masking of child-logger propagation), and being pure
  stdlib with no dependencies.
- `tests/cli/unit/test_debug_logging.py`: that `--debug` attaches a masking handler
  to each logger, and that botocore-form DEBUG records (the endpoint's
  `AWSPreparedRequest` repr, auth's `Signature:\n`) are all masked on stderr.
