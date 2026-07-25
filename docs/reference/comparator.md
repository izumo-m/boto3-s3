# Sync pairing and update strategies

`sync` prunes each side's listing independently, merge-joins what survives by
compare key, and then decides per pair. This page specifies the types that
second stage is built from: the merge-join, the three pair shapes it emits, the
predicate type the update lane takes, the three strategies that implement that
predicate, the wrapper that moves a lane's decisions onto a thread pool, and
the two predicate combinators.

The narrative — which lane does what, and how the defaults reproduce
`aws s3 sync` — is in [`../library/sync.md`](../library/sync.md) and
[`../library/sync-content.md`](../library/sync-content.md); the pipeline's
rationale is in [`../../design/sync.md`](../../design/sync.md). The `sync`
operation's own parameters, including which lane each filter argument feeds,
are in [`./operations/sync.md`](./operations/sync.md).

Every symbol below is exported from the `boto3_s3` root. `Comparator`, the
three pair shapes, `MergedPair`, `PairFilter`, `ParallelFilter`, `all_of` and
`any_of` are additionally exported from `boto3_s3.comparator`; each of the
three update strategies from its own module — `AwsCliComparison` from
`boto3_s3.awsclicompare`, `EtagComparison` from `boto3_s3.etagcompare`,
`ChecksumComparison` from `boto3_s3.checksumcompare`.

## Comparator

The merge-join `sync` pairs with: it consumes two key-ordered
`(compare_key, info)` streams and yields one `MergedPair` per pairing step,
the shape of each telling which side or sides hold that key. It is a pure
pairer. It compares keys and nothing else — no size, timestamp, or content
comparison happens here, and no key is dropped; every entry of both inputs
comes out. Judging what to do with a pair belongs to the lane filters that
consume the stream. The one thing `Comparator` adds to the entries is
`transfer_type`, stamped on every emitted pair as context for those filters.

```python
@dataclass(frozen=True)
class Comparator:
    transfer_type: TransferType

    def compare(
        self,
        src_entries: Iterable[tuple[str, FileInfo]],
        dest_entries: Iterable[tuple[str, FileInfo]],
    ) -> Iterator[MergedPair]: ...
```

`transfer_type` is the run's direction. `S3.sync` constructs the comparator
with the direction its route classification produced — `UPLOAD`, `DOWNLOAD` or
`COPY` (see [`./options.md`](./options.md) for the enum). The field is stamped
onto emitted pairs verbatim and is not validated or interpreted, so a caller
driving `Comparator` directly may pass any `TransferType` member and will read
it back on every pair.

The dataclass is frozen and takes the field positionally
(`Comparator(transfer_type)`) or by keyword. It holds no other state, so one
instance can drive several `compare` calls.

### compare(src_entries, dest_entries)

Yields each pairing step of the two streams:

- a key present on both sides yields a `SyncPair` carrying both entries;
- a key present only in `src_entries` yields a `SrcOnlyPair`;
- a key present only in `dest_entries` yields a `DestOnlyPair`.

Each stream item is a `(compare_key, info)` tuple. The key is what the join
runs on and what lands on the emitted pair's `compare_key`; the `FileInfo`
rides through untouched onto the pair's `src` / `dest`. The two are supplied
separately because the join never derives the key from the entry — the streams
`sync` builds take it from each entry's `FileInfo.compare_key`
([`./results.md`](./results.md)), but a caller driving `compare` directly
chooses the key it joins on.

**Both inputs must ascend by key**, in the UTF-8 byte order a `SORTABLE_SCAN`
backend promises for a scan requested with `ScanOptions(sort=True)`
([`./storage.md`](./storage.md)); S3 listings arrive in that order and the local
walk sorts to match. Given ordered inputs the output is ordered too: emitted
pairs ascend by `compare_key`. Given unordered input the join mis-pairs — the
same key can surface as a `SrcOnlyPair` and a `DestOnlyPair` instead of one
`SyncPair`, which under `sync`'s delete lane means deleting an entry that exists
on both sides. This is why `sync` requires `SORTABLE_SCAN` of a custom backend
and rejects S3 Express directory buckets outright.

