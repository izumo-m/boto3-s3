# Session, config file, logging, path resolution, batch deletion

The public symbols that stand outside the `S3` object and its options, results
and filters: the tuned session factory and the timestamp parser it installs,
the AWS config-file reader and the sections it hands out, the masked
debug-logging entry point, the access-point path resolver, the batch-deletion
driver and its batch limit, and the package version. The narrative
counterparts are [`../library/s3-object.md`](../library/s3-object.md) for the
session and the config reader,
[`../library/logging.md`](../library/logging.md) for masked logging, and
[`../library/deleter.md`](../library/deleter.md) for the deleter.

## session

A factory returning a new `boto3.Session` whose clients parse response
timestamps through [`fast_parse_timestamp`](#fast_parse_timestamp). Installing
that parser is opt-in: the library installs it on the sessions this factory
builds, and installs or mutates one nowhere else.
`S3(session=boto3_s3.session())` is the construction that gets it; a
zero-config `S3()` keeps plain `boto3.client("s3")` semantics, so its
clients parse timestamps the way botocore does by default. Which parser a
client gets is decided by the session it was built from and by nothing else:
the decision reads no state from `boto3.DEFAULT_SESSION`, and boto3-s3 does not
mutate that session's parser factory. A zero-config `S3()`'s clients come from
`boto3.client("s3")`, which resolves the process-wide default session exactly
as `boto3.client` itself resolves it. Unrelated boto3 use elsewhere in the
process therefore does not change how boto3-s3 parses, and boto3-s3 does not
change how the application parses. Other entry points do bind to that session
deliberately — [`S3.aws_config()`](./s3.md#aws_config) reads the config file of
`boto3.DEFAULT_SESSION` when the `S3` was built without a session, so the
reader and the clients see one profile — but none of them touches parsing.

```python
def session(**kwargs: Any) -> boto3.session.Session
```

`kwargs` are forwarded verbatim to `boto3.Session`, so configuration semantics
(`profile_name`, `region_name`, credentials, and the rest) are boto3's, as is
the user-agent branding. The one keyword that cannot be passed is
`botocore_session`: this factory supplies its own fresh botocore session, and
passing one raises Python's duplicate-keyword-argument `TypeError`. Beyond
that, whatever `boto3.Session(**kwargs)` raises for its own arguments comes
straight out.

The parser default is registered on that fresh botocore session before any
client exists, so every client later created from the returned session
inherits it — including clients built for an
[`S3Storage`](./storage.md#s3storage) directly, and clients for other services.
The returned object is an ordinary `boto3.Session` with no other behavioral
difference.

This library registers the default only on sessions it creates here; it does
not retrofit a session handed to it. A caller who manages a botocore session of
their own can register the same default on that session's
`response_parser_factory` component.

## fast_parse_timestamp

botocore's `parse_timestamp` with a C fast path for the ISO 8601 timestamps S3
sends. [`session()`](#session) installs it as the response timestamp parser;
it is also callable directly and can be registered on a botocore session the
caller owns. Parsing dominates the CPU cost of large listings, where every
object's `LastModified` goes through this function.

```python
def fast_parse_timestamp(value: Any) -> datetime
```

`value` is whatever botocore hands its timestamp parser: an ISO 8601 string, an
RFC 822 header date, an epoch number. The fast path is taken only for a `str`
containing `-`; a trailing `Z` is rewritten to `+00:00` and the string goes to
`datetime.fromisoformat`. Everything else — and any string `fromisoformat`
rejects with `ValueError` — falls through to botocore's own `parse_timestamp`
untouched. The `-` requirement is a deliberate guard: a digit-only string such
as `"20200101"` is an epoch-seconds value to botocore, while Python 3.11+'s
`fromisoformat` would read it as a basic-format date, so the guard keeps such
input on botocore's interpretation on every supported Python.

The return is a `datetime`. For every input both paths accept, the two agree in
value; the difference is the tzinfo class — `datetime.timezone.utc` on the fast
path where botocore's fallback yields dateutil's `tzutc` — which compares,
subtracts and formats identically. A value neither path can parse raises out of
botocore's `parse_timestamp` unchanged; no library exception is involved.

## AwsConfig

A reader over the AWS config file — `AWS_CONFIG_FILE`, default
`~/.aws/config` — bound to one session. It is the building block behind
[`S3.aws_config()`](./s3.md#aws_config), which memoizes one per `S3` instance,
and it is usable on its own. Any value the file holds is reachable, not only
the `[s3]` transfer tuning: `region`, `output`, a `[services ...]` block, an
`[sso-session ...]` block, `[plugins]`. It is exported from the `boto3_s3`
root and from `boto3_s3.awsconfig`; importing that module loads no AWS SDK,
since boto3 and botocore are imported on the read path. The narrative is
[the guide](../library/s3-object.md#6-reading-the-aws-config-file).

The parse itself is botocore's — `Session.full_config` and
`Session.get_scoped_config` — so the file location, the nested `s3 =`
subsection syntax and active-profile resolution behave the way `aws` and
`boto3` behave. Every parsed value is a string, with one level of subsection
nesting, and nothing is validated at parse time: a value is interpreted only
when a getter fetches it, so `get_size("s3.multipart_chunksize")` reads
`"16MB"` as a byte count while `get_str` returns `"16MB"` verbatim. Sizes and
rates are 1024-based, matching `aws s3`, so a value written for `aws` yields
the same number here.

`full_config` merges the credentials file's profile values into the map, so a
key set only in `~/.aws/credentials` — secrets included — is reachable through
these getters, as `aws configure get` reaches it.

```python
class AwsConfig:
    def __init__(self, session: Any) -> None: ...

    @classmethod
    def from_session(
        cls, session: boto3.Session | None = None
    ) -> AwsConfig: ...

    def profile(self, name: str | None = None) -> ConfigSection: ...
    def services(self, name: str) -> ConfigSection: ...
    def sso_session(self, name: str) -> ConfigSection: ...
    def plugins(self) -> ConfigSection: ...

    def get_str(self, key: str, default: str | None = None) -> str | None: ...
    def get_int(self, key: str, default: int | None = None) -> int | None: ...
    def get_size(self, key: str, default: int | None = None) -> int | None: ...
    def get_rate(self, key: str, default: int | None = None) -> int | None: ...
    def get_bool(
        self, key: str, default: bool | None = None
    ) -> bool | None: ...
```

The direct constructor takes a **botocore** session, duck-typed on
`.full_config` and `.get_scoped_config()` rather than typed, which is what
keeps this module's import free of the SDK. `from_session` is the way to build
one from a boto3 session, and is what `S3.aws_config()` calls.

`from_session(session)` binds to the session given. `None` reuses
`boto3.DEFAULT_SESSION` when the process already has one — the session
`boto3.client()` resolves, and therefore the one a zero-config `S3()`'s clients
come from, so a `boto3.setup_default_session(profile_name=...)` binds the
reader and those clients to the same profile — and otherwise builds a plain
`boto3.Session()`, which gives the same default / `AWS_PROFILE` resolution
without installing a global default as a side effect.

**Section selectors.** Each returns a [`ConfigSection`](#configsection).
`profile()` with no name — the default — resolves the session's active profile
through botocore's `get_scoped_config` (`--profile` / `AWS_PROFILE`, else
`[default]`). `profile(name)` reads `[profile name]` out of the full config, or
`[default]` for the name `"default"`. `services(name)` reads `[services name]`,
`sso_session(name)` reads `[sso-session name]`, and `plugins()` reads
`[plugins]`. Every *named* selector is tolerant: an absent section yields an
empty `ConfigSection` whose getters fall through to the caller's defaults. The
active profile is the exception — a `--profile` / `AWS_PROFILE` that is set but
names no section in the file raises
[`InvalidConfigError`](./exceptions.md#invalidconfigerror).

**Active-profile shortcuts.** The five bare getters delegate to `profile()`,
so `cfg.get_size(key, default)` is `cfg.profile().get_size(key, default)` —
the profile is the default context, as it is for `aws configure get varname`.
Their contract, dotted keys and error behavior included, is
[`ConfigSection`](#configsection)'s. Each is overloaded on `default`: a
non-`None` default narrows the return to that type, while omitting it (or
passing `None`) admits `None`.

The reader holds no defaults table of its own — every getter takes the
caller's default — and the operations never consult it, so no operation
carries a hidden dependence on ambient `[s3]` settings
([`S3.aws_config()`](./s3.md#aws_config)). The `[s3]` values are readable here
all the same, as `cfg.profile().get_size("s3.multipart_chunksize", default)`;
applying `aws s3`'s own interpretation of that section — its defaults table,
its validation, its engine choice — is the `boto3-s3` command's job.

**Caching.** The full config is parsed on first access and held, and the
active profile's section is built once and held; both caches are per instance.

Raises [`InvalidConfigError`](./exceptions.md#invalidconfigerror): from
`from_session(None)` when building the default session fails, which is what a
set-but-missing `AWS_PROFILE` produces; from any selector or getter whose
underlying botocore parse fails, including a set-but-missing active profile and
an unparsable file; and from a value a getter cannot convert. Its parent is
[`ConfigurationError`](./exceptions.md#configurationerror), so one
`except ConfigurationError` covers all of them.

The same module also carries `SIZE_SUFFIX`, the 1024-based suffix table the
size and rate getters share with the `boto3-s3` command's own parser, and
`split_size_suffix`; neither is exported from the package root.

## ConfigSection

One resolved section of the config file, with typed getters over the string
tree botocore parsed for it — a profile, a `[services ...]` block, and the rest
of what [`AwsConfig`](#awsconfig)'s selectors return. It is exported from the
`boto3_s3` root and from `boto3_s3.awsconfig`. Constructing one directly over a
mapping is what those selectors do, and is open to a caller with a mapping of
their own; nothing else in the library consumes one.

```python
class ConfigSection:
    def __init__(self, data: Mapping[str, Any]) -> None: ...

    def get_str(self, key: str, default: str | None = None) -> str | None: ...
    def get_int(self, key: str, default: int | None = None) -> int | None: ...
    def get_size(self, key: str, default: int | None = None) -> int | None: ...
    def get_rate(self, key: str, default: int | None = None) -> int | None: ...
    def get_bool(
        self, key: str, default: bool | None = None
    ) -> bool | None: ...
```

`key` may be **dotted** to reach into a nested subsection:
`"s3.multipart_chunksize"` reads `multipart_chunksize` out of the section's
`s3` block. The walk follows as many components as the key has; botocore's
config parse produces one level of subsection.

`default` is what a missing key returns, and an absent named section is an
empty one, so a key read out of a `[services ...]` block that the file does not
contain returns the default too. Each getter is overloaded on `default` the way
`AwsConfig`'s shortcuts are.

**The getters.** `get_str` returns the string verbatim, uninterpreted.
`get_int` reads a plain integer. `get_size` reads a byte count: a bare integer,
or a magnitude carrying one of the 1024-based suffixes `kb`, `mb`, `gb`, `tb`
or their `kib` / `mib` / `gib` / `tib` spellings, matched case-insensitively —
so `"16MB"` is `16 * 1024**2` and `MB` means `MiB`. `get_rate` reads bytes per
second: a value ending `B/s` is a byte rate (`"10MB/s"`), one ending `b/s` a
bit rate divided by eight (`"800Kb/s"`), and a bare integer is bytes per
second — that `B` / `b` distinction is case-sensitive, where the magnitude
suffix is not. `get_bool` follows botocore's `ensure_boolean`. None of them
range-check what they return.

**Errors.** A present value a getter cannot convert raises
[`InvalidConfigError`](./exceptions.md#invalidconfigerror), so a config typo is
surfaced rather than silently defaulted. So does a key naming a whole
subsection instead of a value — for every getter, `get_bool` included.
Otherwise `get_bool` is the one getter that never rejects a value: following
`ensure_boolean`, any scalar other than a case-insensitive `"true"` reads as
`False`.

The section holds the mapping it was built from and nothing else; the file was
read by `AwsConfig`, so a getter here reads no file. It defines `slots`, so no
attribute can be added to an instance.

## set_stream_logger

Attaches a stream handler to a logger, mirroring `boto3.set_stream_logger`, and
by default attaches a secret-masking filter to that handler so the
credential-bearing records botocore and s3transfer emit at DEBUG are redacted
before they reach the stream. The design is
[`../../design/masking.md`](../../design/masking.md).

```python
def set_stream_logger(
    name: str = "boto3_s3",
    level: int = logging.DEBUG,
    format_string: str | None = None,
    *,
    stream: TextIO | None = None,
    mask_secrets: bool = True,
    extra_secrets: Iterable[str] = (),
) -> None
```

`name` is the logger to configure. The default is `"boto3_s3"` where boto3's is
`"boto3"`, so name the logger you actually want: the credential-bearing records
come from `"botocore"`, and `"s3transfer"` carries the SSE-C key in its task
kwargs. `level` is set on both the logger and the new handler.

`format_string` selects the record layout; `None` means boto3's own default,
`"%(asctime)s %(name)s [%(levelname)s] %(message)s"`. `stream` is the handler's
target; `None` means `logging.StreamHandler`'s default, `sys.stderr`.

`mask_secrets` decides whether the handler carries a
`boto3_s3.masking.SecretMaskingFilter`. `True`, the default, attaches one;
`False` attaches none, giving raw boto3-style output.

`extra_secrets` are literal values of the caller's own to redact. They are
applied after the built-in patterns have run, so a literal those already
partially rewrote is not matched again. A value shorter than
`boto3_s3.masking.MASK_MIN_LEN` (16 characters) is skipped, so a stray short
string cannot blank out swathes of the log.

The call configures process-global `logging` state and returns `None`. Each
call attaches a fresh handler to the named logger, so calling it twice for the
same logger duplicates that logger's output.

**Scope of the masking guarantee.** Masking is a property of the handler this
call attaches: everything that handler writes is redacted, and nothing else. It
is not a process-wide guarantee. Python logging delivers each record to every
handler on the chain independently, and a handler-side filter rewrites only its
own handler's copy — so a handler that *other code* attached to the same logger
formats the record itself, unmasked, and nothing here can reach it. Calling the
raw `boto3.set_stream_logger("botocore")` installs exactly such a handler.
Masked debug output comes from this entry point, or from the `boto3-s3`
command's `--debug`, which uses it; handlers installed by other code own their
own output. One further gap: the `http.client` wire dump is outside the
mechanism, and it appears only when `http.client.HTTPConnection.debuglevel` is
raised, which nothing in this project does.

**What is redacted.** Secret values are replaced with `***`, preserving the
surrounding structure — parameter and header names, the credential signing
scope, the proxy host. The one carve-out is the AWS Access Key ID, whose last
four characters are kept (`***MPLE`) so the issuing account stays
identifiable; that tail reveal applies to `AKIA` / `ASIA`-shaped ids only, and
an id in another shape in the same slot is masked entirely. Covered: access key
ids, signatures and the legacy SigV2 authorization header, session tokens
(including the S3 Express session token), SSO bearer tokens and sso-oidc token
bodies, credentials in STS and instance-metadata responses, the byte dumps and
echoed signature in a `SignatureDoesNotMatch` body, web-identity and SAML
request tokens, the MFA `TokenCode`, SSE-C customer keys in both header and
boto3-parameter form, proxy URL credentials and `Proxy-Authorization`. An
exception traceback attached to a record is masked on the same handler too.
`ContinuationToken` / `NextToken` and the `-md5` / `KeyMD5` companions of SSE-C
keys are deliberately left visible. The pattern-by-pattern table is
[`../../design/masking.md`](../../design/masking.md) section 4.1.

## S3PathResolver

Resolves access-point-shaped S3 paths to the real `s3://bucket/key` forms they
land in. An access point ARN or alias, an S3 on Outposts access point ARN, or a
Multi-Region Access Point ARN can hide the bucket a path really addresses, so
two different-looking paths can name the same object; resolving both sides
first lets a caller compare the real pairs (with
[`S3Storage.same_path`](./storage.md#s3storage)) before moving anything. This
is a port of the AWS CLI's own resolver, the machinery behind
`aws s3 mv --validate-same-s3-paths`. The AWS CLI's `from_session` constructor
is deliberately not ported: the clients come from the caller.

```python
class S3PathResolver:
    def __init__(self, *, s3control_client: Any, sts_client: Any) -> None
```

`s3control_client` answers `GetAccessPoint` and
`ListMultiRegionAccessPoints`. `sts_client` answers `GetCallerIdentity`, and is
consulted only for an access point *alias*, which carries no account id. Both
are keyword-only and required, and both are used lazily: a path with a plain
bucket name resolves to itself without touching either client. The
parameters are untyped so any boto3 client fits; build them with the region,
endpoint and profile wiring you want (the AWS CLI builds the s3control client
in the path's region and the sts client without one). The resolver keeps no
other state.

`S3.mv`'s same-path guard is textual only, so it does not see two access-point
paths that share an underlying bucket; this resolver is how a caller closes
that gap ([`operations/mv.md`](./operations/mv.md)).

### resolve_underlying_s3_paths(path)

```python
def resolve_underlying_s3_paths(self, path: str) -> list[str]
```

Every `s3://bucket/key` form `path` may land in. The bucket part decides the
route: a plain access point ARN and an Outposts access point ARN each resolve
through `GetAccessPoint` to one path; a `-s3alias` access point alias first
takes the account id from `GetCallerIdentity`, then resolves the same way; a
Multi-Region Access Point ARN is looked up by alias through paged
`ListMultiRegionAccessPoints` calls and fans out to one path per region of the
access point. A `--op-s3` Outposts access point alias matches none of those
routes and raises, since no API resolves it. Any other bucket part is not an
access point: the input string is returned unchanged, as the single element of
the list, with no API call.

A resolved element is rebuilt as `s3://<bucket>/<key>`, so a resolved path with
no key comes back with a trailing `/`. The unresolved element is the caller's
own string, `s3://` prefix or not.

Raises:

- [`ValidationError`](./exceptions.md#validationerror) for a `--op-s3` Outposts
  access point alias, which no API can resolve, and for a Multi-Region Access
  Point alias not found in the account after the listing is exhausted. Both
  carry the AWS CLI's wording.
- [`Boto3S3Error`](./exceptions.md#boto3s3error) and its subclasses —
  [`AccessDeniedError`](./exceptions.md#accessdeniederror),
  [`NotFoundError`](./exceptions.md#notfounderror),
  [`TransportError`](./exceptions.md#transporterror) — for a failing
  `GetAccessPoint`, `GetCallerIdentity` or `ListMultiRegionAccessPoints`, with
  the botocore error kept as `__cause__`.

Both of the above carry `operation="mv"`: the tag is fixed in this class rather
than taken as a parameter.

## has_underlying_s3_path

```python
def has_underlying_s3_path(path: str) -> bool
```

Whether `path`'s bucket part may resolve to a different real bucket: `True` for
the three ARN shapes [`S3PathResolver`](#s3pathresolver) resolves and for the
two access point alias suffixes, `-s3alias` and the Outposts `--op-s3`. Pure
string inspection — no client, no I/O — which is what gates the AWS CLI's "may
resolve to same underlying s3 object(s)" warning when validation is off.

A leading `s3://` is optional and stripped when present. The bucket part is the
ARN when the string starts with one, otherwise everything before the first `/`.
Nothing is validated: a string that is not an S3 path simply reports `False`,
unless its first segment happens to end in one of the two alias suffixes.

The same module, `boto3_s3.pathresolver`, also carries the string probes
`is_mrap_path`, `is_outpost_path` and `is_s3express_path`, which are not
re-exported from the package root. They name the target shapes whose signing
scheme botocore has to resolve for itself — asymmetric SigV4a for the first
two, `sigv4-s3express` for the third — so a caller that pins a symmetric
`signature_version` on its client (as the `boto3-s3` command does) can stand
that pin down for them.

## S3Deleter

Deletes S3 objects in batches on a background worker while the caller keeps
enumerating. Submitted entries are buffered; each full buffer is dispatched as
one `DeleteObjects` call for the keys XML 1.0 can carry, plus one
`DeleteObject` call per key it cannot. It is the machinery behind
[`rm`](./operations/rm.md) and
[`sync(delete_filter=...)`](./operations/sync.md), and is usable directly. The
rationale, the parity analysis and the error model in full are
[`../../design/deleter.md`](../../design/deleter.md).

```python
S3Deleter(
    storage: S3Storage,
    *,
    request_payer: str | None = None,
    on_result: ResultCallback | None = None,
    cancel_token: CancelToken | None = None,
    batch_size: int = S3_DELETE_BATCH,
    operation: str = "delete",
    capture_response: bool = False,
)
```

`storage` must be an [`S3Storage`](./storage.md#s3storage). Only its client and
its bucket address the requests — the key or prefix part is not consulted — but
the object itself is kept and rides every result as `src_storage`. Its client
is resolved in the constructor, so a client-build failure surfaces on the
caller's thread instead of in the worker, and it is held for the deleter's
lifetime: do not `storage.close()` before the deleter is closed.

`request_payer` is forwarded as the `RequestPayer` request parameter on both
the batch and the per-key route; `None` omits it.

`on_result` is a [`ResultCallback`](./results.md#resultcallback) receiving one
[`OpResult`](./results.md#opresult) per dispatched entry — see
[Results](#results) below for the fields and the threading rules.
`cancel_token` is a [`CancelToken`](./results.md#canceltoken); see
[Cancellation](#cancellation).

`batch_size` bounds the buffer's auto-flush point, one worker dispatch, and the
`DeleteObjects` call carrying that dispatch's XML-compatible keys. It must be
between 1 and [`S3_DELETE_BATCH`](#s3_delete_batch) (1000), which is AWS's own
per-call limit.

`operation` is the operation tag stamped on the exceptions the deleter raises
and on the per-key failures it reports; `rm` and `sync` put their own name
there. `capture_response` attaches the per-key response to each successful
result — see [Captured responses](#captured-responses).

Construction raises [`ValidationError`](./exceptions.md#validationerror) for a
`storage` that is not an `S3Storage` and for a `batch_size` outside 1..1000. If
`storage` has to build its own default client, that build can raise
[`ConfigurationError`](./exceptions.md#configurationerror) or
[`InvalidConfigError`](./exceptions.md#invalidconfigerror).

The rollup is exposed as three read-only properties. `succeeded` and `failed`
count keys reported so far; `first_error` is the first per-key failure
recorded, which is the sample a caller uses as `BatchError.__cause__`. All
three are approximate while the deleter is running — the worker writes them —
and final after `close()`. `S3Deleter` itself does not raise
[`BatchError`](./exceptions.md#batcherror); the caller builds one from the
rollup.

The batch limit itself is [`S3_DELETE_BATCH`](#s3_delete_batch).

### submit(info)

```python
def submit(self, info: FileInfo) -> None
```

Buffers one [`FileInfo`](./results.md#fileinfo), and auto-flushes when the
buffer reaches `batch_size`.
`info.key` is the **full object key** to delete, not a `Prefix`-relative one;
the rest of the entry rides through to its result untouched, so a richer
subtype such as an [`S3FileInfo`](./results.md#s3fileinfo) with its `etag`
arrives at `on_result` intact. Duplicate keys are passed through as submitted;
de-duplicating is the caller's concern.

Feed it a recursive scan. A non-recursive `S3Storage.scan` also yields a
`DIRECTORY`-kind entry per `CommonPrefixes` entry, and those are not object
keys.

Raises [`ValidationError`](./exceptions.md#validationerror) for an empty
`info.key` — S3 requires a key of length 1 or more, and a single empty key
would fail its whole batch — and for a deleter that is already closed. When the
auto-flush re-raises a previous batch's worker exception, this entry has
already been buffered: do not submit it again after catching that error.

### flush()

```python
def flush(self) -> None
```

Hands the buffered entries to the worker, `batch_size` entries per dispatch. A
no-op when the buffer is empty. Each dispatch first waits for the previous
batch — at most one batch is in flight, this is the backpressure point, and
this is where an unexpected worker exception (including one raised by
`on_result`) re-raises on the caller's thread. Entries not yet dispatched then
stay in the buffer; nothing is lost. The buffer can exceed `batch_size` after
such a re-raise, and while the token is cancelled, since the auto-flush of a
further `submit` then returns without dispatching (those entries are discarded
at `close()`). The loop re-chunks whatever it finds, so a single dispatch never
carries more than `batch_size` entries.

A `flush()` that finds the token cancelled stops dispatching and returns,
leaving whatever it has not dispatched in the buffer. Raises
[`ValidationError`](./exceptions.md#validationerror) on a closed deleter.

### close(\*, flush=True)

```python
def close(self, *, flush: bool = True) -> None
```

Flush, wait for in-flight work, shut the worker down. Idempotent. The flush is
skipped when `flush=False` or when the token is already cancelled. Afterwards
the rollup counters are final and `submit` / `flush` raise
[`ValidationError`](./exceptions.md#validationerror).

An unexpected worker exception — or one raised by `on_result` — re-raises here;
the deleter still ends up closed and the worker shut down either way. Entries
left in the buffer by `flush=False`, by a cancelled token, or by that re-raise
are abandoned without any result being emitted for them.

### Context manager

`__enter__` returns the deleter; `__exit__` calls
`close(flush=exc_type is None)`. Leaving the block normally flushes; leaving it
because of an exception abandons the unsent buffer while still waiting for the
batch already in flight, and a worker error raised at that point propagates
with the body's exception chained as its `__context__`.

Closing matters: the worker thread inherits daemon-ness from the thread that
created the deleter, so from an ordinary non-daemon thread an unclosed deleter
keeps the interpreter alive until the in-flight batch finishes.

### Threading

`submit`, `flush` and `close` belong to a single caller thread (single
producer). The work runs on one worker thread (thread name prefix
`boto3-s3-deleter`), spawned lazily at the first dispatch.

`on_result` is invoked from that worker thread. It must be fast and must not
raise. If it does raise, its own record has already been counted in the rollup,
the remaining entries of that batch get no result, and the exception surfaces
on the caller's thread at the next non-empty `flush()` or at `close()`.

### Results

One [`OpResult`](./results.md#opresult) per dispatched entry, emitted in
submission order within a batch. `transfer_type` is
[`TransferType.DELETE`](./results.md#transfertype) and `bytes_transferred`
stays `0`. `compare_key` is the entry's own `compare_key` when it has one and
its `key` otherwise. `src` is `s3://<bucket>/<key>`, `src_info` is the
submitted entry and `src_storage` is the constructor's `storage`; `dest`,
`dest_info` and `dest_storage` stay `None`. `outcome` is
[`OpOutcome.SUCCEEDED`](./results.md#opoutcome) or `OpOutcome.FAILED`, and a
failure carries its exception in `error`. Entries that are never dispatched
produce no record at all.

The library prints nothing. Beyond the results, the deleter logs to the
`boto3_s3.deleter` logger: batch dispatches, the per-key fallback, and
request-level and per-key failures at DEBUG, and unattributable response
entries as warnings ([`../library/logging.md`](../library/logging.md)).

### Captured responses

With `capture_response=True`, each **successful** key's result gains an
`extra_info["delete"]` slot holding that key's response in single-object
shape. On the batch route the batch is sent with `Quiet=False` so the response
also lists its successful `Deleted[]` entries, and each entry becomes a slot:
the entry minus its `Key`, with a `DeleteMarkerVersionId` renamed to
`VersionId` (which is how a single `DeleteObject` reports the same id), plus
the batch-wide `RequestCharged` when the response carries one. The per-key
fallback route uses its real `DeleteObject` response with `ResponseMetadata`
stripped. Either way the caller sees the same shape. Failed keys get no slot,
and with `capture_response=False` (the default) the batch is sent with
`Quiet=True` and no slot is produced at all.

One limitation: `DeleteObjects` reports per key spelling, so when the same key
was submitted more than once in one batch, all of that key's results share a
single slot — the response's last entry for that key wins.

### Errors

A per-key failure from the response's `Errors[]` is translated by S3 error
code, through the same table the request-level path uses: `AccessDenied`
becomes [`AccessDeniedError`](./exceptions.md#accessdeniederror);
`NoSuchBucket`, `NoSuchKey`, `NoSuchVersion` and `NotFound` become
[`NotFoundError`](./exceptions.md#notfounderror); `InternalError`, `SlowDown`,
`ServiceUnavailable` and `RequestTimeout` become
[`TransportError`](./exceptions.md#transporterror); anything else becomes
[`Boto3S3Error`](./exceptions.md#boto3s3error). The message is shaped like
botocore's `ClientError` string — `An error occurred ({Code}) when calling the
DeleteObjects operation: {Message}` — and the exception carries `operation`,
`bucket` and `key`.

An `Errors[]` entry that cannot be attributed to a submitted key, because it
has no `Key` or a key spelled differently from the one sent, is logged as a
warning and skipped. Successes on the batch route are synthesized as the
submitted keys minus the keys in `Errors[]`, so such a key may still be
recorded as a success; the warning is the trace that this happened.

A request-level failure — the `delete_objects` call itself failing — is
recorded as the failure of **every** key that call carried, with the same
translated exception, and the following batches still run. A wrong bucket name
therefore fails everything and shows up in the counts. Keys of the same
dispatch that took the per-key fallback route are unaffected by it: there, that
call's own success or translated exception is the key's result directly.

Anything the translation does not cover is a programming error: it is not
turned into per-key results but propagates from the worker and re-raises on the
caller's thread at the next non-empty `flush()` or at `close()`.

### Cancellation

A cancelled [`CancelToken`](./results.md#canceltoken) stops further batches
from being dispatched. A batch whose request has already started completes and
delivers its per-key results. Buffered entries that were never sent are
discarded without results, and
[`CancelMode.IMMEDIATE`](./results.md#cancelmode) may additionally cancel a
dispatched batch that has not started, whose entries likewise produce no
records. The deleter does not raise
[`CancelledError`](./exceptions.md#cancellederror) itself; the operations
driving it do.

### Relation to `aws s3`

`aws s3 rm` deletes one key per `DeleteObject` call and does not use the batch
API. The batching here is a wire-level deviation that is observably equivalent
for ordinary keys: deleting a key that does not exist succeeds either way, and
per-key success and failure are preserved. Two consequences follow. A run that
dies mid-way leaves different remote state, because the unsent buffer — up to
`batch_size - 1` entries — is abandoned, where the AWS CLI has already issued a
delete for everything it enumerated. And a key that XML 1.0 cannot carry
(control characters other than TAB/LF/CR, surrogate code points, `U+FFFE` /
`U+FFFF`) falls back to an individual `DeleteObject`, the route the AWS CLI
uses for every key, while the rest of the batch stays batched. Deleting a
specific `VersionId` is not provided, as `aws s3 rm` does not offer it either.

## S3_DELETE_BATCH

```python
S3_DELETE_BATCH = 1000
```

AWS's per-call limit on `DeleteObjects`, and the default `batch_size` of
[`S3Deleter`](#s3deleter). It is also the ceiling the deleter's constructor
validates `batch_size` against — a larger value raises
[`ValidationError`](./exceptions.md#validationerror) — so no `DeleteObjects`
call the deleter issues carries more keys than this. It is exported from the
`boto3_s3` root and from `boto3_s3.deleter`.

## \_\_version\_\_

```python
__version__: str
```

The installed version of the `boto3-s3` distribution, read from installed
package metadata on first attribute access and cached like every other name in
the package root — so reading it imports no submodule and no AWS SDK. In a
checkout where the distribution is not installed, it reads `"0.0.0+unknown"`.
The `boto3-s3-cli` distribution carries its own version, independent of this
one.
