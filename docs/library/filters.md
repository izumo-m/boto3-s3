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

`globsieve.compile` turns an ordered list of
[`Exclude`](../reference/filters.md#exclude) and
[`Include`](../reference/filters.md#include) rules into a
[`GlobFilter`](../reference/filters.md#globfilter), the `aws s3`-style
include/exclude filter. Patterns are evaluated in order, **the last one that
matches wins**, and an item that matches nothing is **included**:

```python
from boto3_s3 import Exclude, Include, globsieve

keep = globsieve.compile([Exclude("*"), Include("*.tar.gz")])
s3.cp("./build", "s3://artifacts/", recursive=True, filter=keep)
```

`Exclude` and `Include` each take one or more patterns. The compiled filter is
immutable, so one instance serves several operations, both sides of a `sync`,
and a predicate of your own:
`filter=lambda f: keep(f) and (f.size or 0) < 10**8`.

Patterns are `fnmatch` form, in which `*` is greedy and crosses `/`.

## 2. Which path a pattern matches

The filter decides as `aws s3` does, which joins each pattern onto the
directory or prefix the operation names and matches the entry's full path
against the result.

A **relative** pattern therefore matches the entry's `compare_key` — its key
relative to the root the operation is enumerating, `/`-separated on every
operating system. For a recursive `rm s3://bucket/logs/`, an object at
`logs/app/x.txt` has a compare key of `app/x.txt`, so `app/*` matches it and
`logs/*` does not. A single source is shown to the filter in `aws s3`'s form:
`s3.cp("d", "s3://b/k")` matches the directory as `d/` (so `Exclude("d/")`
skips it and `Exclude("d")` does not), and the key `a//b` as `/b`
([reference](../reference/filters.md#filefilter)).

A pattern the join takes in place of the root — `/data/*` on Linux and macOS;
`C:\data\*`, a UNC path or a rooted `\data\*` on Windows — matches the entry's
full path instead: the absolute path of a local entry, `bucket/key` of an S3
one. Such a pattern never matches an S3 entry, so one rooted at the local
source of a `sync` prunes that side only; relative patterns are symmetric
across sides, except where a local side is rooted at a filesystem root (`/`,
`D:\`), whose entries are matched with a leading `/` as on `aws s3`, so
`bin/*` then prunes the other side only. On Windows a drive-relative `C:foo` is relative for entries on
drive C: and is matched as written against the full path of every other
entry, so on Windows `*:*` matches every drive-letter path, every S3 key that
holds a `:`, and every key under an access point or Outposts ARN (whose
`bucket/key` starts with `arn:`). The
[reference](../reference/filters.md#globfilter) has the full table.

Matching follows `aws s3` on each platform: byte-exact on Linux and macOS,
where a backslash is an ordinary character; on Windows case-insensitive, with
`/` and `\` one separator, in patterns and in S3 keys alike.

## 3. Filtering on anything else

`filter=` accepts any predicate, not just globs:

```python
s3.rm("s3://bucket/tmp/", recursive=True, filter=lambda info: info.size == 0)
s3.sync(src, dest, filter=lambda info: not (info.compare_key or "").startswith("draft/"))
```

[`FileInfo`](../reference/results.md#fileinfo) carries `key`, `compare_key`,
`size`, `mtime` and `storage` — the last being the backend the entry came from,
always stamped before any filter runs, so a predicate can reach back through it
(a `HeadObject` for something the listing omits, say). An S3 entry is an
`S3FileInfo`, which adds `storage_class` and `etag`; a local entry has neither,
so a predicate that reads them must allow for local entries.
One caveat: on `rm`'s non-recursive single-key path there is no listing, so
only `key`, `compare_key` and `storage` are populated — `size`, `mtime` and
`storage_class` are `None`. Guard for that if your predicate might run there.

**On an enumerating path the predicate runs on a listing prefetch worker
thread**, once per entry as each page of results arrives, so it must be
thread-safe and it should be cheap: a slow predicate throttles enumeration. The
exception is a local `sync` destination while `delete_filter` or `pair_filter`
is on, which is walked without reading ahead: there the predicate runs on the
calling thread. On the single-object routes — a non-recursive `cp` / `mv`, `rm`
on one key — there is no prefetch worker and it runs inline on the calling
thread.

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

A compiled filter also decides on plain strings, for matching paths outside an
operation:

```python
from boto3_s3 import Exclude, Include, globsieve

m = globsieve.compile([Exclude("*"), Include("*.txt")])
m.included("foo.txt")   # True
m.included("foo.log")   # False
```

`included(relative, full=None)` takes the path below the root and, for the
patterns matched against a full path, that full path. `globsieve.normcase` is
the fold `aws s3` applies to both strings before matching on this platform.
`Exclude`, `Include` and `GlobFilter` are also available at the package root.