Both streams are consumed lazily and only one entry of each is held at a time,
so page-by-page listings pair without either side being materialized. Draining
the returned iterator consumes both inputs to exhaustion, and each consumed
entry contributes to exactly one emitted pair. Abandoning the iterator early
leaves the rest of both inputs unconsumed; releasing whatever they hold open is
the caller's business, which is what `sync` does with its two listing
generators.

Raises: an `AssertionError` when a side yields a key smaller than the one
before. This is a development guard against a backend that declares
`SORTABLE_SCAN` and then yields out of order; it is active whenever assertions
are (the default) and is removed entirely under `python -O`, where unordered
input mis-pairs silently instead. Anything the input iterables raise propagates
unchanged — `compare` catches nothing.

## SyncPair

A compare key present on **both** sides: the update pair, and the only shape
`sync`'s `update_filter` ever sees. Both sides are present by construction —
there is no `None` slot and no "which side is missing" case — so a filter reads
`pair.src.size` / `pair.dest.mtime` directly.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class SyncPair:
    compare_key: str
    transfer_type: TransferType
    src: FileInfo
    dest: FileInfo
```

`compare_key` is the key the two sides matched on: the entry's key relative to
the directory being enumerated for a local side, and `Prefix`-relative for an
S3 side, `/`-separated on every platform. Both entries share it by
construction, which is what lets a name-based filter work without knowing where
either side lives; each side's own full identifier stays on its `FileInfo.key`.

`transfer_type` is the run's direction as stamped by the `Comparator`. It is on
the pair so that a filter can apply direction-asymmetric rules — the default
size-and-time judgment is one — without being told the route separately.

`src` and `dest` are the two sides' listing entries
([`./results.md`](./results.md)). An entry produced by a scan carries the
backend it was listed from on its `storage` attribute, so a filter that needs
bytes rather than listing metadata opens that side through `pair.src.storage` /
`pair.dest.storage` ([`./storage.md`](./storage.md)) — which is how the content
strategies compare against any backend, not only a local filesystem. It is
`None` on a hand-built `FileInfo`, which the content strategies treat as
unreadable and copy.

The dataclass is frozen and keyword-only, so construction is
`SyncPair(compare_key=..., transfer_type=..., src=..., dest=...)`. It defines
`slots`, so no attribute can be added to an instance.

## SrcOnlyPair

A compare key present only on the source side: a new entry, which `sync` routes
to the create lane (`create_filter`).

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class SrcOnlyPair:
    compare_key: str
    transfer_type: TransferType
    src: FileInfo
```

`compare_key` and `transfer_type` carry what they carry on `SyncPair`. `src` is
the source listing entry.

**There is no `dest` attribute at all** — not a `None`-valued one. The missing
side is absent from the shape, so reading `pair.dest` raises `AttributeError`
rather than returning `None`; `slots` additionally keeps one from being attached
to an instance. This is what makes the shape itself the discriminator: code that
holds a `SrcOnlyPair` knows statically that only a source exists.

The create lane's filter is a `FileFilter` over that one side
([`./filters.md`](./filters.md)), not a `PairFilter`, so a caller normally sees
the `FileInfo` rather than this pair; the shape is what `Comparator.compare`
yields and what `sync` routes on.

## DestOnlyPair

A compare key present only on the destination side: an orphan, which `sync`
routes to the delete lane (`delete_filter`).

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class DestOnlyPair:
    compare_key: str
    transfer_type: TransferType
    dest: FileInfo
