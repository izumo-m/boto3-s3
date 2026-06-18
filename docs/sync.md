# sync (`S3.sync` / two-layer pipeline + pure-pairing comparator) design

The established design for the `aws s3 sync` equivalent. For the CLI-side
behavior see [`cli.md`](./cli.md) section 5.9, for the transfer engine see
[`transfer.md`](./transfer.md), for the delete batch see
[`deleter.md`](./deleter.md), and for the test structure see
[`testing.md`](./testing.md). Behavior matches aws 2.34.53, cross-checked
against MinIO and the aws-cli source.

## 1. Two-layer pipeline

sync runs two layers in series: **visibility** and **pair decision**. Layers
differ in series, within a layer they combine as booleans - this separation is
the very structure that produces aws-cli's observable behavior (the exclusion
protection of `--delete`), and it is the heart of the design.

```
src listing -- filter (visibility) --+
                                     +- Comparator (pure pairing)
dst listing -- filter (visibility) --+        |
                                                          per SyncPair(key, src, dst):
                                                          compare / delete
                                                                  |
                                                   Transferrer (transfer) / delete lane
```

- **Visibility layer** (before pairing, decided on one side's `FileInfo`
  alone): the single `filter` is applied **to both streams**, each against its
  own root-relative compare key - one symmetric predicate, not a per-side pair.
  `--exclude` / `--include` thus prune the source and destination identically
  (aws evaluates the same pattern list against both sides). An entry excluded
  this way is "nonexistent" on **both** sides and is therefore protected from
  `--delete` - not a special rule on the delete-decision side but a consequence
  of symmetric visibility; a one-sided prune would manufacture a phantom
  new/delete pair, so visibility is never one-sided (per-side / per-lane
  narrowing lives in the pair layer). Folder markers (size 0, trailing `/`) are
  dropped from the S3 side here; the local walk never produces them - either way
  sync neither transfers nor deletes markers. The implementation is
  `ScanOptions.filter` (S3 side, pruned at the listing-page stage) and the
  filter immediately after the local walk.
- **Pair-decision layer** (after merge, per `SyncPair`): every decision that
  looks at both sides - size / timestamp / ETag, etc. - lives here. The default
  is aws-compatible, and this is the only layer an application swaps.

## 2. Components

| module | role |
|---|---|
| `comparator.py` | `Comparator` (pure pairing: merge-joins two key-ascending streams and emits every key as a `SyncPair`; makes no decision), the `SyncPair` / `PairFilter` types, `compare_size_time` (the internal aws-compatible default, a function reading `pair.kind`), `all_of` / `any_of` (visibility combinators). No SDK import |
| `S3.sync` in `s3.py` | orchestration: route classification -> pre-validation (src missing 255 / dest dir creation) -> build both-side entry streams (visibility applied) -> pairing -> copy decision -> item builder + gate shared with cp -> `Transferrer` submit; delete decision -> delete lane. The rollup is a `BatchError` combining transfer + delete |
| `_SyncDeletes` in `s3.py` | delete lane: an S3 dest uses `S3Deleter` (batch, lazily created on the first submit), a local dest uses a synchronous `os.remove` on the calling thread (same shape as aws-cli `LocalDeleteRequestSubmitter`). dryrun emits only a DRYRUN record. Emits with a display endpoint (`s3://bucket/key` / native path) on the `OpResult` |

Structural correspondence with aws-cli: aws-cli's `Comparator` calls three
strategies inside its merge loop (`file_at_src_and_dest` / `file_not_at_dest` /
`file_not_at_src`), whereas we separate pairing from the decision. aws-cli's
three slots map onto `compare` (a pair with a src = the unification of
at_both + not_at_dest; it can branch on whether a dst exists) and `delete`
(not_at_src).

## 3. Public API

```python
S3().sync(src, dst, *,
    delete: bool | FileFilter = False,             # False / True / predicate: lane + scope
    filter: FileFilter | None = None,              # visibility, applied to BOTH sides (same type as rm/cp)
    compare: bool | PairFilter | None = None,      # None=size+time / True=all / False=none / callable=custom
    size_only=False, exact_timestamps=False,       # tune compare=None (the default strategy)
    follow_symlinks=True, dryrun=False, page_size=1000,
    on_progress=None, on_result=None, cancel_token=None, transfer_config=None,
    **options)                          # TransferOptions (acl / sse / metadata / no_overwrite ...)
```

"Filter" is reserved for **visibility** - a predicate that narrows which entries
are in scope (`FileFilter = Callable[[FileInfo], bool]`, e.g. `GlobFilter`,
applied per side); `filter` and the `delete` lane are filters. The **copy
decision** is a separate axis, `compare`: it selects exactly one strategy (it
does not compose), where a custom strategy is a `PairFilter =
Callable[[SyncPair], bool]` that needs both sides (True = copy).

- `SyncPair(key, kind, src, dst)` - `key` is the relative compare key common to
  both sides (`/`-separated); `kind` is the sync's direction, stamped on every
  pair so a filter applies the direction-asymmetric rules without being told the
  route. `dst is None` = new, `src is None` = deletion candidate, both present =
  update decision.
