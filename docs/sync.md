# sync (`S3.sync` / two-layer pipeline + pure-pairing comparator) design

This document is the established design for boto3-s3's equivalent of
`aws s3 sync`. For the CLI-side
behavior see [`cli.md`](./cli.md) section 5.9, for the transfer engine see
[`transfer.md`](./transfer.md), for the delete batch see
[`deleter.md`](./deleter.md), and for the test structure see
[`testing.md`](./testing.md). Behavior matches aws 2.35.18, cross-checked
against MinIO and the aws-cli source.

## 1. Two-layer pipeline

sync runs two layers in series: **visibility** and **pair decision**. The
layers apply one after the other; within a layer, conditions combine as
booleans. This separation is the very structure that produces aws-cli's
observable behavior (the exclusion protection of `--delete`), and it is the
heart of the design.

```
src listing -- filter (visibility) --+
                                     +- Comparator (pure pairing)
dest listing -- filter (visibility) --+        |
                                                          per pair, by type:
                                                          SrcOnlyPair -> create / SyncPair -> update / DestOnlyPair -> delete
                                                                  |
                                                   Transferrer (transfer) / delete lane
```

- **Visibility layer** (before pairing, decided on one side's `FileInfo`
  alone): the single `filter` is applied **to both streams**. A **relative**
  `--exclude` / `--include` matches each side's `compare_key` (directory-relative
  for local entries, `Prefix`-relative for S3 entries), so it
  prunes the source and destination identically (symmetric); an entry excluded
  this way is "nonexistent" on **both** sides and is thus protected from
  `--delete` (aws's "files excluded by filters are excluded from deletion"). A
  **root-anchored** (absolute) pattern instead matches each side's full key, so
  it prunes **per-side** - the same per-side roots aws gets from joining the
  pattern onto `src_rootdir` / `dst_rootdir` (an absolute pattern matching only
  the source leaves the destination visible, so `--delete` still removes it).
  The same one filter expresses both because `globsieve.Anchored` routes a
  relative pattern to `compare_key` and an absolute one to the full key (one is
  symmetric, the other per-side automatically); see globsieve.md. Folder markers
  (size 0, trailing `/`) are dropped from the S3 side here; the local walk never
  produces them - either way sync neither transfers nor deletes markers. The
  implementation is `ScanOptions.filter` on both sides (the S3 listing prunes
  at the listing-page stage; the local walk applies the predicate inline
  during the walk).
- **Pair-decision layer** (after merge, per pair): every decision that
  looks at both sides - size / timestamp / ETag, etc. - lives here. The default
  is aws-compatible, and this is the only layer an application swaps.

## 2. Components

| module | role |
|---|---|
| `comparator.py` | `Comparator` (pure pairing: merge-joins two key-ascending streams and emits every key as its `MergedPair` shape - `SrcOnlyPair` / `SyncPair` / `DestOnlyPair`, telling by type which sides hold it; makes no decision), the pair types and `PairFilter`, `compare_size_time` (the internal aws-compatible default, a function reading `pair.transfer_type`), `all_of` / `any_of` (visibility combinators). No SDK import |
| `S3.sync` in `s3.py` | orchestration: route classification -> pre-validation (src missing 255 / dest dir creation) -> build both-side entry streams (visibility applied) -> pairing -> copy decision -> item builder + gate shared with cp -> `Transferrer` submit; delete decision -> delete lane. The rollup is a `BatchError` combining transfer + delete |
| `_SyncDeletes` in `s3.py` | delete lane: an S3 dest uses `S3Deleter` (batch, lazily created on the first submit), a local or custom dest uses a synchronous `Storage.delete(info)` on the calling thread (`LocalStorage.delete` is an `os.remove`, the shape of aws-cli's `LocalDeleteRequestSubmitter`). dryrun emits only a DRYRUN record. Emits with a display endpoint (`s3://bucket/key` / native path) on the `OpResult` |

Structural correspondence with aws-cli: aws-cli's `Comparator` calls three
strategies inside its merge loop (`file_at_src_and_dest` / `file_not_at_dest` /
`file_not_at_src`), whereas we separate pairing from the decision. Those three
slots map one-to-one onto sync's three lanes: `file_not_at_dest` -> `create_filter`
(the new / source-only entries), `file_at_src_and_dest` -> `update_filter` (the
update pairs), `file_not_at_src` -> `delete_filter` (the orphans).

## 3. Public API

```python
S3().sync(src, dest, *,
    filter: FileFilter | None = None,              # visibility, applied to BOTH sides (same type as rm/cp)
    create_filter: bool | FileFilter | ParallelFilter[FileInfo] = True,          # new (source-only) lane: True=all / False=none / predicate=scope / ParallelFilter=pooled
    update_filter: bool | PairFilter | ParallelFilter[SyncPair] | None = None,  # update lane: None=AwsCliComparison() / True=all / False=none / PairFilter=custom / ParallelFilter=pooled
    delete_filter: bool | FileFilter | ParallelFilter[FileInfo] = False,      # orphan lane: False=none / True=all / predicate=scope / ParallelFilter=pooled
    dryrun=False,                                  # follow_symlinks / page_size are storage config now (storage.md)
    on_progress=None, on_result=None, cancel_token=None, transfer_config=None,
    capture_response=False,             # surface S3 responses on extra_info (opresult.md)
    **options)                          # TransferOptions (acl / sse / metadata / no_overwrite ...)
```

Each merge-joined pair lands in exactly one lane by its type, and each lane
has its own filter deciding whether to act on the entry: **create** a new one
(`create_filter`, a `SrcOnlyPair`), **overwrite** one present on both sides
(`update_filter`, a `SyncPair`), or **delete** an orphan (`delete_filter`, a
`DestOnlyPair`).
`create_filter` and `delete_filter` are the two membership knobs (create / delete) -
duals of each other, and the reason `create_filter` has no `aws` counterpart is that
aws hard-codes "always create" (`file_not_at_dest`); `update_filter` is the
overwrite judgment for the intersection. The aws `sync` equivalent is the default
of all three (`create_filter=True` / `update_filter=None` / `delete_filter=False`).
`filter` is separate: it is **visibility** (`FileFilter = Callable[[FileInfo],
bool]`, e.g. `GlobFilter`), applied per side *before* pairing to narrow which
entries are in scope. `create_filter` / `delete_filter` are `FileFilter`s over the
one side their lane has; `update_filter` is the copy decision over an update
pair (`PairFilter = Callable[[SyncPair], bool]`, True = copy).

- The pair types (`MergedPair = SrcOnlyPair | SyncPair | DestOnlyPair`, what
  `Comparator.compare` yields) tell by type which sides hold the key:
  `SrcOnlyPair(key, transfer_type, src)` = new (the `create_filter` lane),
  `DestOnlyPair(key, transfer_type, dest)` = orphan (the `delete_filter` lane),
  `SyncPair(key, transfer_type, src, dest)` = update (the `update_filter` lane) -
  the one-sided shapes have **no attribute at all** for their missing side. In
  each, `key` is the relative compare key (`/`-separated) and `transfer_type` is
  the sync's direction, stamped on every pair so a filter applies the
  direction-asymmetric rules without being told the route. The three shapes map
  one-to-one onto aws-cli's strategy slots (section 2): `file_not_at_dest` ->
  `SrcOnlyPair`, `file_at_src_and_dest` -> `SyncPair`, `file_not_at_src` ->
  `DestOnlyPair`.
- `create_filter` is the new (`SrcOnlyPair`) lane: `True` (default) copies every new
  entry, `False` none, a `FileFilter` only those it keeps (matched against the
  source `FileInfo` / compare key, the same shape as `rm`'s `filter`). aws-cli
  fills this slot with `MissingFileSync` (a hard-coded "always transfer", which
  the `True` default matches); the filter forms add a scope aws has no flag for.
- `update_filter` is the update (both-sides) copy decision, a single strategy
  (it does not compose): `None` (default) is the aws size + last-modified
  judgment, equivalently `AwsCliComparison()` (it reads the direction from
  `pair.transfer_type`, so it works across routes); `True` re-copies every
  update, `False` none (additive-only: existing destinations are left as-is);
  any `PairFilter` is a custom strategy - the building blocks `AwsCliComparison`
  (section 4) / `EtagComparison` / `ChecksumComparison` (sections 8-9) are
  drop-in replacements. The lane's pair type `SyncPair` carries both sides by
  construction, so a custom strategy reads `pair.src` / `pair.dest` directly -
  no `None` handling. The aws-cli `--size-only`
  / `--exact-timestamps` tuners are **constructor arguments of
  `AwsCliComparison`** (`AwsCliComparison(size_only=True)`), not `sync` options:
  a content compare replaces the judgment wholesale, so there is nothing for them
  to tune there, and the combination is simply unrepresentable. Note `None`
  (default) != `False` (no update).
- `no_overwrite` is an orthogonal write-guard on the update lane, applied before
  `update_filter`: if a destination already exists it is never overwritten (a
  new / source-only pair still copies via `create_filter`). It composes with any
  `update_filter`, and sync keeps it decision-only - no `IfNoneMatch` on the
  wire (unlike cp / mv). `S3.sync(no_overwrite=True)` works because it rides the
  shared `TransferOptions`.
- `delete_filter` is the orphan (destination-only) lane (aws `--delete`): `False`
  (default) deletes nothing, `True` deletes every orphan, and a `FileFilter`
  deletes only the orphans it keeps - matched against the orphan's `FileInfo` /
  compare key, the same shape as `rm`'s `filter` (the delete lane is rm over the
  orphans).

Example (content-based sync + delete only old generations):

```python
from boto3_s3.etagcompare import EtagComparison

s3.sync(src, dest,
    update_filter=EtagComparison(s3),                 # decide updates by content, not size + mtime
    delete_filter=lambda info: info.mtime < cutoff)    # delete only old orphans
```

## 4. The default decision (ported from aws-cli, pinned by measurement)

`update_filter=None` is `AwsCliComparison()`, the public form of the internal
`compare_size_time(pair, size_only, exact_timestamps)` (direction from
`pair.transfer_type`); tune it with `AwsCliComparison(size_only=...)` /
`(exact_timestamps=...)`. `no_overwrite` is not part of it - it is the orthogonal
write-guard the sync loop applies first (section 3). A key missing at the
destination never reaches this judgment: that is the create lane (a
`SrcOnlyPair`; aws-cli fills that slot with `MissingFileSync`, a hard-coded
copy, which `create_filter=True` matches):

| condition | decision |
|---|---|
| `size_only` (and no `exact_timestamps`) | copy only when the size differs |
| default | copy when the size differs **or** when the mtime rule says "not redundant" |

mtime rule (full float precision; `delta = dest.mtime - src.mtime`):

- **upload / copy**: `delta >= 0` (dest at or after the same time) -> do not
  copy.
- **download**: `delta <= 0` -> do not copy. That is, **a same-size download
  runs only when the local side is newer** - a same-size object updated only on
  the S3 side is not pulled down by default (aws-cli's asymmetry).
  `--exact-timestamps` tightens this download rule to `delta == 0` (upload /
  copy unchanged).
- combining `--size-only` and `--exact-timestamps`, **exact-timestamps wins**
  (aws-cli's strategy override order; a same-size, different-mtime
  download runs).
- a pair missing either mtime or size is treated as "different" (falls to the
  copy side). aws's listing always carries both, so this never happens on the
  parity path (the permissive reading of a library building block).

## 5. The execution side of transfer and delete

- The transfer goes through the same item builder + gate as cp
  ([`transfer.md`](./transfer.md) section 8): glacier (download / copy, wording is the
  route word), parent-escape (download), oversize warning (upload). The
  **case-conflict gate applies only to `SrcOnlyPair`s** (aws-cli inserts
  `CaseConflictSync` into the `file_not_at_dest` slot - updating the same key is
  not a conflict).
- delete: an S3 dest uses `S3Deleter` (the `DeleteObjects` batch; the known
  wire divergence from aws-cli's per-key `DeleteObject` - same as rm,
  deleter.md section 4). A local dest uses a synchronous `Storage.delete` (`LocalStorage.delete`, an `os.remove`). The output is
  `delete: <endpoint>` (no `to` clause; the library emits the `s3://bucket/key`
  endpoint for an S3 dest and the full native path for a local dest - matching
  section 2 - which the CLI then renders cwd-relative) / `(dryrun) delete: ...`.
- the local-walk warnings on both sides (missing / unreadable / special file /
  invalid timestamp) ride the rollup as WARNED (**including the dest-side
  walk** - aws-cli also funnels both streams' warnings into the same result
  queue. rc 2).
- rc rollup: transfer failed + delete failed > 0 -> `BatchError`
  (`"N of M operations failed"`, first_error as `__cause__`) -> CLI rc 1.
  Warnings only -> rc 2. The interleaving of delete lines and transfer lines is
  non-deterministic in aws-cli too (goldens compare sorted).

## 6. Per-route pre-behavior

- locals3: src missing -> `The user-provided path <raw> does not exist.` (255).
  When src is a **file**, the dir-style walk warns `Skipping file <abspath>/.
  File does not exist.` (the formatted root carries a trailing separator, so
  `os.path.exists` is False) and gives rc 2 (not a hard error).
- s3local: the dest dir is created **before scanning** (aws-cli does it at the
  validation stage; the dir remains even for an empty sync). It is a
  bare exists check, not `exist_ok` - if the dest exists as a **file** it passes
  straight through, then each item fails with `[Errno 20]` and gives rc 1.
- `sync s3://b/p s3://b/p` (identical path) makes every pair identical -> silent
  rc 0 (there is no onto-itself guard like mv's).
- opens3 / s3open (a custom backend on one side, the other always S3 - the open
  route, transfer.md section 12): the custom side must declare `SORTABLE_SCAN`
  (the merge-join needs both listings byte-ordered) plus the route's I/O
  (`OPEN_READ` source / `OPEN_WRITE` dest) and `DELETE` for an `s3open` `--delete`
  destination; a dedicated gate rejects a backend short of that *before* any
  listing. `sync` requests `ScanOptions(sort=True)` from the custom side (the
  built-ins always sort and ignore it). Orphan deletes go through the backend's
  own `Storage.delete` for an `s3open` destination (an `opens3` destination is
  S3); the case-conflict gate stays scoped to a local destination. Library-only -
  the CLI never pairs a custom backend.

## 7. Known divergences (recorded only)

- **delete batching and output timing**: the batch is finalized all at once on
  flush (the final state, line set, and rc match; as with the rm precedent).
- **restored glacier**: because sync decides from the listing, `Restore` is not
  visible, so even a restored object is skip-warned - this is identical to
  aws-cli's behavior (they too look at the listing's response_data).
  `--force-glacier-transfer` is the only path.

## 8. ETag content comparison (`EtagComparison`, opt-in)

`update_filter=None` decides by size + mtime. When the decision must follow
**content**, `boto3_s3.etagcompare.EtagComparison(...)` builds a `update_filter=` strategy that
compares S3's ETag against the ETag the source would carry. It is a standalone,
opt-in building block - imported by submodule path, not part of the package root
re-export:

```python
from boto3_s3.etagcompare import EtagComparison

s3.sync(src, dest, update_filter=EtagComparison(s3))          # part_size from the profile
s3.sync(src, dest, update_filter=EtagComparison())            # 8 MiB default part size
s3.sync(src, dest, update_filter=EtagComparison(part_size=16 * 1024 * 1024))   # explicit part size
```

- **s3->s3** compares the two listings' ETags directly (no bytes read);
  **upload / download** reconstructs the local file's single- or multipart
  S3-style ETag and compares. A missing /
  non-MD5 ETag is treated as differing (never skip on an indeterminate compare).
- **`part_size`** is the multipart chunk size, fixed at construction.
  `EtagComparison(s3)` reads it from that `s3`'s active profile
  (`[s3] multipart_chunksize`, else 8 MiB) - an *explicit* config read tied to the
  `s3` you pass, not an ambient one (the library never reads config on its
  own; `EtagComparison()` is a plain 8 MiB constant). An explicit `part_size=` wins
  over `s3`. The value must match what the object was uploaded with, or every
  multipart object reads as differing (the rclone `--s3-chunk-size` constraint);
  the *effective* size has a 5 MiB floor / 5 GiB ceiling and auto-grows past S3's
  10000-part limit.
- **`check_size`** (default on) treats a known size mismatch as differing before
  any ETag work. On s3->s3 this guards against an MD5/ETag collision (equal ETag
  != equal content); on upload / download it also skips the local read + hash.
  `check_size=False` restores pure-ETag semantics.
- **Caveats.** SSE-KMS / SSE-C / DSSE objects carry an opaque, non-MD5 ETag, so
  against such a bucket every object reads as differing - use the default
  `update_filter=None` there instead. The upload / download hash runs on sync's
  calling thread unless the strategy is wrapped in `ParallelFilter` (section 10).

## 9. Native-checksum content comparison (`ChecksumComparison`, opt-in)

Where `EtagComparison` reconstructs the S3 ETag (and so must be told the multipart
part size), `boto3_s3.checksumcompare.ChecksumComparison(...)` reads the object's
**native S3 checksum** with `GetObjectAttributes` and recomputes that same
algorithm over the local file. It needs **no write side** (the checksum is one
S3 already stores), works on objects any tool uploaded with a checksum, is
**exact for multipart** objects (the part boundaries come back in `ObjectParts`,
so nothing is guessed), and works for **SSE** objects (a checksum is independent
of encryption). The cost is one `GetObjectAttributes` round-trip per object.
Like `EtagComparison` it is a standalone, opt-in building block - imported by submodule
path, not part of the package root re-export:

```python
from boto3_s3.checksumcompare import ChecksumComparison

# decide every both-sides pair by content (mtime is not consulted):
s3.sync(src, dest, update_filter=ChecksumComparison(s3, src, dest))
```

It is a **replacement** strategy, not composed with the size + time default.
Composing it via `any_of(compare_size_time, ChecksumComparison(...))` would be wrong:
`any_of` copies when *either* rule says copy, so it still copies every object
size + mtime alone would copy - including a same-content object whose only
change is its mtime - and reaches the content check only on the subset size +
mtime already skips. Content comparison can then add copies but never prevent
the mtime-driven ones, which defeats the reason to compare by content (mtime is
exactly what content comparison must not trust). `update_filter=ChecksumComparison(...)`
instead decides every both-sides pair by content; the only shortcut is the
`check_size` size pre-check (a differing size copies for free). The cost is one
`GetObjectAttributes` (plus the local hash) per both-sides pair; wrap the
strategy in `ParallelFilter` (section 10) to run those concurrently.

- **upload / download** reads the remote object's checksum and recomputes it over
  the local file: whole-file for a `FULL_OBJECT` checksum (CRC32 / CRC32C /
  CRC64NVME - the modern default), or part-by-part at the exact `ObjectParts`
  sizes for a `COMPOSITE` one (a SHA-style multipart). **s3->s3** compares the
  two objects' stored checksums directly (a `GetObjectAttributes` on each, no
  bytes read).
- **Indeterminate -> copy.** An object with no native checksum, a mismatched
  algorithm across an s3->s3 pair, an unknown algorithm, a CRC32C / CRC64NVME
  checksum beyond `pure_max_size` when `awscrt` is unavailable, or any
  `GetObjectAttributes` error (a 404, a denied `s3:GetObjectAttributes`, an SSE-C
  object that needs a key) is treated as differing - the strategy never skips on
  an indeterminate compare.
- **`check_size`** (default on) treats a known size mismatch as differing before
  any call or hash - a shortcut, and for s3->s3 a guard against a CRC collision.
- **Checksum backends.** `crc32` (zlib) and `sha1` / `sha256` (hashlib) are
  always available. `crc32c` / `crc64nvme` use **`awscrt`** when installed (a C
  path ~1000x faster, the same one S3 uses) and fall back to a bundled
  pure-Python implementation otherwise (~15-20 MiB/s) - on a par with the awscrt
  extra elsewhere (crt.md section 6): without it these still work, just slowly.
  Because that fallback is slow, **`pure_max_size`** caps it: above the cap, with
  no `awscrt`, a `crc32c` / `crc64nvme` object reads as indeterminate (copy)
  rather than being hashed. `None` (default) never caps.
- **Endpoint injection.** `ChecksumComparison(s3, src, dest)` resolves the S3 side(s)
  for their client + bucket from the same `src` / `dest` passed to `sync`
  (`bucket` is not on a `FileInfo`); pass `S3Storage` instances for a
  cross-account s3->s3 sync, exactly as `sync` does. The upload / download hash
  runs on sync's calling thread unless the strategy is wrapped in
  `ParallelFilter` (section 10).

## 10. Parallelizing a lane's decision (`ParallelFilter`, opt-in)

A lane's per-entry decision can do I/O: a content `update_filter=` strategy
(`EtagComparison` / `ChecksumComparison`) runs a `GetObjectAttributes` round-trip
and/or a local hash per pair, and a `create_filter` / `delete_filter` predicate
may read bytes / object tags / attributes to decide. By default `sync` runs every
decision on its calling thread, one entry at a time. Wrapping any lane's filter in
`boto3_s3.comparator.ParallelFilter` runs that lane's decisions on a
**caller-supplied** thread pool instead:

```python
from concurrent.futures import ThreadPoolExecutor
from boto3_s3.checksumcompare import ChecksumComparison
from boto3_s3.comparator import ParallelFilter

with ThreadPoolExecutor(16, thread_name_prefix="sync-cmp") as pool:
    s3.sync(src, dest,
        update_filter=ParallelFilter(ChecksumComparison(s3, src, dest), executor=pool),
        delete_filter=ParallelFilter(is_expired, executor=pool))   # one pool, both lanes
```

`ParallelFilter` is a value container, not a callable: `sync` recognizes it in
any of the three lanes, reads `.decide` and `.executor`, and drives the pool
itself - it is **never invoked** as a filter. Wrapping is a pure performance
transform: the same entries are acted on and the exit is the same as the bare
filter, only faster. The wrapped filter must therefore be thread-safe;
`ChecksumComparison` and `EtagComparison` are (read-only over their fields; a
botocore client is safe to share for concurrent calls).

- **The executor is the caller's.** It is a required argument, and `sync` neither
  creates nor shuts it down - reuse it across `sync` calls, share one pool across
  lanes (pass the same object to each `ParallelFilter`) or give each lane its own,
  as the application prefers. A `ParallelFilter` runs only filter decisions
  (transfers use s3transfer's own pool, deletes the deleter's worker), so the pool
  is a per-filter resource and lives on the wrapper rather than as a `sync`
  argument. A `ProcessPoolExecutor` will not work (the predicate and its client
  are not picklable) - the pool must be thread-based.
- **What each lane parallelizes.** The **update** lane pools its both-sides
  (`SyncPair`) decision, the **new** lane its `create_filter`, the
  **delete** lane its `delete_filter` - each on its own executor when wrapped. A
  lane left unwrapped decides inline on the calling thread in compare-key order.
- **Ordering.** Pooled decisions are consumed in completion order, so survivors
  submit out of compare-key order - already true of every sync (s3transfer moves
  bytes on its own pool; `on_result` fires unordered). The one visible
  consequence is `create_filter`: parallelizing it makes the `--case-conflict`
  gate's "first key wins" non-deterministic (which case-variant survives a
  case-insensitive local destination, and which warns), because the gate's
  in-flight set is order-sensitive (aws-cli's `CaseConflictSync` in the
  not-at-dest slot). The **update** lane never touches the gate, and **delete**
  orphans have no order to lose (their output interleaving is already
  non-deterministic, section 5). Exit parity is otherwise unaffected.
- **Back-pressure.** `sync` submits each pooled decision as a `Future` and keeps a
  bounded number outstanding (the distinct executors' worker counts, summed), so a
  huge listing is never materialized into futures all at once - a pool's own
  `submit` queue is unbounded, so holding that bound is `sync`'s. The bound is
  read from each pool (`ThreadPoolExecutor._max_workers`, else a constant); it is
  throughput-only, never correctness.
- **Errors / cancel.** A decision that raises aborts the sync (as the serial path
  does), surfacing when its result is consumed; decisions that have not started
  are cancelled and running decisions are awaited before the exception returns.
  `cancel_token` is polled between pairs. Graceful cancellation stops new pair
  actions and drains transfers and delete batches already accepted; immediate
  cancellation additionally asks their futures to cancel where possible.
  Outstanding decisions on the caller's pool are always awaited before sync
  returns; immediate mode first calls `cancel()` on each future, without shutting
  down the caller-owned executor.

`ParallelFilter` is a library-only building block: there is no `aws s3` flag for a
parallel filter, so the CLI never sets it and parity is not at stake.
