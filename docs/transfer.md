# Transfer engine (the design of `Transferrer` / `S3.cp` / `S3.mv`)

This document is the settled design of the byte-transfer layer for
`cp` / `mv` / `sync`. For the
CLI-side behavior, see [`cli.md`](./cli.md) section 5.7 (cp) / section 5.8 (mv) / section 5.9 (sync);
for the test structure, see [`testing.md`](./testing.md); for the exception
model, see [`exceptions.md`](./exceptions.md). `mv` is `cp` plus source deletion
(section 11). `sync` reuses this engine as-is for its transfer face (the pairing,
comparison, and deletion lanes live in [`sync.md`](./sync.md)).

## 1. Components

| module | role |
|---|---|
| `transferplan.py` | The transfer planner: the aws-cli `fileformat.py` counterpart (`plan_transfer` = `FileFormat.format` / `TransferPlan`, plus `find_dest_path_comp_key` as `item_paths`/`dest_for`). Sits above the backends and routes by concrete type (isinstance against `S3Storage`/`LocalStorage`); each side formats *itself* through the polymorphic `Storage.format` (`S3Storage` = aws's `s3_format` from the held bucket/key, `LocalStorage` = aws's `local_format` from the held abspath/raw form, the base = the open-route rule) and carries its own separator (`Storage.sep`). The per-backend string grammars also live on the backends (`S3Storage.split_bucket_key` and friends, `LocalStorage.relative_path`); `identify_type` (string classification) is the CLI's. The CLI and the library derive paths and key naming from the **same code** |
| `producers.py` | The per-info item builders and gates cp / mv / sync share: `TransferPlan` + listing entries -> `TransferItem`s, with the aws-cli item gates applied on the way (case-conflict, glacier, parent-reference, oversize; the open-route capability checks). Plain functions over the plan and the entry - no `S3` instance state - called by the orchestrator as `producers.upload_items(...)` etc. Kept out of `transfer.py` so the engine stays blind to `transferplan` (its backend knowledge stays narrow: the `LocalStorage` `isinstance` for the fsync barrier, section 11) |
| `requestparams.py` | Pure-function port of `TransferOptions` (snake_case) -> S3 API parameters (PascalCase) (aws-cli `RequestParamsMapper`). The format validation of grants is also done with aws's wording |
| `localstorage.py` | `LocalStorage` (the `Storage` ABC for a local path) plus `LocalFileGenerator`, the customizable directory walk it composes (boto3-s3's aws-cli `FileGenerator`). `LocalFileGenerator.list_files` reproduces aws-cli `FileGenerator.list_files` behavior (byte-order walk, warning rules) on an `os.scandir` engine - `d_type` types entries syscall-free and, where the platform allows (`have_dir_fd`), the directory is scanned through its fd so per-entry stats are dir-relative (`fstatat`; Windows falls back to path-based scandir). An app customizes it by subclassing `LocalFileGenerator` (public `list_files` / `should_ignore_file` / `entry_stat_result` / `scan_children` / `classify_child` / `stat_info` / `finalize_children` / `normalize_sort` seams, aws-cli names where a counterpart exists) and injecting via `LocalStorage(path, walker=...)`; the walk's source-config (`follow_symlinks` / `detect_symlink_loops` / `enumerate_all_entries`) is set on the same constructor; complete enumeration includes every metadata-readable native entry before filtering, and `LoopDetector` guards symlink cycles |
| `transfer.py` | `Transferrer`: the transfer engine proper that drives the classic / CRT transfer manager (the subject of this document). With `is_move` it deletes the source and reports MOVE (section 11). Engine selection is in section 2 / [`crt.md`](./crt.md) |
| `transferconfig.py` | The public `TransferConfig` = a subclass of boto3's that adds the CRT tuning fields and `annotation_temp_dir` ([`crt.md`](./crt.md) section 2) |
| `crtsupport.py` | CRT engine resolution (a faithful port of boto3 `boto3/crt.py` plus refinements). `should_use_crt` / `create_crt_transfer_manager` / lock. The design is in [`crt.md`](./crt.md) |
| `pathresolver.py` | A port of aws's `S3PathResolver` (resolves access point ARN / alias / MRAP to the real bucket; the s3control / sts client is injected). The building block for `mv --validate-same-s3-paths` (cli.md section 5.8) |
| `comparator.py` | sync's pairing and the building blocks for its decisions (`Comparator` / the `MergedPair` pair shapes / `PairFilter` / `compare_size_time` / combinators). The design is in [`sync.md`](./sync.md) |
| `S3.cp` / `S3.mv` / `S3.sync` in `s3.py` | orchestration: path classification -> pre-validation -> enumeration -> gates (glacier / parent-ref / dryrun) -> submit -> `BatchError` aggregation (cp / mv share `_run_transfer`). mv adds a same-path guard and `is_move` ahead of that. sync forks the enumeration into two streams, inserts pair decisions in between, and shares the per-info item builder and the gates with cp |

## 2. Engine selection and lifetime

- **The engine follows `TransferConfig.preferred_transfer_client`**
  (`'auto'` (default) / `'classic'` / `'crt'`). `Transferrer._get_manager`
  resolves it with the same semantics as boto3: either it uses the classic
  `s3transfer.manager.TransferManager` directly, or, if CRT is chosen,
  `crtsupport.create_crt_transfer_manager` ([`crt.md`](./crt.md)). **COPY
  (s3->s3) is unconditionally classic** - `CRTTransferManager` has no copy, and
  boto3 / aws-cli likewise pin s3->s3 to classic. `'auto'` faithfully
  reproduces boto3's behavior that "CRT can be auto-selected merely because
  awscrt is importable" (on a machine where `is_optimized_for_system()` is true
  it becomes auto-CRT, just as in boto3 - that fidelity is the whole point). We
  do not use `boto3.s3.transfer.create_transfer_manager` and hold manager
  creation ourselves in order to control subscriber wiring, lazy creation, and
  the IfNoneMatch patch.
- The public type is `TransferConfig` from `transferconfig.py`
  (a subclass of boto3's that adds the CRT tuning fields and
  `annotation_temp_dir`; the defaults are the same as aws-cli - 8 MiB
  threshold / 8 MiB chunk / concurrency 10). As in boto3, classic maps
  `use_threads=False` to `NonThreadedExecutor` (a determinization lever for
  tests). Classic-only knobs under CRT also follow boto3: auto-selected CRT
  ignores them, while an explicit `preferred_transfer_client='crt'` rejects
  them up front (`_validate_crt_transfer_config`). The overall design of CRT
  mode is in [`crt.md`](./crt.md).
