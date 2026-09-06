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
| `mimetable.py` | Generated data: CPython 3.14's built-in `mimetypes` tables, the ones the official `aws` distribution guesses an upload's `ContentType` from (it is frozen against that interpreter). `transfer.py` builds its own `mimetypes.MimeTypes` from them - plus the same live overlays `mimetypes.init` applies, the Windows registry and the existing `knownfiles` - so the guess is the shipped distribution's on every supported interpreter instead of the running one's (3.10 knows no `.php`, 3.15 re-types `.texinfo`), the same pin the CLI applies to argparse behaviour that moved between releases ([`cli.md`](./cli.md) section 2) |
| `localstorage.py` | `LocalStorage` (the `Storage` ABC for a local path) plus `LocalFileGenerator`, the customizable directory walk it composes (boto3-s3's aws-cli `FileGenerator`). `LocalFileGenerator.list_files` reproduces aws-cli `FileGenerator.list_files` behavior (byte-order walk, warning rules) on an `os.scandir` engine - `d_type` types entries syscall-free and, where the platform allows (`have_dir_fd`), the directory is scanned through its fd so per-entry stats are dir-relative (`fstatat`; Windows falls back to path-based scandir). An app customizes it by subclassing `LocalFileGenerator` (public `list_files` / `should_ignore_file` / `entry_stat_result` / `scan_children` / `classify_child` / `stat_info` / `restat_leaf` / `finalize_children` / `normalize_sort` seams, aws-cli names where a counterpart exists) and injecting via `LocalStorage(path, walker=...)`; the walk's source-config (`follow_symlinks` / `detect_symlink_loops` / `enumerate_all_entries`) is set on the same constructor; complete enumeration includes every metadata-readable native entry before filtering, and `LoopDetector` guards symlink cycles |
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
  boto3 / aws-cli likewise pin s3->s3 to classic. This bullet is the library
  layer's engine resolution in full; [`crt.md`](./crt.md) points here rather
  than restating it, while [`cli.md`](./cli.md) section 8 covers the CLI's
  separate, earlier resolution (it settles the value before handing it over). `'auto'` faithfully
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
/ mv only), and two gates add unnumbered subscribers of their own at aws-cli's
slots: the open route's `_CloseFileobj` right after item 3 (before the mv
deletion pair, so a failed flush keeps the source) and the case-conflict gate's
`_CaseConflictCleanup` right after item 7 (aws-cli registers
`CaseConflictCleanupSubscriber` after the DeleteSource family, so a key stays
"in flight" for the whole of its download's teardown). The always-present pair
is 8-9 (the numbering is the slot order, not a single chain that every transfer
runs end to end).

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
- **Where that tagging write sits relative to the annotation writes**
  (`all`, multipart, a tag set too large for the create header - recorded, not
  fixed): aws-cli writes both from `on_done` subscribers registered tags-first,
  so its PutObjectTagging goes out ahead of its PutObjectAnnotation calls,
  while here the annotations ride upstream s3transfer's native write path
  inside the CompleteMultipartUpload task, which finishes before any
  subscriber's `on_done` - so the tagging write goes out last. The same
  requests are sent, and the console lines and the exit code are the same; only
  the order differs, and so does the destination in exactly one corner. When
  the tagging write fails and the rollback delete succeeds, both tools leave no
  destination object. When the rollback delete fails too (the bullet above,
  rc 0 on both), the surviving object carries the copied annotations here and
  carries none on aws-cli, which had not written them yet - aws-cli's
  annotations subscriber sees a future already settled with the tagging failure
  and writes nothing. Moving the write point would mean giving up upstream's
  native path, which is what carries the annotations at all; recorded for the
  reader in [`aws-differences.md`](../docs/cli/aws-differences.md).
- **Annotations** (aws-cli 2.35.6+, S3 Object Annotations): every mode short
  of `all` appends `_ExcludeAnnotationDirective` (aws-cli's
  ExcludeAnnotationDirectiveSubscriber), sending `AnnotationDirective=EXCLUDE`
  on the CopyObject so annotations are *not* carried (the server default is
  COPY). Two member-presence guards adapt what aws-cli does unconditionally:
  a botocore whose CopyObject lacks the parameter skips the injection
  silently (feature-level degradation, compatibility.md - copies behave
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
  **no destination rollback** - aws-cli's AnnotationCopyError outcome, and its
  wording too: the reported sentence is rebuilt to aws's
  (`_align_annotation_copy_error`), because upstream keeps the per-name
  outcomes only as formatted text inside the exception it raises, so they are
  recorded as each PutObjectAnnotation returns or fails and re-worded from
  that record. The exception type upstream raises is reused, so the taxonomy
  translation and the exit code are unchanged; only the text moves.
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
  guarded spots, realigns two of upstream's tables and one of its methods at
  manager build - the same idempotent-mutation pattern as the IfNoneMatch
  patch - and flips one request parameter per copy):
  upstream 0.19 grew its own multipart copy-props handling -
  it strips the seven injected properties from CreateMultipartUpload unless
  `MetadataDirective` is REPLACE, and blacklists inline `Tagging` from the
  create call. The port therefore always sets `MetadataDirective=REPLACE` when
  injecting (every supported s3transfer drops the directive from the create
  call via the same blacklist, so the wire request is unchanged on older
  versions), and **removes `Tagging` from upstream's create blacklist at
  manager build** (`_allow_inline_mpu_tagging`): aws-cli's bundled table never
  blacklisted
  the plain header, so the small-tag set rides CreateMultipartUpload there -
  atomic, and failing at create (source kept) where tagging is denied. The
  `_mpu_inline_tagging_supported` probe guards the alignment: a future
  upstream that reshapes the table degrades to the post-copy PutObjectTagging
  fallback instead of silently dropping the header. Two more spots cover paths
  the chain does not own:
  **a multipart-bound explicit COPY directive is flipped to REPLACE in the
  request parameters** (`Transferrer._submit_copy`): an explicit
  `--metadata-directive COPY` disables the whole chain, and with it the
  REPLACE that would have satisfied upstream's guard, so upstream would strip
  the caller's `--content-type` and friends from CreateMultipartUpload where
  aws-cli's fork - tableless - passes them through. Every supported s3transfer
  blocklists `MetadataDirective` from the create call, so the flip changes no
  wire request, and the single-part CopyObject keeps the real COPY. Upstream's
  `PRESERVED_METADATA_FIELDS` table itself is deliberately not emptied: the
  table patches are process-shared, and emptying this one would flip a plain
  s3transfer caller's *default* multipart-copy behavior in the same process,
  where the two surviving patches stay inert until a caller passes the
  affected argument. The multipart decision is predicted with upstream's own
  comparison (`size >= multipart_threshold`, the annotation gate's pattern); a
  copy whose size was never provided skips the flip - upstream then sizes it
  with its HeadObject probe and applies its own preservation, a path aws-cli
  cannot produce (it always provides the size).
  **`ChecksumAlgorithm` is removed from `PUT_OBJECT_ANNOTATION_ARGS`**
  (`_align_annotation_put_args`): `all`'s annotation writes ride upstream's
  native path, which would forward the copy's checksum algorithm onto every
  PutObjectAnnotation, while aws-cli maps only `RequestPayer` there.
  **The annotation write path is wrapped so a partial failure reads as aws's**
  (`_align_annotation_copy_error`): upstream formats the succeeded and failed
  names straight into its own exception message and keeps no structured record
  of them, so the destination client is stood in for during that one call to
  capture each outcome, and the exception is re-raised with aws-cli's
  `AnnotationCopyError` sentence. Like the table patches it is
  process-shared and stays inert for a plain s3transfer caller - the wrapping
  and the re-wording happen only for a copy carrying this module's own
  annotation subscriber - and both lookups are `getattr` guards, so an upstream
  that reshapes or drops the private write path degrades to upstream's own
  wording rather than failing at manager build.
  s3transfer 0.19's own
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
  `os.utime`, truncated to whole seconds - the same timestamp as aws's result,
  which loses the sub-second part in its `timetuple` / `time.mktime` round trip
  (visible only against an endpoint that lists sub-second `LastModified`, where
  the stamp then decides `--exact-timestamps`). A failure does not
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
  `no_overwrite` (IfNoneMatch). `filter` is silently ignored on a **`cp`**
  stream (a single object has nothing to filter; aws rc 0) - `cp` diverts to
  the dedicated stream path without it. A `mv` onto a stream keeps the ordinary
  transfer path (`s3open`), so its `filter` is still applied, to the single
  source entry. `expected_size` applies to an upload stream and is ignored
  elsewhere.
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
  only). `StdioStorage`'s stdout writer is a **write-only** view (aws's
  `StdoutBytesWriter`), so s3transfer always takes its non-seekable path and
  writes ranged chunks in order - a redirected stdout can report seekable
  while `>>` opened it `O_APPEND`, where seek-based parallel writes would
  interleave. That writer has no `flush` and its `close` does nothing, since
  aws's has neither method and the non-seekable download manager's final task
  is a no-op: **nothing in either codebase flushes the process stream**, so the
  bytes wait for the interpreter's shutdown flush. That decides where an
  unwritable stdout surfaces: a failing `write` is the item's failure
  (`download failed:` + rc 1), while a failure only the buffered flush can
  raise arrives at interpreter shutdown instead, as the process's 120 with no
  failure line, the boundary between the two being the interpreter's stdout
  buffer size rather than anything here
  ([`aws-differences.md`](../docs/cli/aws-differences.md) section 2 records the
  band). Only stdin is checked ahead of the transfer (`open("rb")` raises
  `ValidationError`, aws's `StdinMissingError` sentence); stdout has no
  counterpart on either side, so a process without one fails the item with
  `'NoneType' object has no attribute 'write'` from inside the transfer.
  An `IOStorage` (caller-supplied stream) keeps the stream's own
  seekability: the caller chose the object, so its protocol governs - and its
  writer view still absorbs the transfer's `close` into a flush, the
  `StdioStorage` no-op being the aws-shaped exception.
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
- **SDK floor gate** (the compatibility.md degradation): the write op's S3
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
- **an S3-side timestamp the local calendar cannot hold ends the run** - the
  mirror image of that last walk warning, and deliberately not a warning.
  aws-cli converts every timestamp an S3 response carries to the local zone the
  moment it reads it (a listing entry through `BucketLister`'s date parser, a
  single object as the last thing `_list_single_object` does), so a
  `LastModified` that leaves `datetime`'s range once the local offset is added
  fails right there with `date value out of range` and nothing is transferred -
  where a *local* mtime it cannot represent is warned away and stamped with the
  epoch instead. `s3storage.reject_unrepresentable_stamp` runs that exact
  conversion for its exception alone, at both of aws-cli's points: the listing
  conversion inside the S3 backend, and `producers.head_single` for the
  single-object HEAD route. The value carried on stays UTC per the
  `FileInfo.mtime` contract, and the conversion is skipped for the years that
  cannot reach either end (only years 1 and 9999 on POSIX, always on Windows,
  the same banding the local side uses). A single blind delete is exempt
  because aws issues no HeadObject for it - `rm s3://bkt/key` stays rc 0 on
  both tools.
- **symlink-loop guard** (`detect_symlink_loops`, a **library extension**, default
  off so `cp` / `mv` / `sync` keep aws parity - `aws s3` has no such option):
  off, a symlink cycle descends until the kernel's `ELOOP` / path-length
  boundary ends it with aws's `File does not exist.` warning battery and the
  walk skips the directory, exactly like aws-cli (the boundary comes long
  before any `RecursionError` could - and the stop now comes from the
  vetting-time boundary probe one level up rather than from the descent's own
  `os.open`, which is what keeps aws's wording on the bare path); on
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
- **a directory that changes underneath the walk** - replaced by a file,
  removed, or locked away between its parent's scan and its own descent:
  aws-cli re-tests every child immediately before recursing into it, on the
  *separator-terminated* path its sort key carries, and warn-skips it
  (`Skipping file <dir>/. File does not exist.`, or `... File/Directory is not
  readable.` for the chmod race - rc 2) rather than failing the run.
  Establishing the descent's own scan **is** that re-test here, so the same
  battery runs on the path as given, separator and all, and the walk continues;
  when the battery finds nothing wrong the `OSError` propagates instead, so
  nothing is ever pruned silently.
- **a leaf that changes between its parent's scan and its own turn** - the other
  direction of the same race, and the one aws-cli spends a second syscall on per
  name (`os.path.isdir` then `_safely_get_file_stats`, both in its descent loop).
  `LocalFileGenerator.restat_leaf` is that one `os.stat`, taken just before the
  leaf is emitted - after every earlier sibling's whole subtree has been read -
  and it decides three outcomes. Gone: the `File does not exist.` battery runs on
  the path and the leaf is dropped (rc 2) rather than submitted and failed at
  open (rc 1). A directory by now: the record comes back promoted
  (`promoted_directory`) and the walk descends it, so its children are
  transferred and `--dryrun` previews them - addressed by the **bare** name the
  leaf sorted under, since the separator-terminated addressing above belongs to
  names that were directories at scan time, which is also where the subtree
  lands in the emitted order. Still a leaf: `size` / `mtime` / `stat_result` are
  refreshed from that stat, so what is uploaded and what `sync` compares is the
  file as of its turn rather than as of the scan. The cost is that one extra
  `os.stat` per leaf. A record classified from a link's own stat (`S_IFLNK` - the
  complete no-follow view, or an lstat-style `entry_stat_result` override) is
  returned untouched, so a link stays that walker's own leaf; and a walker whose
  children are not live filesystem paths overrides the seam to `return info`.
- **fd-relative walk boundary fallback**: the fast walk vets each entry through
  the owning directory's fd (`fstatat`/`openat`, `localstorage.py`'s
  `have_dir_fd` path), which re-anchors resolution one level at a time and so
  hides what aws-cli's own full-path `stat` would trip on - an ancestor
  symlink chain crossing `SYMLOOP_MAX`, or a path crossing `PATH_MAX` - and can
  admit a child the transfer then fails to open (rc 1) where aws warn-skips it
  at enumeration (rc 2, `File does not exist.`). Only near either boundary
  (`sym_depth` for a symlink child, the full path's length for any child - both
  floors sit well below the real OS limits, so an ordinary walk never reaches
  them) does the walk re-run the full-path warning battery
  (`LocalFileGenerator.crosses_full_path_boundary`) and drop a child it would
  warn away, so the two agree. **Directories take that probe too**, by their
  bare full path: aws-cli names a child by its bare path when it vets it and
  with the trailing separator when it descends, so a boundary-crossing
  directory left to fail its own descent would be warned with the separator
  aws-cli's vetting-time warning does not carry.
  On Windows (`have_dir_fd` false) the walk
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
  extra configuration as long as awscrt is present. The installed botocore lets
  `BOTO_DISABLE_CRT` turn that detection off, and the botocore aws bundles has
  no such switch; the CLI drops the variable before any of its modules can
  reach botocore, so it decides nothing there either (cli.md section 4 item 10).
  The library leaves it alone - an application embedding `boto3_s3` keeps its
  own botocore's behavior.
  awscrt is **not a default dependency but an opt-in extra**: the library
  provides `boto3-s3[crt]`
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

