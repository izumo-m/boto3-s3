# globsieve (the glob filter engine) and the filter contract of `S3.rm`

`boto3_s3/globsieve.py` is a self-contained module (depending only on the
standard library). It provides the same semantics as aws-cli's
`--exclude` / `--include` - **evaluate the patterns in sequence; the last one
that matches wins, and a key that matches none is included**.

## 1. API

```python
from boto3_s3 import globsieve

m = globsieve.compile([
    globsieve.GlobPattern.exclude("*"),
    globsieve.GlobPattern.include("*.txt"),
])
m.included("foo.txt")  # True
m.included("foo.log")  # False
```

- `GlobPattern.include(p)` / `GlobPattern.exclude(p)` - one rule. fnmatch form
  (`*` is greedy across `/`).
- `compile(patterns) -> Matcher` - `Matcher.included(key) -> bool`.
- `translate_pattern_for_root(pattern, rootdir) -> str | None` - aws-cli joins
  each pattern to the source root and matches it as an absolute path
  (`filters.py:_full_path_patterns`). This function converts it into a
  **relative form** that selects the same key set (a pattern anchored outside
  the root cannot match anything, so the result is `None` = the caller discards
  it).

## 2. Compile-time optimization

`compile` detects the **macro shape** of the pattern sequence and picks the
fastest Matcher:

| Shape | Matcher |
|---|---|
| empty | `AlwaysInclude` |
| only `[exclude "*"]` | `AlwaysExclude` |
| `exclude "*"` + include group | `IncludeOnly` (default-deny) |
| `include "*"` + exclude group / exclude only | `ExcludeOnly` (default-allow) |
| mixed order | `Sequential` (last-wins walk) |

Within each set it specializes further by pattern shape (literal ->
`LiteralSet` (frozenset), `*X` -> `SuffixSet` (tuple endswith), `X*` ->
`PrefixSet`, otherwise -> `UnionRegex` (alternation of `fnmatch.translate`)).
This optimization presumes that the **entire glob pattern sequence is visible**
- this is why `S3.rm`'s filter is an either/or choice between "a compiled
Matcher or a callable" and never mixes the two within a single rule sequence.

## 3. The contract of `S3.rm(filter=...)`

`filter: FileFilter | None` where `FileFilter = Matcher | Callable[[FileInfo],
bool]` (so `None` = no filter). The discrimination is
`isinstance(filter, Matcher)`: `Matcher` is a `runtime_checkable` Protocol (the
`included` method), so a compiled matcher is told apart from a plain `Callable`
at runtime.

- **Matcher**: receives the **key relative** to the root determined by
  `rm_filter_root(key, recursive=...)`. The root is, for recursive = the prefix
  normalized to a `/`-terminated form, for a single key = its parent
  "directory", for the bucket root = `""` (equivalent to the composition of
  aws's `filters._get_s3_root` plus `FileFormat.s3_format`). Passing the
  relative form keeps the shape in which `Exclude("*")` is recognized as the
  catch-all and the optimizations in section 2 take effect. The CLI relativizes
  `--exclude` / `--include` in their order of appearance via
  `translate_pattern_for_root`, `compile`s them, and passes the result here
  (`cli/filters.py`). The bucket name does not affect the decision under either
  aws's join or the relativization, so it is not included in the root.
- **callable**: a custom extension that receives `FileInfo` directly (it can
  decide on size / mtime / storage_class; e.g.,
  `filter=lambda info: info.size == 0`). True = keep as a deletion target. On
  the non-recursive blind single-key path there is no listing, so the
  `FileInfo` passed to the callable has only `key` populated (`size` / `mtime` /
  `storage_class` are `None`).
- A return value of True = include (a deletion target); False = skip (silently;
  as with aws, no OpResult is emitted either).

### Application mechanism (`ScanOptions.filter`)

`S3.rm` normalizes the filter into a `FileInfo` predicate (a Matcher is wrapped
in a closure that slices out the root-relative key) and passes it as
`ScanOptions.filter` to the enumeration. The evaluation is done **per page** by
`Storage.scan` (the concrete base-class method) on the listing's prefetch
worker thread - an excluded
entry is not handed to the consumer, and a page that is wiped out entirely never
even reaches the hand-off queue. A `scan_pages` implementation or override only
ever needs to return raw pages and is unaware of the filter. A callable filter
is invoked from a worker thread, so it must be thread-safe and lightweight.

The scan-level `ScanOptions.filter` is used by rm, cp, mv, and sync (all
implemented); ls does not apply it yet. **sync prunes each side's listing
independently as its visibility layer** (before the comparator pairs the
streams): the S3 side(s) do this through `ScanOptions.filter` (the same
scan-level mechanism), while the local walk applies the predicate inline. The
both sides are matched against the single `filter` (one symmetric predicate over
each side's compare key). A destination entry pruned here
is invisible to `--delete`, reproducing aws's "files excluded by filters are
excluded from deletion". The pair-level judgments (`copy_filter` /
`delete`) are a separate sync-specific layer applied after the
merge-join; see sync.md.

cp / mv / sync take the same single `filter` parameter as rm; the two-parameter
form exclude= / include= is not adopted because it cannot express the
alternating order.

## 4. Path separators and the key space

Every key the filter sees is **POSIX `/`-separated on every OS** - never the
host `os.sep`. S3 keys are `/`-separated natively, and a local walk translates
`os.sep` to `/` (`LocalFileInfo`), so both backends feed one key space (the
basis of sync's merge-join). The **compare key** a Matcher is matched against is
this `/`-form key with the scan root stripped (`info.key[len(prefix):]`); see
[`glossary.md`](./glossary.md). Matching therefore happens in `/`-space, so **a
pattern must be `/`-form to match**:

- **CLI**: patterns are written `/`-form. `cli/filters.py` runs each through
  `translate_pattern_for_root`, which folds the host separator to `/`
  (`pattern.replace(os.sep, "/")`), so a Windows user may also write `\` and it
  is normalized. This collapses aws-cli `filters._match_pattern`'s per-side
  rewrite (local `/` -> `os.sep`, s3 `os.sep` -> `/`) into a single `/`-space
  match: on POSIX (`os.sep == "/"`) it is a no-op, so a literal `\` in an S3 key
  survives instead of being rewritten. On Windows aws-cli additionally
  `normcase`s both sides, making the match **case-insensitive**; `cli/filters.py`
  reproduces this by lower-casing patterns at compile and keys at match
  (`os.name == "nt"`), and stays byte-exact on POSIX.
- **library**: a Matcher passed to `filter=` is matched against the `/`-form
  compare key, so its patterns must be `/`-form too - globsieve does no separator
  normalization at match time. To accept `os.sep` input, route the patterns
  through `translate_pattern_for_root` (the public helper the CLI uses) before
  `compile`. A callable filter instead receives the `FileInfo`, whose `key` is
  the `/`-form **full** key (not the compare key), so a callable that inspects
  `key` is comparing full identifiers, which differ per side.