```

`compare_key` and `transfer_type` carry what they carry on `SyncPair`. `dest`
is the destination listing entry.

**There is no `src` attribute at all**, the mirror of `SrcOnlyPair`: the
missing side is absent from the shape, and reading `pair.src` raises
`AttributeError`. As with the create lane, the delete lane's filter is a
`FileFilter` over the one side ([`./filters.md`](./filters.md)).

An entry that the visibility `filter` pruned is not in the destination stream
at all, so it never becomes a `DestOnlyPair` and cannot be deleted — the
protection `aws s3 sync` gives excluded files falls out of this layering. See
[`./operations/sync.md`](./operations/sync.md).

## MergedPair

The union of the three pair shapes — what `Comparator.compare` yields, and the
type to annotate anything that handles a pairing step generically.

```python
MergedPair = SrcOnlyPair | SyncPair | DestOnlyPair
```

It is a type alias, not a base class: the three dataclasses share no common
ancestor beyond `object` and no common `src`/`dest` interface. Discrimination
is therefore by `isinstance` against a concrete class (or an equivalent
structural `match`), and it is exhaustive — every value `compare` yields is one
of exactly these three. Which class it is, is the whole statement of which side
or sides hold the key, so no separate flag or sentinel needs consulting.

`sync` routes on precisely this: `SrcOnlyPair` to the create lane, `SyncPair`
to the update lane, `DestOnlyPair` to the delete lane. Because the classes are
unrelated, the order in which a consumer tests them does not matter.

## PairFilter

The update lane's judgment: a predicate over a both-sides pair where `True`
means copy the source over the destination and `False` means leave the
destination as it is.

```python
PairFilter = Callable[[SyncPair], bool]
```

A type alias for the callable shape, so any function, lambda, or object with a
matching `__call__` is one; nothing needs to be registered or subclassed. It is
what `S3.sync(update_filter=...)` accepts besides the `True` / `False` / `None`
shorthands ([`./operations/sync.md`](./operations/sync.md)) — `None`, the
default, selects the `aws s3` size-and-last-modified judgment, available
explicitly as [`AwsCliComparison`](#awsclicomparison).

The lane is single-strategy: `update_filter` selects one `PairFilter`, and the
default rule is replaced rather than combined. The content strategies
[`EtagComparison`](#etagcomparison) and
[`ChecksumComparison`](#checksumcomparison) are `PairFilter`s of this kind.

Contract of a call: it is invoked once per `SyncPair` that reaches the update
lane, on `sync`'s calling thread and in ascending `compare_key` order, unless
the filter was wrapped in `ParallelFilter`. Its argument always has both sides
populated. Returning `False` drops the pair silently — no transfer is submitted
and no `on_result` record is emitted for it. Raising aborts the run: the
exception propagates out of `sync` rather than being recorded as a per-item
failure.

Two interactions are worth stating because they can make the filter's answer
moot. `no_overwrite=True` removes the update lane entirely, so an
`update_filter` passed alongside it is never consulted. And the visibility
`filter` runs before pairing on both sides, so a pair only reaches this
predicate if both of its entries survived it.

The create and delete lanes take a `FileFilter` instead — a predicate over the
one `FileInfo` their shape has ([`./filters.md`](./filters.md)).

## AwsCliComparison

The `aws s3` size-and-last-modified judgment as an object, and the explicit
form of `sync`'s `update_filter=None`: `update_filter=AwsCliComparison()`
decides exactly as the default does. Passing it explicitly is how the two
`aws s3 sync` tuners are reached, since they are constructor arguments here
rather than parameters of `sync`. The narrative treatment is
[`../library/sync.md`](../library/sync.md).

```python
class AwsCliComparison:
    def __init__(
        self, *, size_only: bool = False, exact_timestamps: bool = False
    ) -> None: ...

    def __call__(self, pair: SyncPair) -> bool: ...