- **`capture_response=True` forces the classic engine.** The write / read
  response capture ([`opresult.md`](./opresult.md)) rides the botocore client's
  `before-parameter-build` / `after-call` events (the `PutObject` / `CopyObject` /
  `CompleteMultipartUpload` write and the `GetObject` read), which the CRT data
  plane bypasses, so `_create_crt_manager` returns `None` (selecting classic)
  whenever the flag is set - logged as a `transfer engine: classic forced by
  capture_response` breadcrumb. A `_ResponseCapture` is then registered on the
  client together with the manager build (`prepare()`, below) and removed after
  the manager shuts down (after a clean drain no request is in flight during
  the change; an interrupted drain may unregister with stragglers still in
  flight - accepted, better than leaving the handlers on a longer-lived
  client); its handlers are per-instance
  bound methods, so register / unregister never disturb the application's or
  another run's handlers on a shared client. Being a library-only flag with no
  `aws s3` equivalent, the forcing has no parity impact.
- A `Transferrer` is **one instance with a single `TransferType` per cp / mv / sync
  run** (one run has a single byte direction). The client placement is: upload
  uses the dest client, download uses the src client, and an s3->s3 copy uses the
  dest client + `manager.copy(source_client=src client)` (a settled fact of the
  connection model).
- The manager is **built by `prepare()`, before the source enumeration
  starts** (the orchestrator calls it once, ahead of pulling the first item;
  the `--case-conflict` destination pre-scan, when that gate is armed, runs
  even earlier, outside the engine). Building the
  manager mutates the client's event registry (the `request-created.s3`
  handlers, and the capture handlers above), and botocore's events engine has
  no lock - so the mutation must happen-before the scan prefetch worker starts
  fetching pages with the same client (aws-cli likewise builds its
  TransferManager before the file generator starts). A dryrun never calls
  `prepare()`: no manager is built (the s3transfer module itself is imported
  regardless, by boto3 when a client is built - [`imports.md`](./imports.md)).
  It does still run the upload/copy request-parameter mapper for each item
  before reporting `DRYRUN`, matching aws-cli's deferred validation of options
  such as malformed grants. Validation performed only by an actual SDK submit
  remains skipped.
- **Backpressure is delegated to s3transfer**: the bounded submission executor
  (`max_submission_queue_size`, default 1000) blocks the submit thread when it
  is full, and the S3 request executor is separately bounded
  (`max_request_queue_size`). This is the same mechanism aws-cli relies on; we
  keep no in-flight window of our own.
- context manager: on normal completion it drains (waits for submitted
  transfers). On **any exception** - a fatal in mid-enumeration included - it
  shuts down **cancelling**, by delegating to the s3transfer manager's own
  `__exit__` (aws's actual path; measured live: a mid-listing fatal leaves
  every queued transfer unrun, prints only the one `fatal error:` line, and
  exits 1). The exception to that is `CancelMode.GRACEFUL`'s
  `CancelledError`, which drains by definition (graceful cancel = stop
  submitting, run accepted work; exceptions.md). `CancelMode.IMMEDIATE`
  additionally calls `cancel()` on the active top-level transfer futures.
  Futures already running may still finish - a cancelled-mid-flight transfer
  that completes reports its real outcome (s3transfer lets the completion
  win), while a revoked one reports one `CANCELLED` record
  ([`opresult.md`](./opresult.md)). Active futures are tracked only until
  their done subscriber fires, keeping cancellation tracking bounded by the
  manager's outstanding (queued + running) work rather than total item count. `KeyboardInterrupt` keeps the
  manager's direct best-effort cancellation path.

## 3. Subscriber composition (follows the order of aws-cli `s3handler`)

Because s3transfer resolves callbacks with `getattr` (duck typing), subscribers
are plain classes that do not inherit `BaseSubscriber` - the base class would
add nothing the `getattr` protocol uses.

1. `_ProvideSize` / `_ProvideETag` - provide every kind with the size and (if
   present, in quoted form) the etag up front (as in aws-cli). **In s3transfer
   0.17, even a copy fires a probe HeadObject against the source if either size
   or etag is missing**, so providing both is a precondition for wire parity.
   Because the CRT manager's future meta does not have `provide_transfer_size` /
   `provide_object_etag`, both are guarded with `hasattr` before being called
   (CRT probes the size itself; isomorphic to aws-cli `ProvideSizeSubscriber`;
   crt.md section 3).
2. `_Progress` - accumulates chunk deltas and forwards `TransferProgress`
   (absolute values) to `on_progress`. Emits one 0-byte notification on queueing
   (to track the in-flight set; the CLI does not print this).
