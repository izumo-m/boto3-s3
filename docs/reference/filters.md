# Filters

The `filter=` parameter of `cp` / `mv` / `rm` / `sync` takes a `FileFilter`, and
`GlobFilter` — compiled from `Exclude` / `Include` rules — is the `aws s3`-style
include/exclude implementation of one. The narrative treatment is
[`../library/filters.md`](../library/filters.md); the engine's derivation from
the AWS CLI is [`../../design/globsieve.md`](../../design/globsieve.md).

`FileFilter` is importable from the package root and from `boto3_s3.types`.
`Exclude`, `Include` and `GlobFilter` are importable from the package root and
from `boto3_s3.globsieve`; `compile` and `normcase` are public under
`boto3_s3.globsieve` only.

## FileFilter

A per-entry predicate: it receives the entry's `FileInfo` and returns whether
the entry takes part in the operation. `True` keeps it, `False` skips it.

```python
FileFilter = Callable[[FileInfo], bool]
```

A skipped entry does not take part and no `OpResult` is emitted for it — it is
absent from the run rather than reported as a skip. See
[`results.md`](./results.md) for `OpResult` and `FileInfo`.

**Where it is accepted.** `cp`, `mv`, `rm` and `sync` each take a single
`filter` parameter ([`operations/README.md`](./operations/README.md)). `ls` has
no `filter` parameter. `sync` additionally accepts a `FileFilter` for its
`create_filter` and `delete_filter` lanes, which run after pairing and receive
the `FileInfo` of the one side that lane owns
([`operations/sync.md`](./operations/sync.md)). The predicate is also the
`filter` field of `ScanOptions` when driving `Storage.scan` directly
([`options.md`](./options.md), [`storage.md`](./storage.md)).