```

**The decision.** A pair is copied when the two sides' sizes differ, or when
the last-modified rule does not rule the copy out. That time rule is
direction-asymmetric and reads the direction from `pair.transfer_type`, not
from which side is the S3 one: for a `DOWNLOAD` pair a copy is redundant when
the destination is at least as old as the source; for every other
`transfer_type` — `UPLOAD` and `COPY` being the two `sync` produces — it is
redundant when the destination is at least as new. Times are compared at full
`timedelta` precision.

`size_only` and `exact_timestamps` are keyword-only and mirror
`aws s3 sync --size-only` / `--exact-timestamps`. `size_only` decides on size
alone and never consults the times. `exact_timestamps` tightens the `DOWNLOAD`
rule to require exactly equal times and leaves upload and copy pairs unchanged.
Setting both makes `size_only` inert: `exact_timestamps` wins, which is the
AWS CLI's own override order. The two are arguments of one object rather than
two `sync` parameters because they tune this judgment only — a content strategy
replaces the judgment outright and has nothing for them to tune.

A missing `size` on either side counts as differing, and so does a missing
`mtime` wherever the time rule is consulted; the pair is copied in both cases.

The two arguments are stored as attributes of the same names and are read on
every call, so changing one between calls changes later decisions; the class
defines `slots`, so no other attribute can be attached. Nothing else is kept,
the judgment reads only values the two listing entries already carry, and it
raises nothing of its own — so there is no I/O for a
[`ParallelFilter`](#parallelfilter) to overlap here.

## EtagComparison

A content strategy that decides by ETag: it copies a pair when the S3 side's
ETag does not match the ETag the other side's bytes would carry. It is a
replacement for the size-and-time judgment, not a composition with it — the
update lane selects exactly one strategy. Choosing between the two content
strategies is [`../library/sync-content.md`](../library/sync-content.md).

```python
class EtagComparison:
    def __init__(
        self,
        s3: S3 | None = None,
        *,
        part_size: int | None = None,
        check_size: bool = True,
    ) -> None: ...

    def __call__(self, pair: SyncPair) -> bool: ...
```

`s3` is consulted for one thing only: the multipart part size. It is read as
`[s3] multipart_chunksize` from that instance's active profile through
[`S3.aws_config()`](./s3.md#aws_config), falling back to `DEFAULT_PART_SIZE`.
The read happens in the constructor and is tied to the passed `S3`; no ambient
or default session is consulted, and nothing else is taken from the object.

`part_size` pins the part size explicitly. It overrides the `s3`-derived value,
so `EtagComparison(s3, part_size=...)` uses the explicit value and does not
consult `s3` at all. With neither argument given the part size is
`boto3_s3.etagcompare.DEFAULT_PART_SIZE` — 8 MiB, boto3's own
`TransferConfig.multipart_chunksize` default. That constant is exported from
`boto3_s3.etagcompare` and not from the package root.

`check_size`, `True` by default, treats a pair whose two sides both report a
size and whose sizes differ as differing, before any ETag work. On an upload or
download that also skips the read and hash; on an s3-to-s3 pair it is a
correctness guard rather than a shortcut, since ETag equality alone can falsely
skip a copy — MD5 can collide, and the size is independent evidence.
`check_size=False` restores pure-ETag semantics.

**How a pair is judged.** Which side is the S3 object is decided by entry type
— the side that is an [`S3FileInfo`](./results.md#s3fileinfo) — not by
`transfer_type`. After the `check_size` step:

- both sides `S3FileInfo`: the two listings' stored ETags are compared as
  strings, whatever form they take, and no bytes are read.
- exactly one side `S3FileInfo`: the other is the readable side. Its bytes are
  read through the backend stamped on its `storage` attribute, as
  `storage.open(info.compare_key, "rb")` ([`./storage.md`](./storage.md)), so
  any backend works and not only a local filesystem. An S3 ETag bearing a
  `-<n>` suffix is reconstructed as `MD5(concatenated part MD5s) + "-<n>"` at
  the effective part size; any other ETag as the hex MD5 of the whole stream.
- neither side `S3FileInfo`: there is no stored ETag to compare against, so the
  pair is copied.

**What reads as differing**, so that the strategy never skips on a comparison
it could not make: an absent `etag` on either side of an s3-to-s3 pair; an
absent or empty `etag` on the S3 side of an upload or download; a readable side
whose `storage` or `compare_key` is `None`, which is what a hand-built
`FileInfo` gives; and a multipart ETag whose readable side reports no `size`,
without which the part split cannot be reconstructed.

**The effective part size.** On the multipart branch the held `part_size` is a
request rather than the split used: per file it is adjusted the way an upload
would chunk that file — grown until the file fits S3's 10000-part limit, then
clamped into S3's 5 MiB to 5 GiB part bounds (s3transfer's
`ChunksizeAdjuster`). A `part_size` below 5 MiB therefore compares at 5 MiB.
The value must still reproduce the boundaries the object was uploaded with:
supplying that upload's `multipart_chunksize` is the reliable way, and a
multipart object uploaded with a different one reconstructs to a different ETag
and is re-copied on every run. A single-part object compares regardless of the
setting.

**Which objects it can judge.** An ETag is a reconstructible MD5 only for an
unencrypted or SSE-S3 object. SSE-KMS, SSE-C and DSSE objects carry an opaque
ETag that is not an MD5, and a listing does not reveal an object's encryption,
so on upload and download every such object reads as differing and is copied on
every run; an s3-to-s3 pair still compares the two opaque strings directly and
does skip when they are equal. Use the default judgment or
[`ChecksumComparison`](#checksumcomparison) against such a bucket.

Raises, from the constructor:
[`InvalidConfigError`](./exceptions.md#invalidconfigerror) when `s3` is given
without `part_size` and the config read fails — a `[s3] multipart_chunksize`
that is not a size, or a set-but-missing profile where the `S3` was built
without a session.

Raises, from a call: an `OSError` raised while reading the readable side is
translated into the library taxonomy —
[`NotFoundError`](./exceptions.md#notfounderror) for a file removed between the
listing and the compare,
[`AccessDeniedError`](./exceptions.md#accessdeniederror) for a permission
failure, [`TransportError`](./exceptions.md#transporterror) otherwise —
carrying `operation="sync"` and the entry's key. Whatever else a
custom backend raises propagates unchanged. Either way the exception aborts the
`sync` run rather than being recorded as a per-item failure, as
[`PairFilter`](#pairfilter) states for any raising predicate.

The object holds no mutable state, so it is safe to wrap in
[`ParallelFilter`](#parallelfilter), which is how the per-pair reads are
overlapped; unwrapped, each read runs on `sync`'s calling thread.

## ChecksumComparison

A content strategy that decides by the native checksum S3 already stores: it
reads the S3 side's checksum with `GetObjectAttributes` and recomputes that
same algorithm over the other side's bytes. Like `EtagComparison` it replaces
the size-and-time judgment rather than composing with it. Unlike it, no part
size has to be supplied — a multipart object's part boundaries come back from
S3, so the comparison is exact — encryption does not affect it, and it works
against objects any tool uploaded with a checksum. The cost is one
`GetObjectAttributes` per object consulted. Choosing between the two content
strategies is [`../library/sync-content.md`](../library/sync-content.md).

```python
class ChecksumComparison:
    def __init__(
        self,
        s3: S3,
        src: Location,
        dest: Location,
        *,
        check_size: bool = True,
        pure_max_size: int | None = None,
        request_payer: str | None = None,
    ) -> None: ...

    def __call__(self, pair: SyncPair) -> bool: ...