- `compare` is the copy decision, a single strategy: `None` (default) is the aws
  size + last-modified judgment (it reads the direction from `pair.kind`, so it
  works across routes), tuned by the `size_only` / `exact_timestamps` options;
  `True` copies every source, `False` copies nothing; any `PairFilter` is a
  custom strategy - the content building blocks `EtagComparison` / `ChecksumComparison`
  (sections 8-9) are drop-in replacements. `size_only` / `exact_timestamps` only
  tune `compare=None`; they are ignored whenever `compare` is anything else
  (`True` / `False` / a custom strategy), since that compare replaces the
  decision wholesale. Note `None` (default) != `False` (copy nothing).
- `no_overwrite` is an orthogonal write-guard applied before `compare`: if a
  destination already exists it is never overwritten (a source-only pair still
  copies). It composes with any `compare`, and sync keeps it decision-only - no
  `IfNoneMatch` on the wire (unlike cp / mv). `S3.sync(no_overwrite=True)` works
  because it rides the shared `TransferOptions`.
- `delete` is the deletion lane in one value (aws `--delete`): `False`
  (default) deletes nothing, `True` deletes every destination-only pair, and a
  `FileFilter` deletes only the orphans it keeps - matched against the orphan's
  `FileInfo` / compare key, the same shape as `rm`'s `filter` (the delete lane
  is rm over the orphans).

Example (content-based sync + delete only old generations):

```python
from boto3_s3.etagfilter import EtagComparison

s3.sync(src, dst,
    compare=EtagComparison(s3),                       # decide by content, not size + mtime
    delete=lambda info: info.mtime < cutoff)   # delete only old orphans
```

## 4. The default decision (ported from aws-cli, pinned by measurement)

`compare=None` is the internal `compare_size_time(pair, size_only,
exact_timestamps)` (direction from `pair.kind`). `no_overwrite` is not part of
it - it is the orthogonal write-guard the sync loop applies first (section 3):

| condition | decision |
|---|---|
| `pair.dst is None` (new) | always copy (aws-cli `MissingFileSync`) |
| `size_only` (and no `exact_timestamps`) | copy only when the size differs |
| default | copy when the size differs **or** when the mtime rule says "not redundant" |

mtime rule (full float precision; `delta = dst.mtime - src.mtime`):

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
  **case-conflict gate applies only to pairs with no dst** (aws-cli inserts
  `CaseConflictSync` into the `file_not_at_dest` slot - updating the same key is
  not a conflict).
- delete: an S3 dest uses `S3Deleter` (the `DeleteObjects` batch; the known
  wire divergence from aws-cli's per-key `DeleteObject` - same as rm,
  deleter.md section 4). A local dest uses a synchronous `os.remove`. The output is
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

## 7. Known divergences (recorded only)

- **filter cross-root edge**: aws-cli applies the pattern lists of both the src
  and dst roots to both streams. We compile the pattern list once (against the
  source root, aws's "evaluate against the source") and apply that single
  filter to both sides' compare keys. For ordinary **relative** patterns this
  is exactly equivalent (a relative pattern is root-independent); an **absolute**
  pattern anchored to one root is relativized once and applied to both sides
  rather than per-root - the only divergence, and only for the rare
  absolute-pattern-in-sync case.
- **delete batching and output timing**: the batch is finalized all at once on
  flush (the final state, line set, and rc match; as with the rm precedent).
- **restored glacier**: because sync decides from the listing, `Restore` is not
  visible, so even a restored object is skip-warned - this is identical to
  aws-cli's behavior (they too look at the listing's response_data).
  `--force-glacier-transfer` is the only path.

## 8. ETag content comparison (`EtagComparison`, opt-in)

`compare=None` decides by size + mtime. When the decision must follow
**content**, `boto3_s3.etagfilter.EtagComparison(...)` builds a `compare=` strategy that
compares S3's ETag against the ETag the source would carry. It is a standalone,
opt-in building block - imported by submodule path, not part of the package root
re-export:

```python
from boto3_s3.etagfilter import EtagComparison

s3.sync(src, dst, compare=EtagComparison(s3))          # part_size from the profile
s3.sync(src, dst, compare=EtagComparison())            # 8 MiB default part size
s3.sync(src, dst, compare=EtagComparison(part_size=16 * 1024 * 1024))   # explicit part size
```

- **s3->s3** compares the two listings' ETags directly (no bytes read);
  **upload / download** reconstructs the local file's single- or multipart
  S3-style ETag and compares. A source-only pair always copies; a missing /
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
  `compare=None` there instead. The upload / download hash runs on sync's
  calling thread unless the strategy is wrapped in `ParallelCompare` (section 10).

## 9. Native-checksum content comparison (`ChecksumComparison`, opt-in)