- The default integrity checksum belongs to the installed botocore: pip's
  `DEFAULT_CHECKSUM_ALGORITHM` is `CRC32`, aws v2's bundled one is `CRC64NVME`,
  and both s3transfers copy that constant onto an upload that names none
  (the CRT modules have their own copy: `CRC32` in pip's, `CRC64NVME` in
  aws's). The library leaves botocore's default in place (boto3-faithful,
  crt.md section 1), so a library upload without `checksum_algorithm` stores a
  composite CRC32 where `aws s3 cp` stores a full-object CRC64NVME - valid
  integrity checks both, same result and rc. The CLI names aws's value
  wherever aws's botocore would have stamped it (uploads on both engines, and
  every other request with a `ChecksumAlgorithm` member: `checksumdefault`,
  cli.md section 4), where the installed botocore can compute CRC64NVME;
  without awscrt the CLI falls back to botocore's `CRC32`, the residual
  difference docs/cli/aws-differences.md records. On the CRT engine the
  algorithm also costs wall time: aws-checksums computes CRC32 in software,
  a measured 10% on a 1 GB upload (benchmarks/RESULTS.md, 2026-09-06).
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
- Under `response_checksum_validation = when_required` (env
  `AWS_RESPONSE_CHECKSUM_VALIDATION` or the config key; not the default), pip
  s3transfer (0.19) skips the sizing HeadObject and opens every classic
  download with a first-chunk **ranged** GetObject, discovering the size from
  that response - an optimization the aws-cli fork does not have. aws HEADs
  (when the size is unknown) and issues a plain GetObject below the multipart
  threshold, so under that setting the two sides send different request
  shapes for a small single-object download (the branch fires before any
  provided size is consulted). Same bytes, same rc; only the wire shape
  differs, and only under that non-default setting - recorded, not worked
  around, for the same reason as the fork-only combine above: the divergence
  lives in the installed s3transfer, not in this codebase.

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
- **`open` is deferred to first byte movement**: the item builders hand the
  engine lazy handles (`producers._DeferredReader` / `_DeferredWriter`), and
  the backend's `Storage.open` runs when s3transfer first reads / writes that
  entry - never at enumeration/queue time, where an eagerly opened handle per
  queued item crossed `RLIMIT_NOFILE` around a thousand entries. The handles
  expose only `read` / `write`, which routes s3transfer to its non-seekable
  paths: the source is consumed sequentially on the bounded submission stage
  (`max_in_memory_upload_chunks` backpressure), the destination receives
  ranged chunks strictly in order (the ordering stdout gets). Consequences:
  an open/read/write failure is that item's **per-item** failure (the
  capability gate's documented contract - runtime errors never abort the
  run up front); an item that fails or is cancelled before its first write
  leaves the backend **untouched** (the failure-path close prefers the
  handle's `discard`); a successful zero-byte download still materializes
  the empty object (`close` commits, opening if nothing was written).
- **dryrun**: enumerates and reports `DRYRUN` but never builds the lazy
  handles at all - opening a `"wb"` writer is itself a side effect, so a dry
  run leaves the backend untouched.
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
