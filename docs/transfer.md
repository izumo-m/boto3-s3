# Transfer engine (the design of `Transferrer` / `S3.cp` / `S3.mv`)

The settled design of the byte-transfer layer for `cp` / `mv` / `sync`. For the
CLI-side behavior, see [`cli.md`](./cli.md) section 5.7 (cp) / section 5.8 (mv) / section 5.9 (sync);
for the test structure, see [`testing.md`](./testing.md); for the exception
model, see [`exceptions.md`](./exceptions.md). `mv` is `cp` plus source deletion
(section 11). `sync` reuses this engine as-is for its transfer face (the pairing,
comparison, and deletion lanes live in [`sync.md`](./sync.md)).

## 1. Components

| module | role |
|---|---|
| `naming.py` | Pure-function port of the path-shape rules (aws-cli `FileFormat` / `find_bucket_key` / keyless normalization / `find_dest_path_comp_key` / filter root). SDK-independent, so the CLI and the library derive paths, key naming, and the filter root from the **same code** |
| `requestparams.py` | Pure-function port of `TransferOptions` (snake_case) -> S3 API parameters (PascalCase) (aws-cli `RequestParamsMapper`). The format validation of grants is also done with aws's wording |
| `localstorage.py` | `walk_local`: a faithful port of aws-cli `FileGenerator.list_files` (byte-order walk, warning rules). `LocalStorage` fully implements the `Storage` ABC |
| `transfer.py` | `Transferrer`: the transfer engine proper that drives the classic / CRT transfer manager (the subject of this document). With `is_move` it deletes the source and reports MOVE (section 11). Engine selection is in section 2 / [`crt.md`](./crt.md) |
| `transferconfig.py` | The public `TransferConfig` = a subclass of boto3's that adds only the CRT tuning fields ([`crt.md`](./crt.md) section 2) |
| `crtsupport.py` | CRT engine resolution (a faithful port of boto3 `boto3/crt.py` plus refinements). `should_use_crt` / `create_crt_transfer_manager` / lock. The design is in [`crt.md`](./crt.md) |
| `pathresolver.py` | A port of aws's `S3PathResolver` (resolves access point ARN / alias / MRAP to the real bucket; the s3control / sts client is injected). The building block for `mv --validate-same-s3-paths` (cli.md section 5.8) |
| `comparator.py` | sync's pairing and the building blocks for its decisions (`Comparator` / `SyncPair` / `PairFilter` / `compare_size_time` / combinators). The design is in [`sync.md`](./sync.md) |
| `S3.cp` / `S3.mv` / `S3.sync` in `s3.py` | orchestration: path classification -> pre-validation -> enumeration -> gates (glacier / parent-ref / dryrun) -> submit -> `BatchError` aggregation (cp / mv share `_run_transfer`). mv adds a same-path guard and `is_move` ahead of that. sync forks the enumeration into two streams, inserts pair decisions in between, and shares the per-info item builder and the gates with cp |

## 2. Engine selection and lifetime

- **The engine follows `TransferConfig.preferred_transfer_client`**
  (`'auto'` (default) / `'classic'` / `'crt'`). `Transferrer._get_manager`
  resolves it with the same semantics as boto3: either it uses the classic
  `s3transfer.manager.TransferManager` directly, or, if CRT is chosen,
  `crtsupport.create_crt_transfer_manager` ([`crt.md`](./crt.md)). **COPY
  (s3->s3) is unconditionally classic** - `CRTTransferManager` has no copy, and
  the rule that boto3 / aws-cli also pin s3->s3 to classic. `'auto'` faithfully
  reproduces boto3's behavior that "CRT can be auto-selected merely because
  awscrt is importable" (on a machine where `is_optimized_for_system()` is true
  it becomes auto-CRT, just as in boto3 - that fidelity is the whole point). We
  do not use `boto3.s3.transfer.create_transfer_manager` and hold manager
  creation ourselves in order to control subscriber wiring, lazy creation, and
  the IfNoneMatch patch.