```

`s3`, `src` and `dest` are required and positional: pass the same three values
the `sync` call gets. Each location is resolved through
[`S3.resolve`](./s3.md#resolveloc), so a subclass that overrides `resolve`
reaches this strategy too, and a cross-account s3-to-s3 sync works by passing
the same two [`S3Storage`](./storage.md#s3storage) objects here that are passed
to `sync`. The endpoint cannot be read off a pair — a `FileInfo` carries no
bucket — which is why it is injected explicitly.

The constructor also builds the client of each resolved side that is an
`S3Storage`, rather than leaving it to the first decision. The decisions may
run on a [`ParallelFilter`](#parallelfilter) pool, where a first client build
would race boto3's client construction; building here is what makes the
strategy safe to wrap. It also means a client-build failure surfaces on the
constructing thread.

`check_size`, `True` by default, behaves as it does for
[`EtagComparison`](#etagcomparison): differing known sizes are treated as
differing before any `GetObjectAttributes` call or hashing, which is a shortcut
on upload and download and, on an s3-to-s3 pair, a guard against a CRC
collision.

`pure_max_size` bounds the pure-Python `crc32c` / `crc64nvme` fallback, which
is reached only when `awscrt` is not installed. An object of one of those two
algorithms whose size is above the cap — or whose size is unknown, which the
cap counts as above it — is treated as indeterminate and copied instead of
being hashed. `None`, the default, never caps. It affects nothing else:
`crc32`, `sha1` and `sha256` always compute, and so do `crc32c` / `crc64nvme`
when `awscrt` is present.

`request_payer` is forwarded as the `RequestPayer` parameter of every
`GetObjectAttributes` call, including the `ObjectParts` continuation pages;
`None` omits it.

**How a pair is judged.** As with `EtagComparison`, the S3 side is the side
that is an [`S3FileInfo`](./results.md#s3fileinfo) and the `check_size` step
runs first.

- both sides `S3FileInfo`: each object's stored checksum is fetched and the two
  are compared by algorithm and value string. No bytes are read, and the
  `ObjectParts` pagination is skipped since the value strings compare directly.
- exactly one side `S3FileInfo`: that side's checksum is fetched and the
  readable side's bytes are read through its `storage`
  ([`./storage.md`](./storage.md)) and hashed with the same algorithm —
  whole-file for a `FULL_OBJECT` checksum, part by part at the sizes
  `GetObjectAttributes` reported for a `COMPOSITE` one. Which endpoint the
  fetch addresses is taken from `pair.transfer_type`: the `dest` location for
  `UPLOAD`, the `src` location for anything else.
- neither side `S3FileInfo`: the pair is copied.

**What reads as indeterminate**, and is therefore copied rather than skipped:
an object carrying no native checksum; an algorithm outside `crc32`, `crc32c`,
`crc64nvme`, `sha1` and `sha256`; a `crc32c` / `crc64nvme` object past
`pure_max_size` with no `awscrt`; a `COMPOSITE` object whose part sizes could
not be read in full; two S3 sides whose stored checksums use different
algorithms; a readable side whose `storage` or `compare_key` is `None`; a
readable side longer than the sum of the parts, an appended tail the per-part
digests would not otherwise see; and any `ClientError` from a
`GetObjectAttributes` call — a 404, a denied `s3:GetObjectAttributes`, an
SSE-C object whose customer key was not supplied, a failure part way through
the pagination. A per-object rejection never aborts the run.

Checksum computation is `zlib` for `crc32` and `hashlib` for `sha1` / `sha256`.
`crc32c` and `crc64nvme` use `awscrt` when it is installed and a bundled
pure-Python implementation otherwise, which is what `pure_max_size` bounds.

Raises, from the constructor: whatever `S3.resolve` and the client build raise
for locations, credentials, a region or an `endpoint_url` that do not resolve —
[`ConfigurationError`](./exceptions.md#configurationerror),
[`InvalidConfigError`](./exceptions.md#invalidconfigerror) or the
[`Boto3S3Error`](./exceptions.md#boto3s3error) base — plus the `TypeError`
`os.fspath` raises for an argument that is not a `Location`.

Raises, from a call: a `BotoCoreError` from `GetObjectAttributes` — absent
credentials, an unreachable endpoint, a read timeout — is not a per-object
verdict but a broken environment, so it is translated into the taxonomy and
raised, aborting the run instead of silently copying everything. An `OSError`
reading the readable side is translated exactly as it is for
[`EtagComparison`](#etagcomparison).

## ParallelFilter

A wrapper that runs one lane's per-entry decision on a caller-supplied thread
pool instead of on `sync`'s calling thread. It exists for lanes whose predicate
does I/O per entry: a content `update_filter` strategy, or a create / delete
filter that reads bytes, tags, or object attributes to decide.

```python
@dataclass(frozen=True, slots=True)
class ParallelFilter(Generic[_T]):
    decide: Callable[[_T], bool]
    executor: Executor