**What the `FileInfo` carries.** `compare_key` is stamped before the predicate
is consulted, on every path that applies a filter: it is the entry's path below
the base the AWS CLI joins `--exclude` / `--include` patterns onto, so a
[`GlobFilter`](#globfilter) decides as `aws s3` does. On the enumerating paths
that is the listing's relative key: for a local entry the key relative to the
directory being enumerated, for an S3 entry the key with the listing's `Prefix`
removed. On the single-entry paths the predicate is shown a copy of the entry in
the AWS CLI's form where the record's own differs, and the record handed on is
unchanged (it names the destination):

- a single S3 object (`cp` / `mv`, and `rm`'s blind single key) carries the key
  relative to its parent as [`rm_filter_root`](./operations/rm.md#rm_filter_root)
  returns it — `c.txt` for `s3://bucket/a/b/c.txt`, and `/b` for a key `a//b`
  that doubles a slash;
- a single local source (`cp` / `mv`) carries the AWS CLI's full path as `key`
  (`/`-separated, with a trailing `/` for a directory or a path typed with
  one) and its path below the typed path's parent as `compare_key`: `d` gives
  `d/`; `d/`, `.` and `../` give `""`; a bare `..` gives `None`, so only
  patterns matched against the full path apply. A source in that
  trailing-`/` form (a directory) is also handed on in it, as the AWS CLI
  names it: its record's `key` ends in `/` and its `compare_key` is `""`, so
  a destination that takes the source's name gets none (`d` into
  `s3://bucket/p/` names `s3://bucket/p/`), and the transfer fails on the
  directory.

Two recursive cases differ too, and are shown a copy with a leading `/` in
`compare_key`: a local walk of a filesystem root (`/`, `D:\`), which the AWS
CLI walks from the root plus a separator — its entries read `//bin/x`, as
their `key` does — and matches as `/bin/x`, so a relative `bin/*` misses them
where `*bin/*` does not; and on Windows an S3 location typed ending in a
backslash, which the AWS CLI's `ntpath.join` reads as a separator, so an entry
`pre\/x` under `s3://bucket/pre\` is shown as `/x`. Each operation page
states which key its filter matches
([`operations/README.md`](./operations/README.md)). `storage` is stamped
before the filter runs too, so a predicate can reach the backend the entry
came from (a `HeadObject` for a tag the listing omits, for instance).
`size` and `mtime` are populated for file entries and may be `None` for
directory / prefix entries; on `rm`'s blind single-key path there is no listing
at all, so only `key`, `compare_key` and `storage` are set and `size` / `mtime`
/ `storage_class` are `None`.

**Where it runs.** On the enumerating paths — a recursive `cp` / `mv`, the
local destination pre-walk a recursive S3-to-local `cp` / `mv` performs when
`case_conflict` is anything but `ignore`, `rm`'s recursive delete and its
bucket-root folder-marker sweep, and each side of `sync` — the predicate is
evaluated page by page on the enumeration's prefetch worker thread, overlapped
with the listing I/O — except where the scan runs without read-ahead
([`ScanOptions.read_ahead`](./options.md#scanoptions) `False`: a local `sync`
destination while the delete lane is on, or a backend that seeds it), where it
runs on the thread that called the operation. It must therefore be
thread-safe, and a slow predicate throttles enumeration. `sync` enumerates its
two sides through separate workers, so the two side-walks can invoke it
concurrently. The `case_conflict`
pre-walk hands the predicate destination-side entries during a `cp` or `mv`,
and an entry it excludes is not counted as an existing destination key by that
gate ([`options.md`](./options.md)). On the single-object paths — a
non-recursive `cp` / `mv`, `rm`'s blind single-key delete — it is applied
inline on the calling thread. An exception raised by the predicate is re-raised
on the consumer side of the enumeration (after entries already queued) and
propagates out of the operation call. One detail on the enumerating paths: the
predicate runs inside the listing, and the operation puts its name on a
listing failure that carries none — so a `Boto3S3Error` the predicate lets out
with `operation` unset (from a storage call of its own, say) arrives with
`operation` set to the operation's name, the same exception object otherwise.
A non-recursive `cp` / `mv` sets it the same way; from `rm`'s blind single-key
delete, and from `on_result` / `on_entry`, it arrives as raised.

**Where it does not apply.** `cp`'s stream route — an `IOStorage` on either
side — has no listing to prune, and `filter` is not consulted on it. `mv` has
no stream route of its own: a stream source raises `ValidationError`, so does a
recursive stream destination, and a single-object stream destination rides the
open route, where `filter` is applied to the single source entry and can call
the whole move off ([`operations/mv.md`](./operations/mv.md)).

**`sync` and the two sides.** `sync` applies the single `filter` to each side's
listing independently, before the sides are paired. A destination entry the
filter hides is therefore invisible to the run and cannot be deleted by
`delete_filter`. The two sides receive the same predicate, not necessarily the
same answer: when the decision rests on `compare_key` alone — a relative glob
pattern, the common case — the two sides share one key space, so an excluded
key is absent from both sides and can be neither transferred nor deleted.
A local side rooted at a filesystem root (`/`, `D:\`, `\\srv\share\`) is the
exception, as on aws: its entries are matched with a leading `/`, so a name-led
relative pattern (`bin/*`) prunes the other side only, while a `*`-led one
(`*bin/*`) reaches both. The same holds on Windows for an S3 base typed
ending in `\`. A
root-anchored pattern, or any predicate reading side-specific data (`key`,
`size`, `mtime`, `storage_class`, `storage`, the `LocalFileInfo` fields), can
hide a key on one side only and leave the run a one-sided view of it.

## Exclude

An `--exclude` rule: an entry whose last matching pattern belongs to it is left
out.

```python
class Exclude:
    def __init__(self, *patterns: str) -> None: ...
    patterns: tuple[str, ...]
```

`patterns` is the tuple given, in order. Each pattern is a rule of its own, at
its own position in the list, and a rule with no pattern changes nothing. The
value is frozen: two rules are equal when they are of one kind with the same
patterns, and they hash and pickle.

## Include

An `--include` rule: an entry whose last matching pattern belongs to it is
kept. Otherwise as [`Exclude`](#exclude).

```python
class Include:
    def __init__(self, *patterns: str) -> None: ...
    patterns: tuple[str, ...]
```

## GlobFilter

An ordered list of [`Exclude`](#exclude) / [`Include`](#include) rules compiled
into a `FileFilter` that decides like the AWS CLI's `--exclude` / `--include`.

```python
def compile(rules: Iterable[Exclude | Include]) -> GlobFilter: ...  # boto3_s3.globsieve

class GlobFilter:
    def __call__(self, info: FileInfo) -> bool: ...
    def included(self, relative: str | None, full: str | None = None) -> bool: ...
    @property
    def rules(self) -> tuple[Exclude | Include, ...]: ...

def normcase(text: str) -> str: ...  # boto3_s3.globsieve
```

```python
from boto3_s3 import Exclude, Include, globsieve

web = globsieve.compile([Exclude("*"), Include("*.html", "*.css")])
s3.sync("./site", "s3://bkt/site", filter=web)
s3.cp("./b", "s3://bkt/b/", recursive=True, filter=lambda f: web(f) and (f.size or 0) < 10**8)
```

`compile` splits and translates the patterns once, for the host's path rules
(POSIX or Windows); a value that is not an `Exclude` or `Include` raises
`ValidationError`, and an empty list keeps everything. The compiled filter is
immutable: one instance serves any number of operations, both sides of a
`sync` and every thread, and it pickles by its rules. It is accepted wherever a
`FileFilter` is — `sync`'s `create_filter` and `delete_filter` lanes and
`ScanOptions.filter` included — and can be called from a predicate of your
own. `update_filter` takes a `SyncPair`, so wrap it there:
`lambda pair: web(pair.src)`. A `pair_filter` receives a `DestOnlyPair` too,
which has only `dest`, so a wrap there picks the side present —
`lambda pair: web(pair.dest if isinstance(pair, DestOnlyPair) else pair.src)` —
though `filter=` already prunes both sides before they are paired. `rules` is
the list as given.

**Decision order.** The patterns apply in the order given and the last one that
matches decides; an entry no pattern matches is included. So `Exclude("*")`
followed by includes is default-deny.

**Pattern syntax.** `fnmatch` form: `*` matches any run of characters, `?`
exactly one, and `[seq]` / `[!seq]` one character in or not in a set. `*` and
`?` match `/` as well, there is no escape character, and the whole path must
match — so `logs/*` covers everything under `logs/` at any depth.

**What a pattern is matched against.** The AWS CLI joins each pattern onto its
side's base directory with `os.path.join` and matches the entry's full path
against the result. The filter reproduces that from three values of the entry:
R, its path below that base, which is `info.compare_key`; F, the full path the
AWS CLI matches — the `key` of a `LocalFileInfo` (the absolute path) or
`bucket/key` for an `S3FileInfo` (the bucket read from its `storage`), and
none for any other entry; and D, the drive or UNC share of F (Windows only,
empty on the S3 side). A pattern the join appends to the base is matched
against R, and one the join puts in place of the base against F:

| Pattern | Matched |
|---|---|
| Relative: `*.txt`, `logs/*` | Against R |
| POSIX `/data/*`; Windows `C:\data\*`, `\\server\share\*` | Against F |
| Windows `\data\*` (a root without a drive) | D followed by the pattern, against F |
| Windows `C:x` (a drive without a root) | Against R as `x` when the drive is D, in any case; otherwise as written, against F |

So an absolute pattern never matches an S3 entry, whose `bucket/key` has no
root, and a pattern rooted at the local side of a `sync` prunes that side
only. On Windows a pattern whose drive part is a glob is matched as written:
`*:*` matches every drive-letter path, every S3 key that holds a `:`, and
every key under an access point or Outposts ARN, whose `bucket/key` starts
with `arn:` — as the AWS CLI does. A bare `C:` on drive C: is the empty
pattern. The base is matched literally,
where the AWS CLI reads glob characters in it. An entry without F (a custom
backend's, or an `S3FileInfo` without `storage`) is decided by the patterns
matched against R alone, and one whose `compare_key` is `None` by those matched
against F alone. `included(relative, full)` is the same decision on plain
strings — `full` in either separator — with `None` for a value that is absent.

**Case and separators.** As the AWS CLI's `fnmatch` does, both strings go
through `normcase` first. On Linux and macOS that is the identity: matching is
byte-exact and `\` is an ordinary character. On Windows it lower-cases with
Windows' own table and turns `/` into `\`, so matching is case-insensitive and
the two separators are one, in an S3 key as in a pattern.