- The public type is `TransferConfig` from [`transferconfig.py`](./crt.md)
  (a subclass of boto3's that adds the CRT tuning fields; the defaults are the
  same as aws-cli - 8 MiB threshold / 8 MiB chunk / concurrency 10). As in
  boto3, classic maps `use_threads=False` to `NonThreadedExecutor` (a
  determinization lever for tests; CRT ignores the threading-family knobs - also
  as in boto3). The overall design of CRT mode is in [`crt.md`](./crt.md).
- A `Transferrer` is **one instance with a single `OpKind` per cp / mv / sync
  run** (one run has a single byte direction). The client placement is: upload
  uses the dst client, download uses the src client, and an s3->s3 copy uses the
  dst client + `manager.copy(source_client=src client)` (a settled fact of the
  connection model).
- The manager is **created lazily on the first `submit()`**: a dryrun or a
  fully-skipped run does not even import s3transfer (the discipline of
  [`imports.md`](./imports.md)).
- **Backpressure is delegated to s3transfer**: the bounded semaphore of
  `TransferConfig.max_request_queue_size` (default 1000) blocks the submit
  thread when it is full. This is the same mechanism aws-cli relies on; we keep
  no in-flight window of our own.
- context manager: on normal completion and on a **normal exception** (a fatal
  in mid-enumeration) it performs a graceful shutdown (submitted transfers run
  to completion = aws's behavior). Only `CancelledError` (`CancelToken`) and the
  `KeyboardInterrupt` family cancel in-flight transfers.

## 3. Subscriber composition (follows the order of aws-cli `s3handler`)

Because s3transfer resolves callbacks with `getattr` (duck typing), subscribers
are plain classes that do not inherit `BaseSubscriber` - so as not to pull in
the SDK at module import time.

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
5. mv only: `_DeleteSource` (section 11) - after the path-specific subscribers and just
   before `_Completion` (the same slot where aws-cli places the DeleteSource
   family ahead of the Done recorder).
6. `_Completion` (**always last**) - bridges the future's result to the rollup
   (locked succeeded/failed/warned/skipped + first_error) and to `OpResult`. On
   a successful download it post-success stamps the mtime (section 5).

Items 3-5 are route-conditional (download / copy / mv only); the always-present
spine is 1-2 then 6 (the numbering is the slot order, not a single chain that
every transfer runs end to end).

`on_result` / `on_progress` are **called from s3transfer's worker threads**
(the same contract as deleter: fast and non-raising).

## 4. copy-props (a port of aws-cli v2's correction)

A single CopyObject lets S3 natively carry over metadata and tags (the directive
default = COPY). **A multipart copy (at or above the threshold) does not carry
them over**, so aws-cli corrects for this with subscribers - we ported the same
chain:

| `copy_props` | subscribers | behavior |
|---|---|---|
| `none` | ReplaceMetadataDirective + ReplaceTaggingDirective | Carries nothing over (sets the directive to REPLACE). s3transfer excludes the directive from CreateMultipartUpload via a blacklist |
| `metadata-directive` | SetMetadataDirectiveProps + ReplaceTaggingDirective | Injects 7 properties (CacheControl / ContentDisposition / ContentEncoding / ContentLanguage / ContentType / Expires / Metadata) from the source HeadObject. Tags are not carried over |
| `default` (the default) | SetMetadataDirectiveProps + SetTags | The above + tags. GetObjectTagging -> percent-encode, and if it is ~2 KiB or under use the `Tagging` header, otherwise PutObjectTagging after the transfer succeeds (**on failure, roll back by best-effort deleting the dest** and treat the transfer as failed) |

- The single-shot path reuses the first HeadObject response
  (`TransferItem.head`) and **does not HEAD twice** (the same as aws-cli's reuse
  of `associated_response_data`).
- When there is an explicit property (`--content-type`, etc.), even the
  single-shot path flips the directive to REPLACE and injects the remaining
  properties from the source (aws-cli's rule).
- When `--metadata-directive` is specified, the entire chain is disabled (as in
  aws).

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

## 6. streaming (`-` / any binary stream)

`S3.cp` accepts a **binary stream** (a file-like object with `read` / `write`)
on one side of src / dst. This is the building block for `aws s3 cp`'s `-`
(stdin / stdout); the CLI merely passes `sys.stdin.buffer` /
`sys.stdout.buffer`.

- **Single item, no gates**: a stream is always a single transfer (the same as
  aws's stream path, which does not go through the generator). `recursive` is a
  `ValidationError` with aws-cli's wording (`Streaming currently is only
  compatible with non-recursive cp commands`), and a stream on both sides is
  also rejected. The glacier / parent-ref gates are not run.
- **Stream option policy (follows aws-cli per option)**: a meaningless option is
  rejected, an additive one that degrades to a no-op is ignored. `recursive`
  (above) and `no_overwrite` on a streaming **download** raise (`no_overwrite is
  not supported for streaming downloads`) - a stream has no existing destination
  to guard, the same combinations aws-cli rejects. An upload stream keeps
  `no_overwrite` (IfNoneMatch). `filter` is silently ignored on a stream (a single
  object has nothing to filter; aws rc 0). `expected_size` applies to an upload
  stream and is ignored elsewhere.
- **The key is verbatim**: the key of the S3-side `S3Storage` is used as-is.
  aws's naming where "in the form where the dest takes the source name
  (`s3://bucket` / `s3://bucket/pre/`) the literal `-` becomes the basename"
  (`pre/-`) is **derived by the CLI layer with naming.py before being passed in**
  (the library is permissive; the quirk is owned by the CLI).
- **upload**: passes `src_fileobj` to s3transfer as-is. No ContentType guess
  (there is no filename). `expected_size` is a chunk-design hint for multipart
  (TransferItem.size) - if unspecified, the engine buffers up to the threshold to
  decide (s3transfer's non-seekable handling = the same implementation as aws).
- **download**: provides neither size nor etag -> s3transfer self-probes with
  HeadObject before GetObject (exactly aws's stream wire shape). Directory
  creation and the mtime stamp are not performed (section 5 is for path destinations
  only).
- The display renders the stream side as `-` (`src_display` / `dst_display`).
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
- **Idempotent patch to pip s3transfer**: the pip build of s3transfer (<=0.17)
  does not have `IfNoneMatch` in its allow-table (the fork bundled with aws-cli
  does). On the first manager creation, `Transferrer` **idempotently appends** it
  to `ALLOWED_UPLOAD_ARGS` / `ALLOWED_COPY_ARGS` / the multipart blocklist / the
  COMPLETE list (harmless even if a future s3transfer adds native support).

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
  from `walk_local`'s `on_warning` to `Transferrer.warn` (aws-cli's wording).
- **case-conflict gate** (`case_conflict`, **S3->local recursive download only**,
  fires when mode != `ignore` = the application condition of aws-cli's
  `_modify_instructions_for_case_conflicts`): aws builds this with the sync
  machinery (reverse-enumerating the dest + comparator), but the observed
  behavior reduces to two sets (confirmed by probing) -
  1. The compare key **exists at the dest in exactly matching case** -> always
     transferred (aws-cli assigns `AlwaysSync` to the at-dest entry = cp
     overwrites it; it also does not enter the conflict set).
  2. Otherwise, if "the lowercased key is in the set already admitted this run"
     or "`os.path.exists(dest)` is true (a case-variant on a case-insensitive
     FS)" -> conflict. `skip` = drop it, `warn` = let it through, both display
     aws-cli's wording as a **NOTICE** (below). `error` = a `Boto3S3Error`
     (`Failed to download <src> -> <dest> because a file whose name differs only
     by case either exists or is being downloaded.`), an in-pipeline fatal
     (CLI rc 1).
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
  verifies the response's checksum).
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

## 10. Known wire divergences (invisible in the result; recorded only)

- We do not send `ChecksumMode: ENABLED` to HeadObject (aws v2 sends it by
  default). `--checksum-mode` rides only on GetObject (section 9).
- When `--checksum-algorithm` is unspecified, the default integrity checksum is
  `CRC32` (pip s3transfer's `setdefault` injection). aws v2's bundled botocore
  injects `CRC64NVME`. Both are valid integrity checks and do not affect the
  transfer result or rc (stated explicitly in the awscli port's adaptation
  rules). When specified explicitly, the two agree.
- A keyless non-recursive S3 source (`cp s3://bucket .`): aws enumerates the
  whole bucket and finishes with 0 exact matches (rc 0, silent). We return the
  same result **without enumerating** (saving request count; behavior can differ
  only in the case where that enumeration would have hit an AccessDenied - an
  extreme edge that we tolerate).

## 11. mv (`is_move`: delete the source when the transfer succeeds)

`mv` goes through the same pipeline as cp for paths (upload / download / copy),
validation, and gates (sharing `S3._run_transfer`). The differences are the two
things `Transferrer(is_move=True)` adds and the same-path guard at the head of
`S3.mv`.

- **The reported kind is `OpKind.MOVE` (`"move"`) on every path**. The path's
  kind remains in the submit branch and in the glacier wording (section 8 - equivalent
  to operation_name). With the same separation as aws-cli's `transfer_type='move'`
  relabeling, every record of result / progress / warning / dryrun calls itself
  `move`.
- **The `_DeleteSource` subscriber** (the position in section 3): performs the deletion
  only when the future succeeded. upload is a plain `os.remove(src_path)`
  (emitting the failure wording in OS form as-is - aws's `move failed: ...
  [Errno 13] Permission denied: '<abs>'` form); download is
  a per-object DeleteObject against the manager's client, and copy against the
  **source-side client** (RequestPayer is passed through via
  `map_delete_object_params`). **A deletion failure flips the already-settled
  future to failed with `set_exception`** (s3transfer accepts an override after
  done - isomorphic to aws-cli's `DeleteSourceSubscriber`), and `_Completion`
  aggregates it as `move failed` (rc 1). The bytes have already arrived.
- Cases where the deletion does not run: dryrun (no submit at all), filter
  exclusion, skip (no-overwrite's 412 / dest already exists, the glacier gate),
  transfer failure. For copy-props' post-copy tagging failure (the rollback of
  section 4), because `_SetTags` flips the future first, the source remains and only the
  dest is rolled back (aws-cli's order). A folder marker is not transferred, so it
  is not deleted either. An emptied local dir is left in place (as in aws).
- **The same-path guard** (always in `S3.mv`; the CLI also does it at the argv
  stage - cli.md section 5.8): apply `naming.same_path` to the keyless-normalized URI -
  if it is an exact match, or a `/`-terminated dest + `basename(src)`
  concatenation matches src, then `Cannot mv a file onto itself: <src> - <dst>`
  (`ValidationError`). `--recursive` is also subject to this (aws-cli's faithful
  false positive; the CLI maps this `ValidationError` to rc
  252). This is a string guard and does not look at
  identity across an access point - that resolution uses `pathresolver.py`
  (`S3PathResolver` + `has_underlying_s3_path`), which the caller uses with an
  injected client (the CLI's `--validate-same-s3-paths`; the flag was removed
  from the library API - because, under the connection
  model, the library does not implicitly create the s3control / sts client).
- **A known local ordering difference**: aws-cli does the download's mtime stamp
  (a subscriber) -> deletion in that order, whereas we do deletion -> stamp
  (`_Completion.post_success`). The difference is observable only in the rare
  case of "a move where the deletion failed, leaving the local mtime unstamped,"
  and the rc / output / file contents agree. If an exact match becomes necessary,
  there is room to promote the stamp into an independent subscriber from section 3 and
  place it before the deletion.
