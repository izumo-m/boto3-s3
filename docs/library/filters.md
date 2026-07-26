# Filtering which items take part

`cp` / `mv` / `rm` / `sync` take a `filter=` that decides which items the
operation acts on. It has one shape everywhere: a callable taking the entry's
`FileInfo` and returning whether to keep it.

```python
FileFilter = Callable[[FileInfo], bool]     # True = act on it, False = skip
```

A skipped item is skipped silently — no record reaches `on_result`, matching
`aws s3`. `ls` takes no `filter=` argument.

## 1. Glob patterns

[`GlobFilter`](../reference/filters.md#globfilter) is the `aws s3`-style
include/exclude matcher. Patterns are evaluated in order, **the last one that
matches wins**, and an item that matches nothing is **included**:

```python
from boto3_s3 import GlobFilter

keep = GlobFilter().exclude("*").include("*.tar.gz").compile()
s3.cp("./build", "s3://artifacts/", recursive=True, filter=keep)
```

`exclude()` and `include()` each take one or more patterns and return the filter,
so they chain. Call `compile()` when you are done adding rules; it is optional —
an uncompiled filter compiles itself on first use.

Patterns are `fnmatch` form, in which `*` is greedy and crosses `/`.

## 2. Which key a pattern matches

Every key the filter sees is `/`-separated on **every** operating system. S3
keys are natively; a local walk converts the host separator. That single key
space is what lets `sync` pair the two sides.

A **relative** pattern matches the entry's `compare_key` — its key relative to
the root the operation is enumerating. For a recursive `rm s3://bucket/logs/`,
an object at `logs/app/x.txt` has a compare key of `app/x.txt`, so `app/*`
matches it and `logs/*` does not.

A pattern is **absolute** when it starts with `/` — on Windows, a drive letter
or a leading `\` counts too. An absolute pattern matches the entry's full key
instead. Since an S3 key normally has no leading `/`, one is inert against S3
entries; against local paths it works as written. This is what allows one
`filter` to
prune the two sides of a `sync` asymmetrically — a pattern rooted at the local
source matches there and not on the S3 side. Relative patterns are symmetric
across sides.

On **Windows** a backslash in a pattern is folded to `/` and works as a
separator, so `logs\*.txt` matches `logs/x.txt`. On Linux and macOS a backslash
stays a literal character. Matching in the library is byte-exact on every
platform — the case-insensitive rule that `aws s3` applies on Windows belongs to
the `boto3-s3` command, not here.

## 3. Filtering on anything else

`filter=` accepts any predicate, not just globs:

```python
s3.rm("s3://bucket/tmp/", recursive=True, filter=lambda info: info.size == 0)
s3.sync(src, dest, filter=lambda info: not info.compare_key.startswith("draft/"))
```

[`FileInfo`](../reference/results.md#fileinfo) carries `key`, `compare_key`,
`size`, `mtime`, `storage_class` and `storage` — the last being the backend the
entry came from, always stamped before any filter runs, so a predicate can
reach back through it (a `HeadObject` for something the listing omits, say).
One caveat: on `rm`'s non-recursive single-key path there is no listing, so
only `key`, `compare_key` and `storage` are populated — `size`, `mtime` and
`storage_class` are `None`. Guard for that if your predicate might run there.

**On an enumerating path the predicate runs on a listing prefetch worker
thread**, once per entry as each page of results arrives, so it must be
thread-safe and it should be cheap: a slow predicate throttles enumeration. On
the single-object routes — a non-recursive `cp` / `mv`, `rm` on one key —
there is no prefetch worker and it runs inline on the calling thread.

## 4. `filter` in `sync`

`sync` applies the single `filter` to **each side independently**, before pairing
them. That has one consequence worth stating on its own: an entry at the
destination that `filter` hides is invisible to the run, so **`delete_filter`
never deletes it**. This reproduces `aws s3 sync`'s rule that files excluded by
filters are excluded from deletion.

`filter` is a different question from `sync`'s three decision filters. `filter`
decides *which entries take part at all*; `create_filter`, `update_filter` and
`delete_filter` decide *what to do* with the entries that did. See
[`sync.md`](./sync.md).

## 5. The pattern engine on its own

The matcher behind `GlobFilter` is usable directly, for matching keys outside an
operation:

```python
from boto3_s3 import globsieve

m = globsieve.compile([
    globsieve.GlobPattern.exclude("*"),
    globsieve.GlobPattern.include("*.txt"),
])
m.included("foo.txt")   # True
m.included("foo.log")   # False
```

`Matcher.included(compare_key, full_key=None)` applies the same last-match-wins
rule; `full_key` is only consulted by absolute patterns. `GlobFilter`,
`GlobPattern` and `PatternKind` are also available at the package root;
everything else lives in `boto3_s3.globsieve`.