```

**It is a value container, not a callable.** It has no `__call__` and is never
invoked as a filter. `S3.sync` recognizes the type in any of `create_filter`,
`update_filter` and `delete_filter`, reads `decide` and `executor`, and drives
the pool itself; nothing else in the library consumes one.

`decide` is the wrapped predicate — exactly what would have been passed
unwrapped. `_T` follows the lane: `SyncPair` for `update_filter` (a
`PairFilter`), `FileInfo` for `create_filter` / `delete_filter` (a
`FileFilter`). Because it runs on the pool's threads it must be thread-safe;
[`EtagComparison`](#etagcomparison) and
[`ChecksumComparison`](#checksumcomparison) are.

`executor` is required and stays the caller's. `sync` neither creates nor shuts
it down, so one pool can be reused across `sync` calls and shared by several
lanes — pass the same object to each `ParallelFilter` — or each lane can be
given its own. Outstanding decisions are always awaited before `sync` returns,
so none outlive the call.

Two constraints on the pool. It must be thread-based: a `ProcessPoolExecutor`
cannot work, because the wrapped predicate and the entries it decides on hold
live handles — S3 clients, a `FileInfo.storage` backend — that do not pickle.
And the `sync` call itself must not be running as a task on the same pool: the
lane blocks its own thread waiting for decisions that need a free worker, so a
bounded pool driving both can deadlock.

`sync` keeps a bounded number of decisions outstanding rather than submitting
the whole listing at once, so pairing a large listing does not materialize a
future per entry.

**What wrapping changes.** For a stateless predicate the decisions themselves
are unchanged: the same entries are acted on and the run ends the same way,
only faster. A stateful predicate can observe the concurrency. What does change
is ordering, in the ways below and no others.

Pooled decisions are consumed in completion order, so surviving entries are
submitted out of `compare_key` order. For real transfers and for batched S3
deletes that is not observable — the transfer engine and the deleter reorder
work on their own anyway, and `on_result` is not ordered in either case.
Records the lane's action emits inline follow that ordering: dry-run records,
and orphan deletions against a local or custom destination, still fire on
`sync`'s calling thread — only the decision moves to the pool — but arrive in
the completion order of the pooled decisions rather than in `compare_key`
order. A lane left unwrapped decides inline in `compare_key` order.

Parallelizing `create_filter` specifically makes the case-conflict gate's
"first key wins" resolution non-deterministic — which variant of a
case-insensitively colliding key survives against a case-insensitive local
destination, and which one warns — because that gate depends on the order
entries reach it. The update lane never touches that gate.

**Failure and cancellation.** A decision that raises aborts the run as the
serial path would: the exception surfaces when that decision's result is
consumed, decisions that have not started are cancelled, and running ones are
awaited first. Cancellation through `cancel_token` behaves the same way —
graceful mode awaits outstanding decisions, immediate mode first asks their
futures to cancel — and in neither case is the caller's executor shut down.

## all_of

Combines same-signature predicates into one that passes only when every
predicate passes.

```python
def all_of(*predicates: Callable[[_T], bool]) -> Callable[[_T], bool]: ...
```

The returned callable takes the same single argument the inputs take and
returns a `bool`. Predicates are called in argument order and evaluation stops
at the first `False`, so a cheap predicate placed first can spare an expensive
one — relevant when a predicate does I/O. With no predicates at all the result
always passes, matching `all([])`.

It is generic in the argument type, so it composes any single-argument
predicate: the `filter=` visibility predicates over `FileInfo` that
[`./filters.md`](./filters.md) describes, `FileFilter`s destined for
`create_filter` / `delete_filter`, or `PairFilter`s. Mixing predicates that
take different argument types is a type error, not a runtime one.

The result is a plain function closing over the given predicates: it exposes
none of the structure a `GlobFilter` has, and it can in turn be wrapped in
`ParallelFilter` like any other predicate.

Note that the update lane is a place to compose with care: `update_filter`
selects one strategy, and combining the default size-and-time rule with a
content strategy does not produce "both checks". See `any_of` below.

## any_of

The `or` counterpart of `all_of`: passes when at least one predicate passes.

```python
def any_of(*predicates: Callable[[_T], bool]) -> Callable[[_T], bool]: ...
```

Predicates are called in argument order and evaluation stops at the first
`True`. With no predicates the result never passes, matching `any([])`.
Everything `all_of` states about argument types and the returned callable holds
here.

One composition to avoid: `any_of(<default rule>, <content strategy>)` as an
`update_filter`. Since it copies when *either* rule says copy, every entry the
size-and-time rule would copy is still copied — including one whose content is
unchanged and whose mtime alone moved — and the content check is only reached
for the entries that rule already skipped. Content comparison can then add
copies but never prevent timestamp-driven ones, which is the opposite of why a
content strategy is chosen. Pass the content strategy alone; it decides every
both-sides pair. See [`../library/sync-content.md`](../library/sync-content.md).
