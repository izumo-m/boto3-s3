# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- `GlobFilter`'s builder is replaced by `Exclude` / `Include` rules compiled with `globsieve.compile`, which matches as `aws s3` does on every OS (absolute and Windows drive forms, Windows case and separators, a single source in `aws s3`'s form); `GlobPattern`, `PatternKind`, `is_anchored` and the matcher classes are gone.
- `cp`, `mv`, `sync` and `rm` now read the `[s3]` section of the AWS config file the way `aws s3` does, filling the transfer settings a `TransferConfig` leaves unset and picking the engine as `aws s3` picks it; `S3.resolve_transfer_config` returns the config a run would use.
- An exception from `on_progress` now ends the run and reaches the caller as it is, like one from `on_result`, instead of failing the item it reported on.
- A single local source that is a directory is now named as `aws s3` names it in its result and warnings (a trailing separator, and no name appended to a prefix destination), and `LocalStorage.get_fileinfo()` finds a file typed with a trailing separator absent, as `aws s3` does.
- A request that dies without any response (a connection closed, a read timeout) now fails its item instead of leaving no record.
- A classic download that gave up retrying a body read now fails with `TransportError` instead of the base `Boto3S3Error`.
- A multipart copy whose post-copy tag write and its cleanup delete both fail is now reported as failed, so `mv` keeps its source, and a Ctrl-C in a copy's post-copy writes under `use_threads=False` now cancels the item.
- A download to `StdioStorage` now flushes standard output when it finishes, so a stdout that cannot take the bytes fails the item, and `mv` keeps its source, instead of the process failing at exit; a process without a stdout fails it even for an empty object.
- On an SDK too old for them, `mb`'s tags and `-an` bucket names and `ls`'s bucket filters now raise `ValidationError` (botocore's own parameter check) instead of `ConfigurationError`.
- Removed `S3(crt_allow_absent_credentials=..., crt_region=...)` and `CLIENT_REGION` / `CrtRegion`: the CRT engine always takes the client's region and boto3's rule for a client without credentials, and `presign` raises `InvalidValueError` for an expiry the CRT signer refuses.
- Failures `aws s3` loses or finds late are now reported: a `PreconditionFailed` to any request but `no_overwrite`'s own write fails the item, a file whose stat failed and then recovered is warned about, a malformed `grants` is refused before the run, and a run refused before it starts no longer creates its destination directory.
- On the CRT engine, items cut short by a fatal error or Ctrl-C, or by an interrupt from an upload's stream, are now `CANCELLED` as on the classic engine, and a Ctrl-C the CRT manager swallows during its drain is re-raised instead of ending in `BatchError`.
- A local source that cannot be walked as a directory (a file, an unreadable or special path, a symlink not followed) now stops a `sync` or recursive `cp` / `mv` before it starts instead of running on an empty source, a `LastModified` only a local-time conversion cannot hold no longer stops a run, and `requestparams.validate_grants` checks a grants value on its own.
- The library no longer checks or works around code you hand it that breaks its documented contract (streams, custom storages, `GlobFilter`'s `compare_key`), and records what that code raises as the error policy says: a `CancelledError` it raises as it is, a CRT stream upload's read failure instead of awscrt's unknown error, and a custom backend's failure during a content comparison as a `Boto3S3Error`.
- More code that only reproduced `aws s3`'s error details or made up for an older SDK or Python is gone: when two things are wrong the other may be reported, a symbolic link a `follow_symlinks=False` walk skips is skipped without a warning wherever it sits, the service root reads as `s3:///` however it is typed, and a boto3 too old for the `DEFAULTS` table keeps its own `max_io_queue`.
- Responses outside S3's API are no longer checked; `MalformedResponseError` and `S3ScanOptions.include_common_prefixes` are removed.
- `ChecksumComparison()` no longer takes `s3, src, dest`: it reaches each S3 side through the entry's `storage`; `mv` deletes a downloaded or copied source through `Storage.delete` as it does an uploaded one; and warnings and notices about an item carry its entry as `src_info`: `Warner.warn` / `notice` take `info=` in place of `key=`, and `Transferrer.warn` / `notice` are removed (use `Transferrer.warner`).
- An upload from a custom storage guesses its `Content-Type` from the entry's `compare_key`, every name read as a file name.
- A single-key `rm` interrupted by Ctrl-C reports the item `CANCELLED` and raises at once instead of waiting for the delete, and a local file time only the local zone cannot hold is read as its UTC value instead of the epoch with a warning.
- Removed `TransferConfig`'s `should_stream` / `disk_throughput` / `direct_io` (they had no effect; the `[s3]` keys are still accepted), the walk's `crosses_full_path_boundary` and `sym_depth`, and `boto3_s3.transfer`'s two SDK-floor probes from its exports.
- `S3` and `LocalStorage` take a `filename_encoding` that fixes how local file names turn into keys and back on Linux and macOS, whatever Python's own default (`None` keeps it); `LocalStorage.os_path` / `text_path` convert a path between the two forms.
- `s3_errors` and `translate_boto_error` moved from `boto3_s3.s3storage` to the new `boto3_s3.sdkerrors`.
- `Transferrer` takes the CRT engine settings as one `crt=` value in place of `session`, `crt_endpoint`, `crt_allow_lockless` and `crt_sign_requests`.
- `S3` and `S3Deleter` take the CRT engine's own client settings as one `CrtClientOptions` (`crt=`) in place of `crt_allow_lockless` / `crt_sign_requests`, and it can also name the CRT client's TLS trust source (`verify`); the CRT engine now signs exactly when the client does.
- `presign` now returns the URL `aws s3 presign` returns for the client's region, however the client was built: SigV4 in aws-cli's parameter order even with awscrt installed (a non-positive `expires_in` is signed as given), and the regional host for a us-east-1 client.
- `pin_proxy_connect()` makes botocore open HTTPS-proxy tunnels with the CONNECT request aws-cli sends, for Python 3.10 and 3.11 behind a proxy that requires a `Host` header.
- `S3.from_aws_cli()` builds an `S3` whose session and clients are assembled the way `aws s3` assembles its own, from the same common options (profile and region order, retries, the us-east-1 endpoint, the default checksum, the CRT engine's CA, local file-name encoding, the shared credential cache).
- `cp`, `mv` and `sync` take `source_region` (`--source-region`), `S3.source_client()` builds that source client, and `mv` takes `validate_same_s3_paths` (`--validate-same-s3-paths`), resolving access points before a same-key move and otherwise warning as `aws s3 mv` does; `S3.check_mv_paths()` runs those checks on their own ahead of a run.
- Removed `pathresolver.is_mrap_path`, `is_outpost_path` and `is_outpost_alias_path`, the probes for standing down a pinned signature version.

## [0.12.0] - 2026-10-18

- Added `is_s3express_accesspoint_path`, `LocalStorage.exists()` / `ensure_directory()` and a `transfer_config` for `rm` and `S3Deleter`; `S3PathResolver` and `has_underlying_s3_path` now cover an S3 Express access point, which `sync` refuses, and an empty local path is rejected instead of meaning the working directory.
- `S3FileInfo.etag` now holds the ETag exactly as S3 returned it, quotes included, so a multipart download from an endpoint serving another form no longer fails its `If-Match`; `EtagComparison.content_differs` takes either form.
- Failures now take the library's exception classes on every path and engine: an incomplete response or a timestamp the host cannot represent is `MalformedResponseError`, and a request's, a local file's or a configuration's error no longer arrives as a bare built-in or botocore exception.
- A CA bundle setting (`AWS_CA_BUNDLE`, `ca_bundle`, `REQUESTS_CA_BUNDLE`, `verify`) that is empty or whitespace-only is now refused with `InvalidConfigError` on every botocore version, instead of turning TLS certificate verification off under an older botocore.
- On an SDK too old for them, `mb`'s tags and `-an` bucket names, the newer checksum algorithms and S3 Express directory buckets and access points are now refused up front with a `ConfigurationError` naming the version they need.
- More `aws s3` parity in transfers and deletes: a request that dies without any response leaves no record where `aws s3` loses it, a batched delete sends again on its own a key that failed transiently or whose batch failed as a whole, deletes go on the CRT client when the CRT engine is selected, and `copy_props=ALL` writes a multipart copy's annotations after its tags.
- More `aws s3` parity in listings and paths: `sync` merges an out-of-order listing instead of stopping, a timestamp sent without a zone is read as local time, a string path is read as `aws s3` reads it (`rm` on an Outposts access point ARN closed with `:` no longer deletes the whole access point), an unreadable `mime.types` fails each upload it would have typed, and the `Content-Type` guess follows `aws`'s interpreter on every host.
- The CRT engine now resolves a lazily refreshed credential per transfer, runs on boto3 1.34 to 1.41 with the `crt` extra, is shared by a second client that resolves the same, and fails a download whose file could not be put in place instead of succeeding (a `mv` then deleted the object).
- A download into an `IOStorage` over an append-mode stream or a pipe that claims to seek is now written in order, a retried one into a stream that cannot seek no longer writes its first bytes twice, a CRT upload from a pipe or socket works, a seekable stream's upload is no longer cut to `expected_size`, and `IOStorage`'s type admits a wrapper offering only `read` or `write`.
- A transfer cut short by Ctrl-C, or by an interrupt the caller's own code raises on a worker thread, is now recorded as cancelled and re-raised, so a `mv` no longer deletes its source; an exception from `on_result` reaches the caller on every engine, a Ctrl-C during a single-key `rm` waits for its request, and an immediate cancellation also interrupts a CRT submission waiting on backpressure.
- Log masking now also hides a legacy SigV2 `Authorization` id and a credentials response's id that are not AWS-shaped, and no longer swallows the rest of a logged XML body when a key is spelled like a secret's name.
- Smaller fixes in `get_file` / `put_file` (long multi-byte names, read-only Windows destinations, special files), `ChecksumComparison` (a multipart checksum sent without its part count), `S3.aws_config()` (a later `boto3.setup_default_session(...)`), a listing failure's `operation`, and `EtagComparison`'s `part_size` check.

## [0.11.1] - 2026-09-07

- The package logger now carries a `NullHandler` like boto3's, so a library warning no longer prints on its own where no logging is configured.
- A delete request that raises outside botocore's own errors now fails its keys instead of propagating.

## [0.11.0] - 2026-08-23

- Added `sync(pair_filter=...)`, one callback deciding every paired entry — create, update and delete alike — in place of the three lane filters.
- `set_stream_logger`'s masking no longer alters the log records other handlers on the same loggers receive.
- Added `EtagComparison.content_differs`, which compares a single local file or stream against a known S3 ETag without a `sync`.
- A batched delete whose response carries an error naming no submitted key now fails the keys it cannot confirm instead of reporting them deleted.
- Added `S3Deleter(dryrun=True)`, which reports what it would delete without sending anything.
- Added `S3Storage.get_file` / `put_file`, which move one local file in a single S3 request (no transfer engine, atomic local write).

## [0.10.0] - 2026-08-13

- Local file timestamps at the edge of `datetime`'s range now warn and fall back to the epoch exactly where `aws s3` does, a download stamps whole seconds on the local file like aws-cli, and an S3-side timestamp the local zone cannot represent now stops the run where aws-cli stops.
- S3 listings and single-object heads now fail on an entry missing a required element the way aws-cli fails, instead of silently dropping it.
- A local entry that changes underneath a recursive walk — a directory replaced or removed, a file that became a directory, one that vanished or was rewritten — is now handled the way `aws s3` handles it (warned and skipped, descended, or transferred as it now is) instead of failing the scan.
- `sync` now refuses a listing that arrives out of order with a `ValidationError` instead of an internal assertion (aws-cli keeps merging there and can delete files present on both sides — deliberately not mirrored).
- Added `ScanOptions.read_ahead`; a deleting `sync` walks its local destination without read-ahead, so a tree aliased through symlinked directories no longer reports spurious delete failures (aws-cli's shape).
- `guess_mime_type` now guesses from the MIME table of the official aws-cli build on every host interpreter, so the stored `Content-Type` no longer depends on the Python version.
- A stream destination no longer checks stdout up front or flushes it on completion (aws-cli's writer shape): a missing or failing stdout is a per-item transfer failure.
- Added `S3ScanOptions.include_common_prefixes`; `ls` uses it to keep prefix entries in recursive listings the way `aws` displays them.
- Fixed a rare hang: a cancellation upgraded to immediate while `sync` was already settling pooled filter decisions now cancels them instead of waiting forever.
- `wait_on_interrupt` was renamed `reusable_after_interrupt`: the flag declares whether operations stay callable after a caught Ctrl-C, which the old name did not convey. Declaring `False` now also abandons an in-flight listing page pull whichever window the interrupt lands in; previously an interrupt outside the pull itself waited the pull out.
- `presign` URLs now name the client's own regional endpoint like `aws s3 presign`; outside `us-east-1` they named the legacy global host while carrying the real region in their credential scope.
- Added `pathresolver.is_outpost_path` and `is_outpost_alias_path`, the S3 Outposts counterparts of `is_mrap_path` for callers that pin a signature version; all three probes now recognize every spelling botocore signs with SigV4a, not just aws-cli's canonical ones.
- More aws-cli parity on transfer wording and shapes: a single-file copy to the bare `s3:///` root keeps the explicit slash and names its destination key like aws-cli, a rewritten `HeadObject` 404 keeps botocore's retry note, and a failed annotation copy reports aws-cli's failure text.
- `TransferConfig`'s download IO queue now defaults to the depth `aws s3` runs at, so passing a default-constructed config no longer shrinks it tenfold.
- A CRT-engine run cut short — by Ctrl-C or by a fatal error elsewhere in the run — now counts the cancelled items as failures the way `aws s3` does (a Ctrl-C that previously returned as if the run had succeeded now raises `BatchError`); only a `CancelToken` cancel still reports them cancelled.
- Added two more `aws s3` CRT postures to `S3(...)`: `crt_allow_lockless` lets an explicit `crt` preference build the CRT engine under cross-process lock contention the way `aws s3` does (an `auto` run never inherits the opt-in through the process singleton), and `crt_sign_requests` declares whether the CRT engine signs its requests instead of deriving it from the client. Every default keeps boto3's own behavior.
- Building a transfer engine no longer rewrites the multipart-copy defaults of plain s3transfer callers sharing the process.
- More aws-cli parity in how the transfer engine is driven: a multipart copy under an explicit `metadata_directive="COPY"` keeps the properties you passed instead of silently dropping them, an annotation copy no longer sends a checksum header `aws s3` omits, a bad option is now reported ahead of an unusable source, and the case-conflict gate holds a key until its download has fully torn down.

## [0.9.0] - 2026-08-01

- Added a user guide covering both packages under `docs/`; the design documents moved to `design/`.
- Added a hand-written per-symbol API reference under `docs/reference/`, with an index pinned to the export surface by a test.
- Names referenced by package-root signatures and defaults (`AwsConfig` / `ConfigSection`, the three sync comparison strategies, `PatternKind`, `S3_DELETE_BATCH`) are now importable from the package root.
- Exception polish: an invalid `case_conflict` or `presign` `method` value now raises `ValidationError`, errors now carry the running operation's name and location context, and `ls` bucket filters on a too-old SDK are now refused up front.
- aws-cli parity fixes on error paths: only a bare-404 `HeadObject` miss is rewritten to the key-missing message, and a custom storage carrying a bucket-less key can reach the delete submit (its dryrun records instead of failing).
- Added `aws s3`'s CRT postures to `S3(...)`: `crt_allow_absent_credentials` and `crt_region` declare how the CRT engine handles absent credentials and where its region comes from, and `materialize_crt_engine()` builds that engine ahead of any transfer. Every default keeps boto3's own behavior.

## [0.8.0] - 2026-07-23

- Added `session()` / `fast_parse_timestamp`: a boto3 `Session` factory whose clients parse response timestamps at C speed, making large listings severalfold faster - `S3(session=boto3_s3.session())` is now the recommended construction.
- Reshaped the public API: the sync-pair / result identity field is now `compare_key` (delete records join the same key space), `ls` takes `on_entry`, `S3Storage` speaks `uri`, unknown transfer options are rejected eagerly, capabilities became the sole custom-backend contract, and the export tiers shrank.
- Documented the public contracts (exception attributes, the two-tier export surface, the backend SPI evolution policy) and fixed a raw `ValueError` leaking from `client()` on a malformed endpoint.
- Aligned more behavior with aws-cli: an unset page size sends no `MaxKeys`, small multipart-copy tag sets stay inline on the create call, deferred annotation copies paginate, and CRT requests keep the caller's client configuration.
- Reused the installed default boto3 session for config reads, masked S3 Express session tokens, refused `--no-overwrite` uploads on too-old s3transfer, and fixed the module-level helpers' reported signatures.
- A fatal error now cancels accepted transfers like aws-cli instead of draining them; revoked items report the new `CANCELLED` outcome, with the `on_result` contract now documented.
- Swept sibling instances of past bug patterns: error translation and source-side attribution close remaining gaps, a pre-cancelled token no longer leaves side effects, and two shutdown/signal hangs are fixed.
- A missing awscrt where SigV4A signing is required (Multi-Region Access Points) now raises `ConfigurationError` instead of the base error.
- The Ctrl-C exit posture moved to `S3(wait_on_interrupt=...)` (reaching scans via `ScanOptions`); the per-storage constructor option is gone, and it no longer applies to `SystemExit`.
- More aws-cli parity: `sync` now rejects S3 Express directory buckets (their listings are unordered, which could mis-pair the merge), and a `mv` download stamps the file mtime before deleting the source; relative local storages also resolve consistently if the process chdir's after construction.
- CRT transfers now derive their endpoint and TLS settings from the run's actual client - a client with a different endpoint, `verify`, or S3 addressing config falls back to the classic engine instead of riding the first client's settings; custom-backend scans stamp `FileInfo.storage` before filters run, and the open route no longer skips the keyless-source listing probe.
- Streaming downloads to stdout now always write in order like aws-cli (a stdout redirected with `>>` opens O_APPEND, where seek-based parallel writes could interleave chunks).
- Custom-backend (open-route) transfers now call `Storage.open` when an entry's bytes first move, not when it is queued: large recursive runs no longer exhaust file handles, failed or cancelled entries leave the backend untouched, and destinations receive one strictly ordered write stream.

## [0.7.0] - 2026-07-17

- `SyncPair` now always carries both sides (`src` / `dest` non-optional); one-sided sync pairs became the new `SrcOnlyPair` / `DestOnlyPair` types (`MergedPair` union).
- Improved cancellation robustness, local path-limit parity, and secret masking coverage; added a storage option to exit scans immediately on interrupts.
- Sped up CRT transfer startup by reusing the caller's session for request serialization.

## [0.6.0] - 2026-07-12

- Expanded local scanning and transfer controls while improving operation reliability and aws-cli parity.

## [0.5.0] - 2026-07-07

- Deepened the `Storage` scan contract, sync decision filters, response capture, content comparison, and local filesystem customization.

## [0.4.0] - 2026-07-03

- Improved transfer correctness, secret masking, custom backend behavior, SDK-floor degradation, and path/filter parity.

## [0.3.0] - 2026-06-27

- Added the capability-based `Storage` extension model, custom-backend transfers, configurable local walking, and symlink-loop detection.

## [0.2.0] - 2026-06-23

- Added streaming copies, AWS config access, sync comparison strategies, parallel comparison, glob filtering, and old-SDK compatibility.

## [0.1.0] - 2026-06-16

- Initial release.

[Unreleased]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.12.0...HEAD
[0.12.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.11.1...boto3-s3-v0.12.0
[0.11.1]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.11.0...boto3-s3-v0.11.1
[0.11.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.10.0...boto3-s3-v0.11.0
[0.10.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.9.0...boto3-s3-v0.10.0
[0.9.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.8.0...boto3-s3-v0.9.0
[0.8.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.7.0...boto3-s3-v0.8.0
[0.7.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.6.0...boto3-s3-v0.7.0
[0.6.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.5.0...boto3-s3-v0.6.0
[0.5.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.4.0...boto3-s3-v0.5.0
[0.4.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.3.0...boto3-s3-v0.4.0
[0.3.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.2.0...boto3-s3-v0.3.0
[0.2.0]: https://github.com/izumo-m/boto3-s3/compare/boto3-s3-v0.1.0...boto3-s3-v0.2.0
[0.1.0]: https://github.com/izumo-m/boto3-s3/releases/tag/boto3-s3-v0.1.0
