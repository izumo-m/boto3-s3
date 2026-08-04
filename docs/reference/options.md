# Options

The value objects that configure a call: the object-shaping options
`cp` / `mv` / `sync` accept as keyword arguments, the `TransferConfig` that
tunes the transfer engine, the `ScanOptions` family a `Storage` enumeration
takes, and the mode enums those carry. The narrative treatment — which options
matter when, with worked examples — is in
[`../library/transfer-options.md`](../library/transfer-options.md); this page is
the per-field contract. Every symbol below is exported from the `boto3_s3` root.

`CancelMode`, `TransferType` and `FileKind` also live in `boto3_s3.types` but
are documented with the records they appear on, in
[`results.md`](./results.md).

## TransferOptions

The object-shaping and S3-request options shared by `cp`, `mv` and `sync`. It
is a `TypedDict` with `total=False`, so every key is optional and each is
passed as a keyword argument (`s3.cp(src, dest, acl="private")`); a caller
holding a dict unpacks it with `**`. The names are the snake_case form of the
corresponding `aws s3` options, and the library translates them into S3 API
PascalCase parameters internally.

An option that does not apply to the route being run is ignored rather than
rejected, with one carve-out: the request mapper for each route reads only the
keys that route sends, so `acl` on a download changes nothing, but
`no_overwrite` on a `cp` / `mv` download into a stream destination is refused
up front (see below). An *unknown* key is a different matter —
`cp` / `mv` / `sync` reject it eagerly with
[`ValidationError`](./exceptions.md#validationerror) before any work starts, so
a typo (`dry_run` for `dryrun`) never passes silently and never lets a `mv`
delete a source under a misread option.

```python
class TransferOptions(TypedDict, total=False):
    acl: str
    grants: Sequence[str]
    storage_class: str
    sse: str
    sse_kms_key_id: str
    sse_c: str
    sse_c_key: str | bytes
    sse_c_copy_source: str
    sse_c_copy_source_key: str | bytes
    metadata: Mapping[str, str]
    metadata_directive: str
    copy_props: CopyPropsMode
    annotation_copy_mode: AnnotationCopyMode
    cache_control: str
    content_type: str
    content_disposition: str
    content_encoding: str
    content_language: str
    expires: str
    website_redirect: str
    checksum_algorithm: str
    checksum_mode: str
    request_payer: str
    guess_mime_type: bool
    force_glacier_transfer: bool
    ignore_glacier_warnings: bool
    case_conflict: CaseConflictMode
    no_overwrite: bool
```

Because there are no field defaults, "the default" below means the behavior
when the key is absent. The request mappers gate on truthiness, so for the
options that become S3 request parameters an empty string or empty mapping has
the same effect as omitting the key. Four keys are read by presence rather than
truthiness and are called out individually: `guess_mime_type`, `copy_props`,
`annotation_copy_mode` and `case_conflict`.

`acl` is the canned ACL name, sent as `ACL` on the write request of an upload
or a copy. `grants` is a sequence of `permission=grantee` strings whose
`permission` is one of `read`, `readacl`, `writeacl` or `full`, becoming
`GrantRead` / `GrantReadACP` / `GrantWriteACP` / `GrantFullControl` on the same
write. Both default to unset. `grants` is validated per item at submission
rather than up front, matching the AWS CLI: a string with no `=`, or an
unrecognized permission, raises `ValidationError` from inside the running
operation — including under `dryrun`, which still runs the upload and copy
mappers — so it propagates out of the call instead of being aggregated into
[`BatchError`](./exceptions.md#batcherror).

`storage_class` is sent as `StorageClass` on an upload or copy write. When the
key is unset no `StorageClass` parameter is sent at all, leaving the service to
apply its own default.

`sse` and `sse_kms_key_id` become `ServerSideEncryption` and `SSEKMSKeyId` on
an upload or copy write. Both default to unset.

`sse_c` and `sse_c_key` are the customer-provided encryption pair for the
object being written or read: with `sse_c` set, `SSECustomerAlgorithm` and
`SSECustomerKey` ride on the upload, the copy, the download `GetObject`, and
the `HeadObject` that resolves a single download source. The key follows the
algorithm's presence — with `sse_c` set, whatever `sse_c_key` holds is sent
verbatim, an absent or empty value included, reproducing the AWS CLI's own
truthiness gate. `sse_c_key` is a `str` or raw `bytes`, like botocore's
`SSECustomerKey`.

`sse_c_copy_source` and `sse_c_copy_source_key` are the same pair for the
*source* object of an S3-to-S3 copy, sent as `CopySourceSSECustomerAlgorithm` /
`CopySourceSSECustomerKey` on the copy, and as the SSE-C headers of the
`HeadObject` that reads the copy source — the single-source resolution and the
copy-props head alike. They carry the same follows-the-algorithm rule and are
sent on no other route. Both default to unset.

`metadata` is the user-metadata mapping, sent as `Metadata`. On a copy,
supplying it without `metadata_directive` implies `MetadataDirective=REPLACE`.
`metadata_directive` is sent verbatim as `MetadataDirective` on a copy; setting
it also disables the entire copy-props mechanism, so `copy_props` then has no
effect at all — including the SDK gate that `CopyPropsMode.ALL` would otherwise
have to pass. Both default to unset.

`copy_props` selects which source properties a copy propagates
([`CopyPropsMode`](#copypropsmode)); it is read only on the S3-to-S3 copy
route and defaults to `CopyPropsMode.DEFAULT`. `annotation_copy_mode` selects
how a multipart copy under `copy_props=ALL` stages the source annotation
payloads ([`AnnotationCopyMode`](#annotationcopymode)); it too is read only on
the copy route and defaults to `AnnotationCopyMode.PRELOAD_MEMORY`. Both accept
the enum member or the equivalent string value the enum defines, and both treat
an explicit `None` as the default. Any other unrecognized value raises
`ValidationError` when the engine is constructed, before any item is submitted.

`cache_control`, `content_type`, `content_disposition`, `content_encoding`,
`content_language`, `expires` and `website_redirect` are the object header
properties, sent on an upload or copy write as `CacheControl`, `ContentType`,
`ContentDisposition`, `ContentEncoding`, `ContentLanguage`, `Expires` and
`WebsiteRedirectLocation`. All default to unset. On a copy, setting any one of
them makes even a single-request copy replace the remaining properties rather
than carry them over — see [`CopyPropsMode`](#copypropsmode).

`checksum_algorithm` is sent as `ChecksumAlgorithm` on an upload or copy write
and propagates to the multipart create, part and complete calls; an explicit
value beats the default checksum s3transfer would otherwise inject.
`checksum_mode` is sent as `ChecksumMode` on the download `GetObject` and on
the `HeadObject` that resolves a single download source. Both default to unset.
Neither is rejected on a route that does not send it — the library ignores it
there.

`request_payer` is sent as `RequestPayer` on the requests a transfer makes: the
object listing (the operation copies it onto
[`S3ScanOptions.request_payer`](#s3scanoptions)), the `HeadObject`, the upload,
download and copy calls, the copy-props tag and annotation calls, the
copy-props rollback delete, `mv`'s source `DeleteObject`, and the
`DeleteObjects` / `DeleteObject` calls `sync`'s delete side issues against the
destination. It defaults to unset. `ls` and `rm` do not take transfer
options and have a `request_payer` parameter of their own
([`operations/ls.md`](./operations/ls.md),
[`operations/rm.md`](./operations/rm.md)).

`guess_mime_type` defaults to `True`: an upload that has not set `content_type`
gets a `ContentType` guessed from the source entry's filename. `False`
suppresses the guess. It applies to the upload route only — a copy never
guesses, and a stream upload has no filename to guess from, so it is
unaffected either way.

`force_glacier_transfer` and `ignore_glacier_warnings` both default to `False`
and act on the archived-object gate that downloads and copies apply.
`force_glacier_transfer=True` lets the item through the gate and be attempted;
`ignore_glacier_warnings=True` turns the gate's warned skip into a silent one.
The gate's own conditions are with the operations that run it
([`operations/cp.md`](./operations/cp.md),
[`operations/sync.md`](./operations/sync.md)).

`case_conflict` selects what a download does when a key would collide with a
destination file differing only in case
([`CaseConflictMode`](#caseconflictmode)). It defaults to
`CaseConflictMode.IGNORE`, which runs no check at all. Like the other mode
options it accepts the enum member or its string value, and an unrecognized
value is rejected with [`ValidationError`](./exceptions.md#validationerror).

`no_overwrite` defaults to `False`. `True` means an object already present at
the destination is left alone rather than overwritten. How that is enforced,
and what it leaves behind, differs by route and operation. An upload or copy
carries `IfNoneMatch: "*"` on its write (on `CompleteMultipartUpload` for a
multipart write) and the server's `PreconditionFailed` becomes an
`OpOutcome.SKIPPED` record, not a failure; a `cp` / `mv` download to a local
destination is guarded by an existence check before the transfer starts and
reports the same skip. A download into a custom (open-route) backend has no
existence probe and overwrites, since the backend owns its key space, while a
`cp` / `mv` download into a stream destination rejects the option itself with
[`ValidationError`](./exceptions.md#validationerror) before any work — a stream
has no existing destination to guard. `sync` is decision-only: it sends no
conditional header at all and drops its update lane outright, so a key already
at the destination produces no record of any kind
([`results.md`](./results.md)).

Raises, over and above what each operation raises on its own:

- [`ValidationError`](./exceptions.md#validationerror) — an unknown key,
  `no_overwrite` on a `cp` / `mv` download into a stream destination, and an
  unrecognized `case_conflict` on any `cp` / `mv` / `sync` run whatever its
  paths (all before any transfer starts, which is not before every side effect
  — a recursive `cp` / `mv` or a `sync` creates its missing destination
  directory first); a malformed `grants` entry (in flight); an unrecognized
  `copy_props` or `annotation_copy_mode` on a copy (at engine construction).
- [`ConfigurationError`](./exceptions.md#configurationerror) — `no_overwrite`
  on an upload or copy whose SDK cannot express a conditional write, and
  `copy_props=CopyPropsMode.ALL` on an SDK without the annotations model
  (unless `metadata_directive` disabled the chain). Both are refused at engine
  construction; `sync` never reaches the conditional-write gate because it
  sends no conditional header. See [`../compatibility.md`](../compatibility.md).

## TransferConfig

Multipart thresholds, part size, concurrency, bandwidth and engine selection,
plus the library's own extras. It subclasses
`boto3.s3.transfer.TransferConfig`, keeping the base parameters' exact names,
order and semantics and appending the extra settings as keyword-only
parameters, so code written against boto3's class works unchanged. Pass one to
`S3(transfer_config=...)` to apply it to every call, or to a single
`cp` / `mv` / `sync` to override for that call ([`s3.md`](./s3.md),
[`operations/README.md`](./operations/README.md)). A plain
`boto3.s3.transfer.TransferConfig` is accepted everywhere a config is taken;
the library reads the extra fields with a `None` fallback, so nothing breaks
when they are absent.

```python
class TransferConfig(boto3.s3.transfer.TransferConfig):
    def __init__(
        self,
        multipart_threshold: int | None = None,
        max_concurrency: int | None = None,
        multipart_chunksize: int | None = None,
        num_download_attempts: int | None = None,
        max_io_queue: int | None = None,
        io_chunksize: int | None = None,
        use_threads: bool | None = None,
        max_bandwidth: int | None = None,
        preferred_transfer_client: str | None = None,
        *,
        target_bandwidth: int | None = None,
        should_stream: bool | None = None,
        disk_throughput: int | None = None,
        direct_io: bool | None = None,
        annotation_temp_dir: str | os.PathLike[str] | None = None,
    ) -> None: ...
```

Each base parameter is forwarded to boto3 only when it is not `None`, so
passing `None` is exactly equivalent to omitting the argument and lets the base
class supply its own default across every supported boto3. The resulting
defaults match `aws s3`: an 8 MiB multipart threshold, 8 MiB parts, a
concurrency of 10, and a 1000-deep download IO queue. That last one is the sole
default that leaves boto3's own value (100): it caps the read parts buffered
for the disk writer, so boto3's figure would give a slow disk a tenth of
`aws s3`'s readahead. Pass `max_io_queue=100` for boto3's ceiling — a plain
`boto3.s3.transfer.TransferConfig` keeps it, as it keeps all of boto3's
defaults. (Not under an explicit `preferred_transfer_client="crt"`: an
explicitly-set `max_io_queue` is a classic-only option there and fails
boto3's own CRT validation; the default needs no override on that lane.) `use_threads=False` selects a non-threaded executor for the
classic engine, as in boto3.

The library never reads the `[s3]` section of `~/.aws/config` — tuning comes
from this object alone. `S3.aws_config()` is the explicit way to lift those
file values into one ([`s3.md`](./s3.md)).

`preferred_transfer_client` selects the transfer engine — `"auto"` (the
default), `"classic"`, or `"crt"` — with boto3's own resolution semantics. It
is accepted on every supported boto3, including one whose base constructor does
not take the parameter. Two settings override the choice: an S3-to-S3 copy runs
on the classic engine regardless, since the CRT engine has no copy operation,
and `capture_response=True` on an operation forces the classic engine for that
run — a library-only flag with no `aws s3` equivalent. What the CRT engine
requires is in [`../compatibility.md`](../compatibility.md); the engine
resolution itself is specified in
[`../../design/transfer.md`](../../design/transfer.md) section 2.

The five extras are plain attributes rather than participants in the base
class's default-sentinel machinery, and each defaults to `None`.

`target_bandwidth` is the target throughput for the CRT engine in bytes per
second, becoming the CRT client's `target_throughput`. It does not cap the
classic engine — `max_bandwidth` is that engine's limit.

`should_stream`, `disk_throughput` and `direct_io` are the CRT file-I/O
settings (`disk_throughput` in bytes per second, converted to the gigabits per
second the CRT client takes). They are accepted and stored, and are passed to
the CRT client only where the installed s3transfer's `create_s3_crt_client`
accepts a `fio_options` parameter. No released pip s3transfer does, so on a
pip-installed s3transfer the three are inert
([`../compatibility.md`](../compatibility.md)).

`annotation_temp_dir` is the directory used by
`AnnotationCopyMode.PRELOAD_TEMPFILE`; `None` delegates to the operating
system's standard temporary-directory selection. The classic multipart-copy
path reads it whenever `copy_props=ALL` staging is set up, and uses it only
under `PRELOAD_TEMPFILE`.

All five are ignored by the classic engine except `annotation_temp_dir`, which
is a classic-path setting. An auto-selected CRT engine ignores the base
classic-only knobs; requesting `preferred_transfer_client="crt"` explicitly
while any of them is set is rejected when the CRT manager is built, surfacing
as [`ValidationError`](./exceptions.md#validationerror) that carries boto3's
own `InvalidCrtTransferConfigError` as its `__cause__`. Requesting `"crt"`
explicitly without a usable awscrt — absent or older than the version the CRT
path needs — or on an s3transfer that lacks the CRT surface propagates
botocore's `MissingDependencyException`, which stays outside the
`Boto3S3Error` hierarchy by design ([`exceptions.md`](./exceptions.md)).

## ScanOptions

The knobs `Storage.scan` and `Storage.scan_pages` take, as one immutable value
instead of a keyword list re-threaded through every override. This base holds
only what every backend honors; backend-specific knobs live on the
[`LocalScanOptions`](#localscanoptions) and [`S3ScanOptions`](#s3scanoptions)
subclasses, so one backend's options never leak into another's code. Which
subclass a backend uses, and how a scan's options are built from a storage's
own configuration, is the `Storage` contract in
[`storage.md`](./storage.md).

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ScanOptions:
    recursive: bool = False
    sort: bool = False
    filter: Callable[[FileInfo], bool] | None = None
    on_warning: Callable[[str], None] | None = None
    wait_on_interrupt: bool = True
```

It is frozen, so a modified copy is made with `dataclasses.replace`. That is
what the high-level operations do: each starts from
`storage.default_scan_options()` — which carries the storage's own
configuration — and overlays the run-level knobs below. A caller reaches these
fields directly only when calling `Storage.scan` itself; the built-in backends
reject an options object that is not their own subclass with a `TypeError`.

`recursive` (default `False`) selects whole-subtree enumeration over a single
level. The concrete meaning is the backend's: an S3 listing drops `Delimiter`
and yields every object, while a non-recursive one sends `Delimiter='/'` and
adds a directory entry per common prefix; the local walk descends or yields one
level ([`storage.md`](./storage.md)). `ls` passes the value the caller gave it;
`rm`'s enumerating paths always list recursively, the non-recursive folder
marker sweep included; a `cp` / `mv` source walk passes the call's `recursive`;
`sync` is always recursive.

`sort` (default `False`) requests entries in UTF-8 byte order of their
`compare_key`. `sync` sets it on any side it walks — its non-S3 side, an S3
listing already arriving byte-ordered — because its merge-join needs both sides
ascending; `cp` / `mv` / `ls` / `rm` leave it `False`, since each entry
transfers, lists or deletes independently. A backend that declares
`StorageCapability.SORTABLE_SCAN` must honor `sort=True`. The two built-in
backends ignore the flag, because the recursive listing each performs is
already in that order: S3's `ListObjectsV2` is byte-ordered (S3 Express
directory buckets excepted, whose unordered listings are why `sync` rejects
them) and the local walk sorts for parity with `aws s3`. A non-recursive S3
listing keeps its native shape instead, emitting a page's common prefixes ahead
of its objects; a one-level local listing is sorted like the recursive walk.
So a custom backend whose sort is expensive is the only
one that pays for it, and may stream natural order when `sort=False`.

`filter` (default `None`, meaning no filtering) is a per-entry predicate over
the entry's `FileInfo` that returns `True` to keep it. Each producer's
`scan_pages` applies it and returns already-filtered pages, and `scan` re-sieves
as a safety net for a backend that has not declared it filters
([`storage.md`](./storage.md)). `FileInfo.storage` is set before the predicate
runs, and a backend's listing stamps `FileInfo.compare_key` as part of its own
contract, so a [`GlobFilter`](./filters.md#globfilter) matches the relative key
directly and a richer predicate can reach the entry's backend. This slot
carries the item filter of `cp` / `mv` / `rm` and `sync`'s visibility layer,
where each side's listing is pruned independently before the pairs are formed —
which is why an entry a filter removed from the destination listing is not a
candidate for `sync`'s delete lane at all. The operations compose their own
conditions with the caller's predicate: an S3 transfer source additionally
drops zero-byte `/`-terminated folder markers, and `rm` adds its own sweep
condition. It runs on the enumeration worker thread, so keep it fast and
thread-safe.

`on_warning` (default `None`) receives the skip messages a backend's
enumeration emits — a broken symlink, an unreadable or special file — worded as
`aws s3` words them. `cp` / `mv` / `sync` wire it to the run's shared warning
rollup, so those skips surface as warnings on the run instead of vanishing;
`ls` and `rm` leave it unset. It runs on the enumeration worker, and because
`sync` walks both of its sides through one sink the two walks can invoke it
concurrently — keep it thread-safe.

`wait_on_interrupt` (default `True`) is the scan's Ctrl-C exit policy. `True`
makes the scan's teardown wait for a page pull already in flight, so no
enumeration worker survives the scan — what an application that may catch
`KeyboardInterrupt` and keep using the process needs. `False` lets a
`KeyboardInterrupt` unwind abandon the daemon prefetch worker rather than wait,
which matters when an in-flight network pull would otherwise hold the exit for
a full timeout; it suits an application that treats Ctrl-C as process-fatal.
The policy is scoped to `KeyboardInterrupt` alone — every other exit,
`SystemExit` included, reclaims fully. The high-level operations overlay this
field from `S3(wait_on_interrupt=...)`, where the application declares the
posture once ([`s3.md`](./s3.md)), so it reaches every scan they start; set it
here only when calling `Storage.scan` directly.

## LocalScanOptions

`ScanOptions` for a local filesystem walk — the option type `LocalStorage`
requires. It adds the walk's three source-config knobs plus one internal field.
The three are set on the `LocalStorage` constructor and seeded into every scan
by that backend's `default_scan_options`, so an application configures the walk
once on the storage rather than per operation; the high-level operations
preserve whatever the storage holds ([`storage.md`](./storage.md)).

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class LocalScanOptions(ScanOptions):
    follow_symlinks: bool = True
    detect_symlink_loops: bool = False
    enumerate_all_entries: bool = False
    storage: Storage | None = None
```

`enumerate_all_entries` (default `False`) selects the candidate enumeration
policy applied before `filter` runs. `False` is the `aws s3`-compatible
transfer view: recursively list transferable files, apply the special-file and
readability battery, and skip symlinks that are not to be followed. `True` is
the complete filesystem-entry view: the scanned directory itself,
sub-directories, symlinks, special files, and entries whose metadata is
readable even when their content is not. A `filter` can still remove any
candidate, but filtering a directory record is already too late to prune its
children — pruning before descent is a `LocalFileGenerator.finalize_children`
override ([`storage.md`](./storage.md)). Because the operations preserve this
setting, a caller that enables it owns the consequence: surviving entries reach
the operation and may fail, block, have device side effects, or be removed by
`mv` or by `sync`'s delete lane, under that operation's normal behavior.

`follow_symlinks` (default `True`) selects how a symlink is interpreted. In the
complete view, `False` returns the link itself as an lstat-based leaf, a
dangling link included, while `True` returns or descends its target as one
entry at that key; if the followed stat fails, the complete view warns and
falls back to the link's own lstat. In the transfer view, `False` skips the
link and `True` keeps the `aws s3`-compatible followed behavior.

`detect_symlink_loops` (default `False`) guards the recursive walk against
symlink cycles. With it — and with `follow_symlinks` — a directory that resolves
to one of its own ancestors is skipped at the first re-entry with a
`Symbolic link loop detected` warning through `on_warning`, instead of
descending until the kernel's loop limit ends the walk with a misleading
`File does not exist.` warning. It is a library extension that `aws s3` has no
counterpart for, which is why it is off by default; off, it costs no extra
`stat`.

`storage` (default `None`) is not a caller knob. `LocalStorage` threads its own
instance through this field so the shared, stateless walker can stamp each
entry's `FileInfo.storage` before the filter runs, without the walker holding a
back-reference to any one storage. It is `None` on a hand-built options object,
but `LocalStorage.scan_pages` replaces it with itself before walking, so the
walker stamps every entry a `LocalStorage` scan produces. `Storage.scan`'s
backstop fills `FileInfo.storage` afterwards only for entries a `scan_pages`
left `None` — a custom backend that drives the walker itself and leaves this
field unset.

## S3ScanOptions

`ScanOptions` for an S3 `ListObjectsV2` enumeration — the option type
`S3Storage` requires.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class S3ScanOptions(ScanOptions):
    page_size: int | None = None
    request_payer: str | None = None
    fetch_owner: bool = False
    prefix: str | None = None
```

`page_size` (default `None`) is the listing page size. `None` sends no
`MaxKeys` at all, exactly like an unset `--page-size` on `aws s3`, leaving the
server to page at its own default of 1000. `fetch_owner` (default `False`)
sends `FetchOwner=True` so the listing populates `S3FileInfo.owner`
([`results.md`](./results.md)), at the cost of per-page latency. Both are
`S3Storage` constructor settings seeded into every scan by that backend's
`default_scan_options`, so they are tuned once on the storage — pass a
configured `S3Storage` where an operation takes a location.

`request_payer` (default `None`) sends `RequestPayer` on the listing requests.
The operations set it from their own parameter or option of that name: `ls` and
`rm` from their `request_payer` argument, a transfer from
[`TransferOptions`](#transferoptions).

`prefix` (default `None`) overrides the listing anchor: the backend lists under
it as the `ListObjectsV2` `Prefix` and relativizes each entry's `compare_key`
to it, instead of using the storage's own key. `None` uses the storage's key. A
transfer sets it when its normalized listing prefix differs from the raw source
key — a recursive `cp` / `mv` / `sync` / `rm` source, where the plan appends a
trailing `/` — so that the storage instance the caller passed is the one
scanned, rather than a fresh one rebuilt from a URI. A custom `S3Storage`
subclass and its `scan_pages` override therefore survive the operation.

`page_size`, `request_payer` and `fetch_owner` are not validated here. Like
`aws s3`, they pass through to the service, which decides: `page_size=0` lists
nothing and a negative value fails with `InvalidArgument`.

## AnnotationCopyMode

How a multipart S3-to-S3 copy under `copy_props=CopyPropsMode.ALL` stages the
source object's annotation payloads. It is the value of the
`annotation_copy_mode` transfer option and has no `aws s3` counterpart; it has
no effect on single-part copies, which carry annotations server-side, or on any
route other than a copy.

```python
class AnnotationCopyMode(enum.Enum):
    PRELOAD_MEMORY = "preload-memory"
    PRELOAD_TEMPFILE = "preload-tempfile"
    DEFERRED = "deferred"
```

`PRELOAD_MEMORY` is the default and matches the AWS CLI: every payload is read
into memory before the multipart copy starts, so a source read that fails
leaves no destination object behind. The memory a single copy may hold until
completion is bounded by S3's aggregate annotation limit, multiplied by the
copies queued concurrently.

`PRELOAD_TEMPFILE` performs the same pre-copy reads — and so keeps the same
failure timing and the same no-destination-on-failure outcome — but stages the
payloads in one auto-deleting temporary file per copy instead. Only the payload
currently being read or written is held in memory.
[`TransferConfig.annotation_temp_dir`](#transferconfig) selects the directory.

`DEFERRED` uses s3transfer's native post-copy reads: the payloads are listed
and fetched after the multipart copy has completed. It avoids the preload
storage and the startup delay, at the price of a different failure state — a
source read that fails leaves the completed destination object in place.

## CaseConflictMode

What a download does when the key it is about to write differs only in case
from a file already at the destination, or from one still being downloaded. It
is the value of the `case_conflict` transfer option and applies to recursive
`cp` / `mv` downloads to a local destination and to `sync`'s new-entry
downloads to a local destination. The check exists for case-insensitive
destination filesystems but runs for every local destination without probing
the filesystem's case behavior, matching `aws s3`; a custom (open-route)
destination owns its own key space and runs no such check.

```python
class CaseConflictMode(enum.Enum):
    IGNORE = "ignore"
    SKIP = "skip"
    WARN = "warn"
    ERROR = "error"
```

`IGNORE` is the default: no check runs at all, so the later object simply
overwrites the earlier file. `SKIP` drops the conflicting entry; `WARN`
transfers it anyway. Each displays its own AWS CLI-worded message — `SKIP`
reports the entry as skipped, `WARN` reports it as downloaded despite the
conflict and points at the AWS documentation topic on case-insensitive
filesystems. Under both, that message is delivered as a display-only
`OpOutcome.NOTICE` record that enters no count
([`results.md`](./results.md)). `ERROR` fails the run, raising
[`ValidationError`](./exceptions.md#validationerror) from inside the pipeline.

A key already present at the destination in exactly matching case is not a
conflict — it is transferred, overwriting that file. What counts as a conflict,
including the in-flight set the check consults, is with the operations that run
the gate ([`operations/cp.md`](./operations/cp.md),
[`operations/sync.md`](./operations/sync.md)).

## CopyPropsMode

Which properties of the source object an S3-to-S3 copy propagates. It is the
value of the `copy_props` transfer option and is read only on the copy route.
A single-request copy carries metadata and tags across natively; a multipart
copy does not, which is what this option corrects for.

```python
class CopyPropsMode(enum.Enum):
    NONE = "none"
    METADATA_DIRECTIVE = "metadata-directive"
    DEFAULT = "default"
    ALL = "all"
```

`NONE` carries nothing over. `METADATA_DIRECTIVE` carries the metadata
properties — cache control, content disposition, content encoding, content
language, content type, expires, and the user metadata — but not tags.
`DEFAULT` is the default value and carries those plus tags. `ALL` carries
those, tags, and the object's S3 annotations, with multipart staging chosen by
[`AnnotationCopyMode`](#annotationcopymode).

Two interactions are worth stating. Setting an explicit property such as
`content_type` makes even a single-request copy replace, rather than copy, the
remaining properties, matching `aws s3`. Setting `metadata_directive` yourself
disables the whole mechanism, whatever this option says.

Under `DEFAULT` and `ALL`, a multipart copy whose tag set is too large for the
create request applies it after the copy succeeds; if that call fails, the
destination is deleted on a best-effort basis and the item is reported as
failed — except when the cleanup itself also fails, where the item is reported
successful with the destination left carrying no tags, as `aws s3` reports it.
`ALL` additionally
requires an SDK that models S3 object annotations — botocore 1.43.31 or newer
and s3transfer 0.19 or newer. On an older SDK the transfer engine refuses `ALL`
up front with [`ConfigurationError`](./exceptions.md#configurationerror), unless
`metadata_directive` has disabled the chain so that the annotations path is
never reached; every other mode degrades silently there
([`../compatibility.md`](../compatibility.md)).