3. download: `_DirectoryCreator` (creates the parent dir; tolerates EEXIST,
   otherwise fails with aws-cli's wording `Could not create directory ...`).
4. copy: the copy-props chain (section 4).
5. download: `_StampMtime` - stamps the source LastModified onto the downloaded
   file on success (section 5), registered before the mv deletion pair below
   (aws-cli's `ProvideLastModifiedTimeSubscriber` slot ahead of the
   DeleteSource family), so a failed source delete still leaves the mtime
   stamped.
6. mv download + `LocalStorage(fsync=True)` only: `_FsyncDest` (section 11) - a
   library-only durability barrier fsyncing the downloaded file (and its parent
   dir on POSIX) just before `_DeleteSource`, so a durability failure flips the
   future and the source is not deleted (off by default = aws parity).
7. mv only: `_DeleteSource` (section 11) - after the path-specific subscribers and just
   before `_Completion` (the same slot where aws-cli places the DeleteSource
   family ahead of the Done recorder).
8. `_Completion` - bridges the future's result to the rollup
   (locked succeeded/failed/warned/skipped + first_error) and to `OpResult`. Each
   record carries the item's listing entries (`src_info` / `dest_info`) and the
   run's side `Storage`s; on success `extra_info` takes `{"ETag": ...}` from
   `future.meta.etag` - the **source object's** ETag, which boto3-s3 provides up
   front from the listing / HEAD entry (`_ProvideETag`, item 1) for a copy and a
   download (s3transfer's own probe fills it from a source HeadObject only when
   it was not provided) but **not an upload** (an upload has no source ETag and
   the PutObject response is discarded), so an upload's `extra_info` is `None`.
   `capture_response` layers the written / read object's own response on top
   ([`opresult.md`](./opresult.md)).
9. `_ForgetFuture` (**always last**) - removes the settled top-level future
   from the run's bounded immediate-cancellation set.

Items 1-2 are value/callback-conditional (they register only when they have
something to provide or forward), items 3-7 route-conditional (download / copy
/ mv only), and the open route / case-conflict gate add unnumbered subscribers
of their own (`_CloseFileobj`, `_CaseConflictCleanup`) in the same order; the
always-present pair is 8-9 (the numbering is the slot order, not a single
chain that every transfer runs end to end).

`on_result` / `on_progress` fire **from s3transfer's worker threads** for
submitted transfers (with `use_threads=False`, on the calling thread), and the
non-submitting records - dry-run / skip / notice and some warnings - are
emitted inline on the submitting thread. Either way the contract matches the
deleter's: fast and non-raising.

## 4. copy-props (a port of aws-cli v2's correction)

A single CopyObject lets S3 natively carry over metadata and tags (the directive
default = COPY). **A multipart copy (at or above the threshold) does not carry
them over**, so aws-cli corrects for this with subscribers - we ported the same
chain:

| `copy_props` | subscribers | behavior |
|---|---|---|
| `none` | ReplaceMetadataDirective + ReplaceTaggingDirective + ExcludeAnnotationDirective | Carries nothing over (sets the directive to REPLACE). s3transfer excludes the directive from CreateMultipartUpload via a blacklist |
| `metadata-directive` | SetMetadataDirectiveProps + ReplaceTaggingDirective + ExcludeAnnotationDirective | Injects 7 properties (CacheControl / ContentDisposition / ContentEncoding / ContentLanguage / ContentType / Expires / Metadata) from the source HeadObject. Tags are not carried over |
| `default` (the default) | SetMetadataDirectiveProps + SetTags + ExcludeAnnotationDirective | The above + tags. GetObjectTagging -> percent-encode, and if it is ~2 KiB or under use the `Tagging` header on CreateMultipartUpload (aws-cli's wire shape - see the s3transfer adaptation below), otherwise PutObjectTagging after the transfer succeeds (**on failure, roll back by best-effort deleting the dest** and treat the transfer as failed) |
| `all` | SetMetadataDirectiveProps + SetTags + SetAnnotations | The above + S3 object annotations (aws-cli 2.35.6+). Single-part copies carry them server-side. Multipart copies stage reads according to `annotation_copy_mode`, then use s3transfer >= 0.19's native destination write path - see below |

- The single-shot path reuses the first HeadObject response
  (`TransferItem.head`) and **does not HEAD twice** (the same as aws-cli's reuse
  of `associated_response_data`).
- When there is an explicit property (`--content-type`, etc.), even the
  single-shot path flips the directive to REPLACE and injects the remaining
  properties from the source (aws-cli's rule).
- When `--metadata-directive` is specified, the entire chain is disabled (as in
  aws).
- **Double failure on the post-copy tagging rollback** (`default` / `all`,
  multipart only): if the post-copy `PutObjectTagging` fails and the
  best-effort rollback `DeleteObject` *also* fails, the rollback's own
  exception is left to propagate out of the subscriber's `on_done` uncaught -
  the same shape as aws-cli's `SetTagsSubscriber._on_success`. s3transfer runs
  each `on_done` subscriber in its own try/except and only logs an escaping
  one, so `future.set_exception` (the line right after the rollback call) never
  runs and the future is left in whatever state it already had - success,
  since the transfer itself completed. The item is therefore reported
  **SUCCEEDED (rc 0)**, with the destination left as the multipart copy
  produced it (no tags, since a multipart copy never carries them natively)
  rather than either fully tagged or deleted, and for `mv` the source-delete
  subscriber (which sits after this one in the chain) still runs and removes
  the source. This mirrors aws-cli's own double-failure outcome rather than
  being a gap to close - the exit-code charter (overview.md section 3) is what
  requires reproducing it, not just tolerating it.
- **Annotations** (aws-cli 2.35.6+, S3 Object Annotations): every mode short
  of `all` appends `_ExcludeAnnotationDirective` (aws-cli's
  ExcludeAnnotationDirectiveSubscriber), sending `AnnotationDirective=EXCLUDE`
  on the CopyObject so annotations are *not* carried (the server default is
  COPY). Two member-presence guards adapt what aws-cli does unconditionally:
  a botocore whose CopyObject lacks the parameter skips the injection
  silently (feature-level degradation, overview.md section 2 - copies behave
  like pre-annotations aws-cli), and a multipart copy skips it unless
  s3transfer blacklists the directive from CreateMultipartUpload (an older
  s3transfer would forward it there and fail; the multipart path carries no
  annotations anyway).
  `all` instead *copies* annotations. Single-part copies need nothing on the
  wire (server-side COPY default, same as aws-cli). Multipart staging is a
  library-only `annotation_copy_mode` with three values:

  - `PRELOAD_MEMORY` (default) paginates ListObjectAnnotations, then reads
    every payload into memory before CreateMultipartUpload. This matches
    aws-cli's timing and failure state: a source read failure leaves no
    destination. A single copy may retain up to
    [S3's 1 GiB aggregate annotation limit](https://docs.aws.amazon.com/AmazonS3/latest/userguide/annotations-overview.html)
    until completion, multiplied by concurrently queued copies.
  - `PRELOAD_TEMPFILE` performs the same pre-copy reads into one
    auto-deleting temporary file per copy. `TransferConfig.annotation_temp_dir`
    selects its directory; `None` uses Python's OS-standard temporary directory
    selection. Only the payload currently being read or written is held in
    memory, and the file is closed on success, failure, or cancellation.
  - `DEFERRED` preserves s3transfer's native behavior: list/get after the
    multipart copy completes. It avoids preload storage and startup delay, but
    a source read failure leaves the completed destination in place.

  Both preload modes hand a per-copy source-client adapter to upstream
  s3transfer >= 0.19, so its `_apply_annotations` still performs
  PutObjectAnnotation with `ObjectIfMatch` pinned to the new ETag. A partial
  destination write failure names succeeded/failed annotations and performs
  **no destination rollback**, matching aws-cli's AnnotationCopyError outcome;
  s3transfer additionally attempts a harmless AbortMultipartUpload after the
  upload has already completed. When the source HeadObject supplied a
  `VersionId`, preload list/get calls pin it like aws-cli. The boto3-s3 CLI
  always selects `PRELOAD_MEMORY` internally and exposes no additional CLI
  option.

  `copy_props=ALL` on an SDK that cannot honor the native write path is refused
  at `Transferrer` construction
  with a `ConfigurationError` (CLI rc 253); the probe behind that gate is
  public: **`annotations_copy_unsupported_reason(client)`** returns the
  rejection wording (naming botocore >= 1.43.31 / s3transfer >= 0.19 as the
  hint) or `None`, introspecting the model and s3transfer directly,
  version-agnostic. The gate only runs when `metadata_directive` is unset -
  an explicit `--metadata-directive` disables the whole copy-props chain
  (the bullet above) before the annotations path is ever reached, so
  `cp ... --metadata-directive REPLACE --copy-props all` is accepted at rc 0
  even on an SDK that cannot honor annotations, the same as aws-cli (which
  never touches `AnnotationDirective` on that path either).
- **Upstream s3transfer >= 0.19 adaptation** (aws-cli bundles a fork that
  predates this, so the port diverges from aws-cli's subscribers in two
  guarded spots): upstream 0.19 grew its own multipart copy-props handling -
  it strips the seven injected properties from CreateMultipartUpload unless
  `MetadataDirective` is REPLACE, and blacklists inline `Tagging` from the
  create call. The port therefore always sets `MetadataDirective=REPLACE` when
  injecting (every supported s3transfer drops the directive from the create
  call via the same blacklist, so the wire request is unchanged on older
  versions), and **removes `Tagging` from upstream's create blacklist at
  manager build** (`_allow_inline_mpu_tagging`, the same idempotent-mutation
  pattern as the IfNoneMatch patch): aws-cli's bundled table never blacklisted
  the plain header, so the small-tag set rides CreateMultipartUpload there -
  atomic, and failing at create (source kept) where tagging is denied. The
  `_mpu_inline_tagging_supported` probe guards the alignment: a future
  upstream that reshapes the table degrades to the post-copy PutObjectTagging
  fallback instead of silently dropping the header. s3transfer 0.19's own
  `TaggingDirective`-driven tag copy is deliberately not used: it has no
  destination rollback when the tagging write fails. Its post-complete
  tag/annotation hooks stay inert here (outside `all`'s deliberate
  `AnnotationDirective=COPY` ride above): `_apply_tags` writes only on a
  `TaggingDirective` of COPY, or REPLACE with a non-empty `Tagging` - `none` /
  `metadata-directive` leave REPLACE with no `Tagging`, which parses to an
  empty tag set and returns without a write, and `default` leaves no directive
  at all - and `_apply_annotations` fires only on the `AnnotationDirective=COPY`
  that `all` alone sends (the other modes send EXCLUDE).

## 5. download's incidental processing

- **mtime stamp**: a successful download stamps the source's `LastModified` with
  `os.utime` (the same timestamp as aws's result). A failure does not
  cancel the transfer but is **WARNED** (rc 2 family) - EPERM is re-worded with
  aws-cli's `set_file_utime` text (the util function: "attempting to modify the
  utime ..."), and the surrounding warning (`Skipping file <path>. Successfully
  Downloaded <path> but was unable to update the last modified time. <err>`;
  `path` appears twice) mirrors aws-cli's `ProvideLastModifiedTimeSubscriber` via
  `create_warning`.
- Parent-dir creation is the subscriber from section 3. The fact that s3transfer
  performs a filename-specified download via a temp file + rename is also
  identical to aws (parity is automatic because it is the same library).

## 6. streaming (`IOStorage` / `StdioStorage`)

`S3.cp` streams when one side is an `IOStorage` - a `Storage` (`storage.py` /
`iostorage.py`) wrapping a single in-hand file-like object - or the
`StdioStorage` convenience for `sys.stdin` / `sys.stdout`. `cp` accepts only a
`Location` (`str | PathLike | Storage`), so a caller wraps the stream
(`cp("s3://b/k", IOStorage(buf))`, `cp(IOStorage(buf), "s3://b/k")`); the CLI
wraps `-` in `StdioStorage`. The S3 side rides `s3transfer` as usual; the stream
side hands `s3transfer` the **binary** fileobj that `IOStorage.open` returns - a
text stream (`io.StringIO`, a text-mode file) is encoded on read / decoded on
write there, so the s3transfer boundary is always bytes (like `StreamingBody`).
The caller's stream is never closed by `IOStorage`.

- **Single item, no gates**: a stream is always a single transfer (the same as
  aws's stream path, which does not go through the generator). `recursive` is a
  `ValidationError` with aws-cli's wording (`Streaming currently is only
  compatible with non-recursive cp commands`), and an `IOStorage` on both sides
  is also rejected. The glacier / parent-ref gates are not run.
- **Stream option policy (follows aws-cli per option)**: a meaningless option is
  rejected, an additive one that degrades to a no-op is ignored. `recursive`
  (above) and `no_overwrite` on a streaming **download** raise (`no_overwrite is
  not supported for streaming downloads`) - a stream has no existing destination
  to guard, the same combinations aws-cli rejects. An upload stream keeps
  `no_overwrite` (IfNoneMatch). `filter` is silently ignored on a stream (a single
  object has nothing to filter; aws rc 0). `expected_size` applies to an upload
  stream and is ignored elsewhere.
- **`mv` with a stream**: an `IOStorage` may be the **destination** of a
  single-object move - the bytes land on the stream, then the S3 source is
  deleted (section 11) - but never the source (a move deletes its source, which
  a stream cannot be), and not a recursive one (a stream is a single endpoint);
  both raise `ValidationError`. The CLI does not expose this permissiveness:
  `mv` with `-` on either side keeps aws-cli's blanket rejection
  ([`cli.md`](./cli.md) section 5.8).
- **The key is verbatim**: the key of the S3-side `S3Storage` is used as-is.
  aws's naming where "in the form where the dest takes the source name
  (`s3://bucket` / `s3://bucket/pre/`) the literal `-` becomes the basename"
  (`pre/-`) is **derived by the CLI layer with transferplan.py before being passed in**
  (the library is permissive; the quirk is owned by the CLI).
- **upload**: hands s3transfer the fileobj from `IOStorage.open(key, "rb")` (the
  open ignores the key - a single endpoint). No ContentType guess (there is no
  filename). `expected_size` is a chunk-design hint for multipart
  (TransferItem.size) - if unspecified, the engine buffers up to the threshold to
  decide (s3transfer's non-seekable handling = the same implementation as aws).
- **download**: provides neither size nor etag -> s3transfer self-probes with
  HeadObject before GetObject (exactly aws's stream wire shape). Directory
  creation and the mtime stamp are not performed (section 5 is for path destinations
  only).
- The display renders the stream side as `-` (`src_display` / `dest_display`).
  The `BatchError` on failure is `1 of 1 transfers failed`.

## 7. Conditional overwrite prohibition (`--no-overwrite` = `no_overwrite`)

aws-cli attaches `IfNoneMatch: "*"` to uploads / copies and substitutes a
dest-existence check for download. We ported the same three faces:

- **upload / copy**: `requestparams` attaches `IfNoneMatch: "*"` to PutObject /
  CopyObject. For multipart it rides **only on CompleteMultipartUpload** and not
  on CreateMultipartUpload / UploadPart(Copy) (the same allocation as aws-cli's
  blocklist / COMPLETE_MULTIPART_ARGS).
- **PreconditionFailed (412) = silent skip**: `_Completion` judges the 412
  before rolling up the failure and drops it to **SKIPPED** (rc 0; aws-cli's
  `DoneResultSubscriber._on_failure` / `_is_precondition_failed`). When a
  multipart Complete hits a 412, s3transfer does an AbortMultipartUpload and then
  the same skip.
- **download**: at the enumeration stage, if `os.path.exists(dest)` then a silent
  skip (does not issue the request itself = the cp form of aws-cli's
  `_warn_if_file_exists_with_no_overwrite`).
- **Idempotent patch to pip s3transfer**: the pip build of s3transfer has not
  shipped `IfNoneMatch` in its allow-table (still absent as of 0.19; the fork
  bundled with aws-cli has it). On the first manager creation, `Transferrer`
  **idempotently appends** it to `ALLOWED_UPLOAD_ARGS` / `ALLOWED_COPY_ARGS` /
  the multipart blocklist / the COMPLETE list (harmless even if a future
  s3transfer adds native support).
- **SDK floor gate** (the overview.md section 2 degradation): the write op's S3
  model must define the `IfNoneMatch` input member (PutObject for uploads -
  CompleteMultipartUpload ships in the same botocore generation, so only
  PutObject is probed - and CopyObject for copies), which older botocore lacks - and an **upload** additionally needs s3transfer's
  create-multipart blocklist (`CREATE_MULTIPART_BLOCKLIST`, s3transfer 0.11):
  older s3transfer hands the full extra_args to CreateMultipartUpload, whose
  model has no `IfNoneMatch`, failing every multipart-threshold upload - a real
  pairing, since boto3 1.35.16+ pins s3transfer 0.10.x while its botocore
  already models the param. `Transferrer` rejects `no_overwrite` at
  construction with a `ConfigurationError` instead of failing deep in botocore
  with an opaque "Unknown parameter in input". The probe behind that gate is
  public:
  **`conditional_write_unsupported_reason(client, is_copy=...)`** returns the
  rejection wording (naming the minimum botocore / s3transfer as a hint) or
  `None` when supported - it introspects the client's model and s3transfer's
  table directly, version-agnostic. A
  compatible tool calls it *before* the pipeline to reproduce aws's up-front
  rejection (the CLI's `validate_no_overwrite_supported` maps it to rc 252);
  aws itself never gates here because it bundles a current SDK. Download
  and `sync` never send `IfNoneMatch`, so they stay usable on an old botocore.

## 8. The semantics of gates and warnings

- **warned counts warnings, not files**: a download that fails to stamp the
  mtime produces **two records**, SUCCEEDED and WARNED (the same as aws-cli's
  files_transferred / files_warned being independent counts). The rc derivation
  is `failed>0 -> 1, elif warned>0 -> 2` (the CLI layer).
- **glacier gate** (download / copy only): a `GLACIER` / `DEEP_ARCHIVE` object
  that is not restored (`Restore` has no `ongoing-request="false"`) is skipped +
  warned. `force_glacier_transfer` passes the gate through (**the S3 side rejects
  an unrestored object with InvalidObjectState** - and that is aws's behavior
  too). `ignore_glacier_warnings` is a silent skip (rc 0). **Because `Restore`
  does not ride on a recursive enumeration, even a restored object is skipped on
  recursion** - a faithful reproduction of aws-cli's
  `fileinfo.is_glacier_compatible`, not "a bug to fix."
- **parent-ref guard** (download only): an object whose compare key normalizes to
  `../` is skipped + warned (`File references a parent directory.`).
- **>48.8 TiB warning** (upload): as in aws-cli, **it only warns and still
  attempts the transfer** (to show S3's EntityTooLarge).
- walk warnings (unreadable / special file / broken symlink / invalid mtime) go
  from a walk's `ScanOptions.on_warning` to the run's shared warning sink
  (`Transferrer.warner`, a `Warner`), the same sink the engine's own warnings use
  (aws-cli's wording) - so the walk reports a warning without reaching into the
  transfer engine.
- **symlink-loop guard** (`detect_symlink_loops`, a **library extension**, default
  off so `cp` / `mv` / `sync` keep aws parity - `aws s3` has no such option):
  off, a symlink cycle descends until the kernel's `ELOOP` / path-length
  boundary ends it with aws's `File does not exist.` warning battery and the
  walk skips the directory, exactly like aws-cli (the boundary comes long
  before any `RecursionError` could); on
  (and with `follow_symlinks`), the recursive walk keeps an ancestor stack of
  `(st_dev, st_ino)` and skips a directory that resolves to one of its own
  ancestors with a `Symbolic link loop detected` warning. An ancestor stack (not
  a global visited set) still follows a legitimate diamond of links to the same
  external directory, like GNU `find -L`; it fails open (no `stat` identity →
  keep descending). Off costs no extra `stat` (a no-op detector). Both
  `detect_symlink_loops`, `follow_symlinks`, and `enumerate_all_entries` are the
  local walk's **source-config**:
  they are set on the `LocalStorage` constructor
  (`LocalStorage(path, follow_symlinks=…, detect_symlink_loops=…,
  enumerate_all_entries=…)`) and seeded into
  every scan by `default_scan_options`, not passed per operation (the CLI bakes
  `--follow-symlinks` into the storage it builds).
- **fd-relative walk boundary fallback**: the fast walk vets each entry through
  the owning directory's fd (`fstatat`/`openat`, `localstorage.py`'s
  `have_dir_fd` path), which re-anchors resolution one level at a time and so
  hides what aws-cli's own full-path `stat` would trip on - an ancestor
  symlink chain crossing `SYMLOOP_MAX`, or a path crossing `PATH_MAX` - and can
  admit a leaf the transfer then fails to open (rc 1) where aws warn-skips it
  at enumeration (rc 2, `File does not exist.`). Only near either boundary
  (`sym_depth` for a symlink leaf, the full path's length for any leaf - both
  floors sit well below the real OS limits, so an ordinary walk never reaches
  them) does the walk re-run the full-path warning battery
  (`LocalFileGenerator.crosses_full_path_boundary`) and drop a leaf it would
  warn away, so the two agree. On Windows (`have_dir_fd` false) the walk
  addresses entries by full path, but the scandir-cached stat still hides an
  over-`MAX_PATH` length on a host without long-path support
  (`LongPathsEnabled=0`, the default), so the readability probe is what fails
  there; the probe's failure path re-checks existence by full path and picks
  the wording aws-cli's exists-first battery emits (`File does not exist.`,
  never `not readable`) - same skip set, same wording, rc 2 as `aws.exe`
  (verified against the pinned aws-cli; pinned by
  `test_over_max_path_entries_warn_does_not_exist_like_aws`). The same
  re-check makes an entry that races away between its stat and the probe warn
  `File does not exist.` like aws's full-path battery would, on every
  platform. Two known residuals: `sym_depth` counts one
  hop per *followed symlinked directory* descended, not the actual number of
  links the kernel resolves for a chain of nested symlinks in one hop, so it
  can undercount relative to the real `SYMLOOP_MAX` counter in an adversarial,
  deeply-chained layout (the floor's margin below the OS limit is what keeps
  this from mattering in practice); and on Windows there is no `ELOOP`, so
  only the path-length boundary applies (confirmed by
  `test_symlink_cycle_descent_warns_like_aws` / `..._at_the_cycle_boundary_...`
  in `tests/lib/test_localstorage_walk.py`, which pin the warn-not-admit
  behavior without depending on the exact host-dependent depth).
- **case-conflict gate** (`case_conflict`, **S3->local recursive download only**,
  fires when mode != `ignore`; aws-cli's `_modify_instructions_for_case_conflicts`
  applies on a non-S3-Express source, and its S3 Express branch - reject
  `skip` / `error`, a standing warning for `warn` - is reproduced by the CLI
  before the mode reaches the library): aws builds this with the sync
  machinery (reverse-enumerating the dest + comparator), but the observed
  behavior reduces to two sets (confirmed by probing) -
  1. The compare key **exists at the dest in exactly matching case** -> always
     transferred (aws-cli assigns `AlwaysSync` to the at-dest entry = cp
     overwrites it; it also does not enter the conflict set).
  2. Otherwise, if "the lowercased key is in the set of downloads **still in
     flight**" or "`os.path.exists(dest)` is true (a case-variant on a
     case-insensitive FS)" -> conflict. `skip` = drop it, `warn` = let it
     through, both display aws-cli's wording as a **NOTICE** (below). `error` =
     a `ValidationError` (`Failed to download <src> -> <dest> because a file
     whose name differs only by case either exists or is being downloaded.`), an
     in-pipeline fatal (CLI rc 1). The in-flight set mirrors aws-cli's
     `CaseConflictCleanupSubscriber`: a key is added when its download is
     admitted and dropped when that download finishes, so a same-case twin is a
     conflict only while the first is still transferring - which means detection
     relies on the threaded, non-blocking submit; a fully synchronous executor
     (`use_threads=False`) would finish each download before the next is
     judged and never see the overlap. aws-cli's detection is racy the same
     way (its own warn wording concedes the race).
- **NOTICE** (`OpOutcome.NOTICE`): a display-only record that does not enter the
  counts. aws `uni_print`s the case-conflict message directly to stderr without
  going through the printer (not counted as warned, with no effect on rc, and
  **displayed even under `--quiet`**) - the CLI-side printer reproduces that
  behavior in its NOTICE branch.

## 9. checksum options

- `checksum_algorithm` (upload / copy) -> `ChecksumAlgorithm`. For multipart,
  s3transfer propagates it correctly to Create/Part/Complete. **An explicit
  specification beats pip s3transfer's default injection (`setdefault`)**.
- `checksum_mode` (download) -> GetObject's `ChecksumMode: ENABLED` (botocore
  verifies the response's checksum). What botocore can verify is the single
  (non-ranged) GET, whose response carries the checksum header; a ranged
  download gets no per-range checksum from S3, so its end-to-end validation is
  the known divergence recorded in section 10.
- The single-source HeadObject (`producers.head_single`, the download / copy point op)
  also `setdefault`s `ChecksumMode: ENABLED` when the client resolves
  `response_checksum_validation` to `when_supported` (the botocore default since
  checksums GA), mirroring aws-cli's filegenerator - so the HEAD's request shape
  matches aws even without `--checksum-mode`. An explicit `--checksum-mode` wins
  (`setdefault`), and an old botocore lacking the config knob just omits it.
- The computation of the CRT-family algorithms (`CRC32C` / `CRC64NVME` /
  `XXHASH64` / `XXHASH3` / `XXHASH128`) is delegated by botocore to `awscrt`.
  Because botocore auto-detects awscrt at import time, it is enabled with no
  extra configuration as long as awscrt is present. awscrt is **not a default
  dependency but an opt-in extra**: the library provides `boto3-s3[crt]`
  (delegating to boto3's own `boto3[crt]`), and the CLI's `boto3-s3-cli[crt]`
  delegates to that - the management of awscrt's version range rides on the SDK
  side. In an environment without awscrt, only the explicit specification of a
  CRT-family algorithm fails (the library is a per-item failure ->
  `BatchError`; the CLI is an in-pipeline `upload failed: ... Missing Dependency:
  Using CRC32C requires an additional dependency. ...` / rc 1; aws is rc 0 with
  the awscrt bundled in v2). Because the charter stipulates that awscrt-dependent
  features are "subject only when awscrt is present" (overview.md section 3), this
  failure does not count as a mismatch. On the download side, when the stored
  checksum is a CRT-family one with no local implementation, botocore silently
  skips verification (result and rc unchanged). **This delegation of checksum
  computation to awscrt is independent of the transfer engine selection (section 2)**:
  even with the classic engine, CRT-family algorithms are computed with awscrt.
  Whether to switch the transfer engine itself to CRT is decided by
  `preferred_transfer_client` (section 2 / [`crt.md`](./crt.md)), and SigV4 signing
  (cli.md section 4 - for symmetry, SigV4 is pinned to pure-Python) is not switched even
  when the CRT engine is in use.

## 10. Known divergence (invisible in the result; recorded only)

- When `--checksum-algorithm` is unspecified, the default integrity checksum is
  `CRC32` (pip s3transfer's `setdefault` injection). aws v2's bundled botocore
  injects `CRC64NVME`. Both are valid integrity checks and do not affect the
  transfer result or rc (stated explicitly in the awscli port's adaptation
  rules). When specified explicitly, the two agree.
- aws-cli's bundled s3transfer fork validates the full-object checksum of a
  **classic ranged download** (a single-object download at or above the
  multipart threshold, when the client resolves `response_checksum_validation`
  to `when_supported` - the default - or `ChecksumMode: ENABLED` is explicit):
  it computes a CRC per range while the body streams, combines the parts with
  awscrt's CRC-combine functions, and compares the result against the expected
  checksum taken from the single-source HeadObject before the temp file is
  renamed into place; a mismatch is `download failed ... did not match combined
  checksum` (rc 1) with no file left behind. pip s3transfer (0.19) has no such
  feature - it exists only in the fork - so our classic ranged download
  completes without end-to-end validation: corruption that slips past TLS/TCP
  integrity would land renamed and SUCCEEDED where aws fails. Every
  surrounding path is divergence-free: the non-ranged download is verified by
  botocore on both sides (section 9), the CRT engine passes
  `S3ChecksumConfig(validate_response=True)` on both sides so validation is
  the CRT client's own, identical by construction (crt.md section 6), and the
  listing-driven (recursive / sync) download is validated by neither side
  (ListObjectsV2 returns no checksum value; the combine applies only to the
  single-source point op, whose HeadObject response `head_single` already
  fetches with `ChecksumMode: ENABLED` - section 9 - so the expected value is
  on `S3FileInfo.head` should the validation ever be implemented). The trigger
  is narrow (an object stored as `ChecksumType=FULL_OBJECT` with a CRC value,
  at or above the multipart threshold) and the divergence is observable only
  under actual corruption, which no test lane can produce; adding the
  validation later is non-breaking (it only turns a corrupted success into a
  failure), so this is recorded as an accepted deviation until the feature
  reaches pip s3transfer.

## 11. mv (`is_move`: delete the source when the transfer succeeds)

`mv` goes through the same pipeline as cp for paths (upload / download / copy),
validation, and gates (sharing `S3._run_transfer`). The differences are the two
things `Transferrer(is_move=True)` adds and the same-path guard at the head of
`S3.mv`.

- **The reported transfer_type is `TransferType.MOVE` (`"move"`) on every path**. The path's
  kind remains in the submit branch and in the glacier wording (section 8 - equivalent
  to operation_name). With the same separation as aws-cli's `transfer_type='move'`
  relabeling, every record of result / progress / warning / dryrun calls itself
  `move`.
- **The `_DeleteSource` subscriber** (the position in section 3): performs the deletion
  only when the future succeeded. An upload removes the source through its
  `Storage.delete(info)` (keyed by `TransferItem.src_info`); `LocalStorage.delete`
  maps the OS error to the library taxonomy, preserving the wording aws's
  `move failed: ... [Errno 13] Permission denied: '<abs>'` form shows; download is
  a per-object DeleteObject against the manager's client, and copy against the
  **source-side client** (RequestPayer is passed through via
  `map_delete_object_params`). **A deletion failure flips the already-settled
  future to failed with `set_exception`** (s3transfer accepts an override after
  done - isomorphic to aws-cli's `DeleteSourceSubscriber`), and `_Completion`
  aggregates it as `move failed` (rc 1). The bytes have already arrived.
- **Durability barrier (`LocalStorage(fsync=True)`, a library extension; default
  off = aws parity)**: s3transfer finalizes a filename download with a temp-file
  write + `os.rename` and never fsyncs, so aws-cli deletes the durable S3 source
  while the downloaded bytes may still be only in the page cache - a crash between
  the two loses the move outright. When the download's destination `LocalStorage`
  opts in, the `_FsyncDest` subscriber (section 3, only on the S3->local `mv`
  download route, `item.dest_path is not None`) fsyncs the file - reopened by path,
  the rename left the inode unchanged - and then its immediate parent directory
  (POSIX only; a directory has no fsyncable handle on Windows, where the file
  fsync alone is the step) **before** `_DeleteSource` runs. A durability failure
  flips the settled future via `set_exception` (the same contract as
  `_CloseFileobj` on the open route), so `_DeleteSource` skips the delete and the
  S3 copy survives (`move failed`, rc 1). A freshly created intermediate directory
  is not walked back to its own parent (the common case downloads into an existing
  tree); the mtime stamp (`_StampMtime`, registered just before `_FsyncDest`)
  has already run, so the fsync covers the final metadata too. The CLI leaves
  this off to keep aws parity.
- Cases where the deletion does not run: dryrun (no submit at all), filter
  exclusion, skip (no-overwrite's 412 / dest already exists, the glacier gate),
  transfer failure, a `LocalStorage(fsync=True)` durability failure (above). For
  copy-props' post-copy tagging failure (the rollback of
  section 4), because `_SetTags` flips the future first, the source remains and only the
  dest is rolled back (aws-cli's order). A folder marker is not transferred, so it
  is not deleted either. An emptied local dir is left in place (as in aws).
- **The same-path guard** (always in `S3.mv`; the CLI also does it at the argv
  stage - cli.md section 5.8): apply `S3Storage.same_path` to the keyless-normalized URI -
  if it is an exact match, or a `/`-terminated dest + `basename(src)`
  concatenation matches src, then `Cannot mv a file onto itself: <src> - <dest>`
  (`ValidationError`). `--recursive` is also subject to this (a faithful
  reproduction of aws-cli's false positive; the CLI maps this `ValidationError`
  to rc 252). This is a string guard and does not look at
  identity across an access point - that resolution uses `pathresolver.py`
  (`S3PathResolver` + `has_underlying_s3_path`), which the caller uses with an
  injected client (the CLI's `--validate-same-s3-paths`; the library API
  deliberately has no such flag - under the connection model, the library does
  not implicitly create the s3control / sts client).
- **The mtime stamp precedes the deletion**, matching aws-cli's subscriber
  order (`ProvideLastModifiedTimeSubscriber` before
  `DeleteSourceObjectSubscriber`): `_StampMtime` is an independent subscriber
  registered before `_FsyncDest` / `_DeleteSource`, so a move whose deletion
  fails still leaves the downloaded file carrying the source mtime (a later
  sync then compares equal instead of re-downloading).

## 12. open route (custom backends: `opens3` / `s3open`)

A custom `Storage` (anything that is not an `S3Storage` / `LocalStorage`,
whatever its `scheme` string says; the built-in `IOStorage` / `StdioStorage`
are the degenerate single-entry case of this same seam, with their own stream
rules - section 6) transfers as one
side of `cp` / `mv` / `sync`, the other side always S3. `transferplan.plan_transfer`
classifies the pair by the endpoints' concrete types (the structural match in
`_paths_type`) into `opens3` (custom source -> S3, an
UPLOAD) or `s3open` (S3 source -> custom destination, a DOWNLOAD); `S3._run_transfer`
(cp / mv) and `S3.sync` route both. The S3 side rides `s3transfer` as usual; the
custom side's bytes move through its `Storage.open(key, mode)` - the same
primitive the stream path uses (section 6), generalized to a keyed, listable
backend. The CLI never pairs a custom backend, so the open route is library-only
and outside aws parity.

- **bytes via `open`, the S3 side via s3transfer**: `opens3` hands `s3transfer`
  the fileobj from `plan.src.open(key, "rb")` to upload; `s3open` hands it the
  fileobj from `plan.dest.open(key, "wb")` to download into. The transfer
  **closes every fileobj `open` returns** (`transfer._CloseFileobj`): for a
  writer that `close` flushes buffered writes (`Storage.open`'s contract), and a
  `close` (flush) failure flips the settled future via `set_exception` (a failed transfer).
  `s3transfer` itself never closes a caller fileobj (`CompleteDownloadNOOPTask`),
  so this is the sole close. An `IOStorage` hands back a close-suppressing view,
  so the caller's own stream is never closed (section 6).
- **the open key**: `""` is the location itself (a single source / destination),
  a non-empty key an entry beneath it - the same key regime as `delete`. A
  recursive item's key is its `compare_key`; a single *source* opens `""`. The
  destination key always derives from `transferplan.dest_for` exactly as for
  the built-in routes: `""` for a single non-directory destination, the
  adopted source name when the custom destination is `/`-terminated or
  `dir_op`.
- **upload shaping**: an `opens3` upload is shaped like a local one - the
  default ContentType guess reads the entry's key (its filename; the
  destination key for a single `""` source), and the >`5 GiB x 10000` oversize
  pre-warning fires too. Only a true stream (section 6) has no filename and
  skips the guess.
- **enumeration / single source**: `opens3` enumerates a recursive source
  through `Storage.scan` and resolves a single source through
  `Storage.get_fileinfo`. An unresolvable single source raises `The user-provided
  path <as_text> does not exist.` (`NotFoundError` with no `ClientError` cause,
  aws's missing-local-source wording; unlike the local up-front check it
  surfaces lazily, once the pipeline pulls the item); an empty recursive
  `scan` transfers nothing (rc 0, like an empty S3 prefix). `s3open`
  enumerates its S3 source exactly like the built-in download (recursive
  `ListObjectsV2` / single `HeadObject`, folder markers dropped; a keyless
  non-recursive source issues the same listing-and-match-nothing probe, so
  e.g. an `AccessDenied` stays observable).
- **capability gate** (`producers.require_open_capabilities`, before any bytes move):
  the custom side is pre-checked against `Storage.capabilities` and a missing
  contract method is a clear `ValidationError` naming the gap (not a deep
  failure). `opens3` needs `OPEN_READ` + (`SCAN` if recursive else
  `GET_FILEINFO`) + `DELETE` for `mv`; `s3open` needs `OPEN_WRITE`. This is
  structural capability, not runtime permission (a denied write / missing object
  stays a per-item execution error).
- **gates that do not apply**: a custom destination owns its own key space, so
  the local-filesystem destination gates do **not** run for `s3open` - no
  case-conflict scan (that gate stays scoped to `s3local`), no parent-reference
  check, no `no_overwrite` `os.path.exists`. Only the **source-side** glacier
  gate runs (the S3 source of an `s3open` download, section 8).
- **`no_overwrite`** (section 7): `opens3` keeps it - it rides `IfNoneMatch` on
  the S3 PutObject. For `cp`, `s3open` + `no_overwrite` is a **silent no-op**:
  the only download-side guard is the local-destination `os.path.exists` check,
  which a custom backend (owning its key space, with no existence probe wired)
  does not run. In `sync` it *does* work - sync lists the destination, so a
  destination-present pair is skipped without any probe.
- **dryrun**: enumerates and reports `DRYRUN` but does **not** call `open` on the
  custom side - opening a `"wb"` writer is itself a side effect, so a dry run
  leaves the backend untouched.
- **mv** (section 11): every upload deletes its source through that source's own
  `Storage.delete(info)` after each successful upload - the source listing entry
  rides on `TransferItem.src_info` (its `info.key` locates the object), and
  `Transferrer`'s `src_storage` carries the source `Storage` (local or custom
  backend). `s3open`'s source is S3, deleted with `DeleteObject` like any
  download `mv`. Data-safe in both: `_CloseFileobj` is
  ordered **before** `_DeleteSource` (section 3), so a failed transfer - or a
  failed writer `close` (flush) - leaves the source in place.
- **sync** ([`sync.md`](./sync.md)): the comparator is a sorted merge-join, so a
  custom side must declare `SORTABLE_SCAN` - an unsorted listing would manufacture
  phantom new/delete pairs and, with `--delete`, corrupt the destination. A
  dedicated gate (`producers.require_open_sync_capabilities`) requires `SORTABLE_SCAN` +
  `OPEN_READ` (an `opens3` source) / `OPEN_WRITE` (an `s3open` destination), plus
  `DELETE` when `--delete` removes orphans from an `s3open` custom destination
  (`opens3` orphans are S3, deleted without the custom side). `sync` passes
  `ScanOptions(sort=True)` to the custom side; the built-ins always sort and
  ignore the flag. Orphans are removed through `_SyncDeletes`: `DeleteObjects` for
  an S3 destination, the backend's `Storage.delete` for a custom one. The
  case-conflict gate is scoped to a `LocalStorage` destination (a custom one owns
  its key space). The transfer of each surviving pair reuses the `opens3` /
  `s3open` builders above (the dry-run-skips-`open` behavior included).
- **display**: the custom side renders through its `Storage.as_text()` (with the
  entry's relative key appended for a child); the S3 side as `s3://bucket/key`.
