# Filters

The `filter=` parameter of `cp` / `mv` / `rm` / `sync` takes a `FileFilter`, and
`GlobFilter` / `GlobPattern` are the `aws s3`-style include/exclude
implementation of one. The narrative treatment is
[`../library/filters.md`](../library/filters.md); the engine's derivation from
the AWS CLI is [`../../design/globsieve.md`](../../design/globsieve.md).

`FileFilter` is importable from the package root and from `boto3_s3.types`.
`GlobFilter`, `GlobPattern` and `PatternKind` are importable from the package
root and from `boto3_s3.globsieve`; the rest of the engine — `compile`, the
`Matcher` and `SetMatcher` protocols, the matcher classes,
`compile_set_matcher` and `is_anchored` — is public under `boto3_s3.globsieve`
only.

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
is consulted, on every path that applies a filter, but what it is relative to
depends on the path. On the enumerating paths it is the listing's relative key:
for a local entry the key relative to the directory being enumerated, for an S3
entry the key with the listing's `Prefix` removed. The single-entry paths stamp
their own form — on the single-object `cp` / `mv` routes the last
`/`-separated component of the key, so a pattern for
`cp s3://bucket/a/b/c.txt .` is matched against `c.txt` and not `a/b/c.txt`;
on `rm`'s blind single-key path the key relative to what
[`rm_filter_root`](./operations/rm.md#rm_filter_root) returns. Each operation
page states which key its filter matches
([`operations/README.md`](./operations/README.md)). `storage` is stamped before
the filter runs too, so a predicate can reach the backend the entry came from
(a `HeadObject` for a tag the listing omits, for instance). `size` and `mtime`
are populated for file
entries and may be `None` for directory / prefix entries; on `rm`'s blind
single-key path there is no listing at all, so only `key`, `compare_key` and
`storage` are set and `size` / `mtime` / `storage_class` are `None`.

**Where it runs.** On the enumerating paths — a recursive `cp` / `mv`, the
local destination pre-walk a recursive S3-to-local `cp` / `mv` performs when
`case_conflict` is anything but `ignore`, `rm`'s recursive delete and its
bucket-root folder-marker sweep, and each side of `sync` — the predicate is
evaluated page by page on the enumeration's prefetch worker thread, overlapped
with the listing I/O. It must therefore be thread-safe, and a slow predicate
throttles enumeration. `sync` enumerates its two sides through separate
workers, so the two side-walks can invoke it concurrently. The `case_conflict`
pre-walk hands the predicate destination-side entries during a `cp` or `mv`,
and an entry it excludes is not counted as an existing destination key by that
gate ([`options.md`](./options.md)). On the single-object paths — a
non-recursive `cp` / `mv`, `rm`'s blind single-key delete — it is applied
inline on the calling thread. An exception raised by the predicate is re-raised
on the consumer side of the enumeration (after entries already queued) and
propagates out of the operation call.

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
key is absent from both sides and can be neither transferred nor deleted. A
root-anchored pattern, or any predicate reading side-specific data (`key`,
`size`, `mtime`, `storage_class`, `storage`, the `LocalFileInfo` fields), can
hide a key on one side only and leave the run a one-sided view of it.

## PatternKind

Whether a [`GlobPattern`](#globpattern) includes or excludes the keys it
matches.

```python
class PatternKind(Enum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
```

Two members and no others. `INCLUDE` keeps a matching key and `EXCLUDE` drops
one, subject to the last-match-wins order [`GlobPattern`](#globpattern)
describes — a rule of either kind decides only the keys it matches, and a key
matched by no rule is included.

It is a plain `Enum`, not a `str` subclass, so a member does not equal its own
`"include"` / `"exclude"` value; comparison is against the member. Nothing in
the library accepts one of those strings where a member is expected.

## GlobPattern

One include/exclude rule: a kind and a pattern string. `GlobPattern` values are
what `GlobFilter` accumulates and what `boto3_s3.globsieve.compile` consumes.
The class is a frozen dataclass, so instances are hashable and compare by
`(kind, pattern)`.

```python
@dataclass(frozen=True)
class GlobPattern:
    kind: PatternKind
    pattern: str
```

`kind` is a [`PatternKind`](#patternkind) member — whether keys matching
`pattern` are kept or dropped. `pattern` is the glob itself, in the form
described below. Neither field is validated: any string is a legal pattern.

**Decision order.** A list of rules is evaluated in the order given and the last
rule that matches decides; a key matched by no rule is included. So
`exclude("*")` followed by include rules is default-deny, and `include("*")`
followed by exclude rules — or a list of exclude rules alone — is default-allow.
`compile` recognizes a leading `GlobPattern.exclude("*")` / `include("*")` by
value equality when it selects an internal matcher; that selection affects speed
only, not the decision.

**Pattern syntax.** Patterns are `fnmatch` form: `*` matches any run of
characters, `?` matches exactly one character, and `[seq]` / `[!seq]` match one
character in or not in a set. Both `*` and `?` match `/` as well, and the whole
key must match — both ends are anchored — so `logs/*` covers everything under
`logs/` at any depth, while `x.txt` matches only the key `x.txt` exactly.
Matching is case-sensitive and byte-exact on every platform; the
case-insensitive rule `aws s3` applies on Windows belongs to the `boto3-s3`
command, not to this library.

**Which key the pattern matches.** A relative pattern is matched against the
entry's `compare_key`, whatever the path that stamped it: on an enumeration,
relative to the directory being enumerated for a local entry and
`Prefix`-relative for an S3 entry — the two sides of a `sync` share that one
key space — and on the single-entry paths the form given under
[`FileFilter`](#filefilter). A root-anchored pattern is matched against the
entry's full `key` instead: the absolute filesystem path (with `/` separators)
for a local entry, the object key for an S3 entry. That is what lets one filter
prune the two sides of a `sync` asymmetrically — a pattern rooted at the local
source matches there and not on the S3 side.

**When a pattern is root-anchored.** The test is `os.path.isabs`, plus, on
Windows, a single leading `/` or `\`. On Linux and macOS that means a leading
`/` and nothing else; on Windows it also covers `C:/data/...` and UNC forms. A
root-anchored pattern is joined onto the entry's full key before matching, which
on Windows lends the entry's drive or UNC anchor to a driveless-absolute pattern
(`/data/*` matches an entry keyed `C:/data/x`). Since an S3 object key normally
carries no anchor, a root-anchored pattern matches no S3 entry — with two
carve-outs: a key that literally begins with `/` genuinely is anchored and does
match, and on Windows a key shaped like a drive path (`C:/...`) picks up a drive
the same way a local path would, because the matcher sees only key strings.

**Windows-only pattern rewrites.** The host separator is folded to `/` before
matching, so `logs\*.txt` matches `logs/x.txt` on Windows; on Linux and macOS
`\` stays a literal character. A drive-relative pattern (`C:foo` — a drive
letter with no separator) is not root-anchored: its drive is stripped and the
remainder is treated as a relative pattern. One residual follows from having no
operation root at compile time: a drive-relative pattern naming a different
drive than the source is folded to that same relative tail rather than kept
drive-specific ([`../../design/globsieve.md`](../../design/globsieve.md)).

### include(pattern)

Classmethod. Returns `GlobPattern(PatternKind.INCLUDE, pattern)`.

### exclude(pattern)

Classmethod. Returns `GlobPattern(PatternKind.EXCLUDE, pattern)`.

## GlobFilter

A chainable builder of `GlobPattern` rules that is itself a `FileFilter`:
construct it, append rules, and pass it as `filter=`. It is sugar over
`boto3_s3.globsieve.compile` — the same rules, the same decision order, and the
same key-per-pattern rule described under [`GlobPattern`](#globpattern).

```python
class GlobFilter:
    def __init__(self) -> None: ...
    def exclude(self, *patterns: str) -> GlobFilter: ...
    def include(self, *patterns: str) -> GlobFilter: ...
    def compile(self) -> GlobFilter: ...
    def __call__(self, info: FileInfo) -> bool: ...
```

```python
from boto3_s3 import GlobFilter

keep = GlobFilter().exclude("*").include("*.tar.gz").compile()
s3.cp("./build", "s3://artifacts/", recursive=True, filter=keep)
```

A filter with no rules includes every entry. Because the rules are a list, the
order of the `exclude` / `include` calls is the decision order, and a later call
can override an earlier one for the same key.

An instance is reusable across operations and across the two sides of a `sync`.
Compilation is idempotent and the accumulated rules are read-only during
matching, so concurrent first use from both sides is harmless; appending rules
while an operation is running is not.

A `GlobFilter` is also accepted wherever a `FileFilter` is — `sync`'s
`create_filter` and `delete_filter` lanes included, where it receives the
`FileInfo` of the one side that lane owns
([`operations/sync.md`](./operations/sync.md)).

### exclude(\*patterns)

Appends one exclude rule per argument, in the order given, and returns `self`.
Matching keys are dropped unless a later rule includes them. Calling it discards
any previously compiled matcher, so rules appended after `compile` still take
effect.

### include(\*patterns)

Appends one include rule per argument, in the order given, and returns `self`.
Matching keys are kept unless a later rule excludes them. Like `exclude`, it
discards any previously compiled matcher.

### compile()

Compiles the accumulated rules eagerly and returns `self`. It is optional: an
uncompiled filter compiles itself on first use, and recompiles after a later
`exclude` / `include`. Calling it is the recommended form for a filter that will
be reused, so the cost is paid once. It does not freeze the filter — further
rules may still be appended.

### \_\_call\_\_(info)

Applies the rules to `info` and returns whether the entry is included. A
relative pattern is matched against `info.compare_key`; a root-anchored pattern
against `info.key`. Compiles the rules first if they are not compiled yet.

Raises the built-in `ValueError` when `info.compare_key` is `None`. Stamping it
is a contract of the concrete backend's `scan_pages`, not of the base
`Storage.scan`, which backfills a missing `storage` as a safety net but never
`compare_key` ([`storage.md`](./storage.md)). So it is raised for a hand-built
`FileInfo` — the filter called outside an operation or a scan — and for an
entry from a custom `Storage` whose `scan_pages` omits the stamp.