Where `EtagComparison` reconstructs the S3 ETag (and so must be told the multipart
part size), `boto3_s3.checksumfilter.ChecksumComparison(...)` reads the object's
**native S3 checksum** with `GetObjectAttributes` and recomputes that same
algorithm over the local file. It needs **no write side** (the checksum is one
S3 already stores), works on objects any tool uploaded with a checksum, is
**exact for multipart** objects (the part boundaries come back in `ObjectParts`,
so nothing is guessed), and works for **SSE** objects (a checksum is independent
of encryption). The cost is one `GetObjectAttributes` round-trip per object.
Like `EtagComparison` it is a standalone, opt-in building block - imported by submodule
path, not part of the package root re-export:

```python
from boto3_s3.checksumfilter import ChecksumComparison

# decide every both-sides pair by content (mtime is not consulted):
s3.sync(src, dst, compare=ChecksumComparison(s3, src, dst))
```

It is a **replacement** strategy, not composed with the size + time default.
Composing it via `any_of(compare_size_time, ChecksumComparison(...))` would be wrong:
`any_of` copies when *either* rule says copy, so it still copies every object
size + mtime alone would copy - including a same-content object whose only
change is its mtime - and reaches the content check only on the subset size +
mtime already skips. Content comparison can then add copies but never prevent
the mtime-driven ones, which defeats the reason to compare by content (mtime is
exactly what content comparison must not trust). `compare=ChecksumComparison(...)`
instead decides every both-sides pair by content; the only shortcut is the
`check_size` size pre-check (a differing size copies for free). The cost is one
`GetObjectAttributes` (plus the local hash) per both-sides pair; wrap the
strategy in `ParallelCompare` (section 10) to run those concurrently.

- **upload / download** reads the remote object's checksum and recomputes it over
  the local file: whole-file for a `FULL_OBJECT` checksum (CRC32 / CRC32C /
  CRC64NVME - the modern default), or part-by-part at the exact `ObjectParts`
  sizes for a `COMPOSITE` one (a SHA-style multipart). **s3->s3** compares the
  two objects' stored checksums directly (a `GetObjectAttributes` on each, no
  bytes read). A source-only pair always copies.
- **Indeterminate -> copy.** An object with no native checksum, a mismatched
  algorithm across an s3->s3 pair, an algorithm that cannot be computed locally,
  or any `GetObjectAttributes` error (a 404, a denied `s3:GetObjectAttributes`,
  an SSE-C object that needs a key) is treated as differing - the strategy never
  skips on an indeterminate compare.
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
- **Endpoint injection.** `ChecksumComparison(s3, src, dst)` resolves the S3 side(s)
  for their client + bucket from the same `src` / `dst` passed to `sync`
  (`bucket` is not on a `FileInfo`); pass `S3Storage` instances for a
  cross-account s3->s3 sync, exactly as `sync` does. The upload / download hash
  runs on sync's calling thread unless the strategy is wrapped in
  `ParallelCompare` (section 10).

## 10. Parallelizing the content compare (`ParallelCompare`, opt-in)

A content compare (`EtagComparison` / `ChecksumComparison`) does per-pair I/O - a
`GetObjectAttributes` round-trip and/or a local hash - and by default `sync` runs
every decision on its calling thread, one pair at a time. Wrapping the strategy
in `boto3_s3.comparator.ParallelCompare` runs the **both-sides (update)**
decisions on a thread pool instead:

```python
from boto3_s3.checksumfilter import ChecksumComparison
from boto3_s3.comparator import ParallelCompare

s3.sync(src, dst, compare=ParallelCompare(ChecksumComparison(s3, src, dst), workers=16))
```

`ParallelCompare` is a value container, not a callable: `sync` recognizes it,
reads `.compare` and `.workers`, and drives the pool itself - it is **never
invoked** as a `PairFilter`. Wrapping is a pure performance transform: the same
pairs are copied and the exit is the same as the bare strategy, only faster. The
wrapped strategy must therefore be thread-safe; `ChecksumComparison` and
`EtagComparison` are (read-only over their fields; a botocore client is safe to
share for concurrent calls).

- **What is parallelized.** Only the both-sides (`dst is not None`) decision -
  the one that does I/O. **New** pairs (`dst is None`) and the **delete** lane
  stay on the calling thread in compare-key order. That is deliberate: a content
  strategy returns "copy" for a new pair with no I/O, so there is nothing to
  parallelize there; and keeping new pairs in key order keeps the
  `--case-conflict` gate's "first key wins" deterministic (its seen-set is
  order-sensitive, like aws-cli's `CaseConflictSync` in the not-at-dest slot).
- **Ordering.** Pooled decisions are consumed in completion order, so transfers
  submit out of compare-key order - already true of every sync (s3transfer moves
  bytes on its own pool; `on_result` fires unordered). Exit parity is unaffected.
- **`workers`** defaults to the sync's `transfer_config.max_concurrency` (10).
  The pooled `GetObjectAttributes` calls use the strategy's own client, so for a
  `workers` above its connection-pool size (botocore's default 10) build the
  strategy with a client sized to match.
- **Errors / cancel.** A decision that raises aborts the sync (as the serial path
  does), surfacing when its result is consumed; `cancel_token` is polled between
  pairs.

`ParallelCompare` is a library-only building block: there is no `aws s3` flag for
content comparison, so the CLI never sets it and parity is not at stake.
