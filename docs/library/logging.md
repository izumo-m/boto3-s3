# Debug logging and credential masking

`boto3` and `botocore` debug logs contain signed headers, signatures, session
tokens and SSE-C keys. `boto3_s3.set_stream_logger` mirrors
`boto3.set_stream_logger` but redacts them:

```python
from boto3_s3 import set_stream_logger

set_stream_logger("botocore")           # masked
set_stream_logger("botocore", mask_secrets=False)   # not masked
```

The first three arguments are boto3's — `name`, `level`, `format_string` — with
the same format string and `sys.stderr`. One default differs: `name` is
`"boto3_s3"` here, where boto3's is `"boto3"`, so pass the logger you mean. The
extra arguments are keyword-only and come after. Masking is on unless you turn
it off.

Neither `boto3` nor `aws s3` masks anything — this is extra protection the
library adds.

## 1. What is guaranteed, and what is not

**Masking is a property of the handler this library attaches** — the one
`set_stream_logger` installs, and the one the `boto3-s3` command installs for
`--debug`. Everything that handler writes is redacted.

**It is not a process-wide guarantee.** Masking applies to that handler's output
only, so:

- A handler **other code attached** to `boto3` / `botocore` / `s3transfer`
  formats the record itself, unmasked. Nothing here can reach it.
- Calling the raw `boto3.set_stream_logger("botocore")` yourself installs such a
  handler, so its output is **not** masked.

Getting masked output means going through this entry point, or the command's
`--debug`. Configuring those loggers around the library is not a supported way
to get redacted output.

One more gap: the `http.client` wire dump is not redacted. It does not appear
under normal debug logging, so in practice it does not arise — but if you turn
it on yourself, it is outside this mechanism.

Masking is also a global, per-process setting rather than something configured
per `S3` instance. Call it once.

## 2. What gets masked

Secrets are replaced with `***`, preserving the surrounding structure so the log
stays readable — parameter names, the credential scope, and the proxy host all
survive.

The one exception is the **access key id**, whose last four characters are kept
(`***MPLE`) so you can tell which account issued a request. That applies to
`AKIA`/`ASIA`-shaped ids; an id in another format — MinIO's `minioadmin`, for
instance — is masked completely.

Covered: access key ids, signatures and the legacy SigV2 authorization header,
session tokens, SSO bearer tokens and the sso-oidc token bodies, credentials in
STS and instance-metadata responses, the byte dumps in a
`SignatureDoesNotMatch` response, web-identity and SAML assertions, the MFA
one-time code in an STS request, SSE-C
customer keys in both header and parameter form, proxy URL credentials, and
`Proxy-Authorization`. Exception tracebacks written through the same handler are
redacted too, so a secret inside an exception message does not escape that way.

**Deliberately kept**: `ContinuationToken` and `NextToken`, which are pagination
state rather than credentials, and the `-md5` / `KeyMD5` companions of SSE-C
keys, which are hashes.

To redact your own values, pass them:

```python
set_stream_logger("botocore", extra_secrets=[my_token])
```

They are applied after the built-in patterns, so a value already rewritten is
not masked twice.

## 3. The library never prints

Operations write nothing to stdout or stderr. Progress, per-item outcomes and
warnings all arrive through the callbacks described in
[`results.md`](./results.md); rendering them is the application's job — which is
exactly what the `boto3-s3` command does with them.

Components that have something to say beyond their results log it under their
own module name, such as `boto3_s3.deleter`. No handler is attached at import
time, so nothing is emitted until you ask for it.
