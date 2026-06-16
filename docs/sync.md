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
                                                          copy_filter / delete
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
| `comparator.py` | `Comparator` (pure pairing: merge-joins two key-ascending streams and emits every key as a `SyncPair`; makes no decision), the `SyncPair` / `PairFilter` types, `DefaultCopyFilter` (the aws-compatible decision, a callable reading `pair.kind`), `all_of` / `any_of` (predicate combinators). No SDK import |
| `S3.sync` in `s3.py` | orchestration: route classification -> pre-validation (src missing 255 / dest dir creation) -> build both-side entry streams (visibility applied) -> pairing -> copy decision -> item builder + gate shared with cp -> `Transferrer` submit; delete decision -> delete lane. The rollup is a `BatchError` combining transfer + delete |
| `_SyncDeletes` in `s3.py` | delete lane: an S3 dest uses `S3Deleter` (batch, lazily created on the first submit), a local dest uses a synchronous `os.remove` on the calling thread (same shape as aws-cli `LocalDeleteRequestSubmitter`). dryrun emits only a DRYRUN record. Emits with a display endpoint (`s3://bucket/key` / native path) on the `OpResult` |

Structural correspondence with aws-cli: aws-cli's `Comparator` calls three
strategies inside its merge loop (`file_at_src_and_dest` / `file_not_at_dest` /
`file_not_at_src`), whereas we separate pairing from the decision. aws-cli's
three slots map onto `copy_filter` (a pair with a src = the unification of
at_both + not_at_dest; it can branch on whether a dst exists) and `delete`
(not_at_src).

## 3. Public API

```python
S3().sync(src, dst, *,
    delete: bool | FileFilter = False,             # False / True / predicate: lane + scope
    filter: FileFilter | None = None,              # visibility, applied to BOTH sides (same type as rm/cp)
    copy_filter: bool | PairFilter | None = None,  # None=default / True=all / False=none / callable=custom
    follow_symlinks=True, dryrun=False, page_size=1000,
    on_progress=None, on_result=None, cancel_token=None, transfer_config=None,
    **options)                                # TransferOptions (acl / sse / metadata ...)
```

The naming is unified on the filter vocabulary: "a predicate that narrows the
operation target = filter". Visibility and the delete
lane are `FileInfo` predicates (`FileFilter` = a Matcher or callable, one side);
the copy decision is a `PairFilter = Callable[[SyncPair], bool]` that needs both
sides (True = act).

- `SyncPair(key, kind, src, dst)` - `key` is the relative compare key common to
  both sides (`/`-separated); `kind` is the sync's direction, stamped on every
  pair so a filter applies the direction-asymmetric rules without being told the
  route. `dst is None` = new, `src is None` = deletion candidate, both present =
  update decision.
- `copy_filter` is the copy decision: `None` (default) uses `DefaultCopyFilter()`
  (the aws judgment; it reads the direction from `pair.kind`, so it composes
  across routes); `True` copies every source, `False` copies nothing; any other
  `PairFilter` is custom. Tune or compose the default as
  `DefaultCopyFilter(size_only=..., exact_timestamps=..., no_overwrite=...)`
  (e.g. `any_of(DefaultCopyFilter(), ...)`). Note `None` (default) != `False`
  (copy nothing).
- `delete` is the deletion lane in one value (aws `--delete`): `False`
  (default) deletes nothing, `True` deletes every destination-only pair, and a
  `FileFilter` deletes only the orphans it keeps - matched against the orphan's
  `FileInfo` / compare key, the same shape as `rm`'s `filter` (the delete lane
  is rm over the orphans).

Example of composing the building blocks (ETag-based sync + delete only old
generations):

```python
from boto3_s3 import DefaultCopyFilter, any_of

etag_differs = lambda p: p.dst is None or p.src.etag != p.dst.etag
s3.sync(src, dst,
    copy_filter=any_of(DefaultCopyFilter(), etag_differs),   # DefaultCopyFilter() reads pair.kind
    delete=lambda info: info.mtime < cutoff)                 # delete only old orphans
```

## 4. The default decision (ported from aws-cli, pinned by measurement)

`DefaultCopyFilter(size_only, exact_timestamps, no_overwrite)` (direction from
`pair.kind`):

| condition | decision |
|---|---|
| `pair.dst is None` (new) | always copy (aws-cli `MissingFileSync`) |
| `no_overwrite` | if a dst exists, never copy (aws-cli `NoOverwriteSync`; takes priority over the others) |
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
  matcher to both sides' compare keys. For ordinary **relative** patterns this
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
