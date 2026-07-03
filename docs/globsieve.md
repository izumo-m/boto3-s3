# globsieve (the glob filter engine) and the filter contract of `S3.rm`

`boto3_s3/globsieve.py` is a self-contained module (depending only on the
standard library). It provides the same semantics as aws-cli's
`--exclude` / `--include` - **evaluate the patterns in sequence; the last one
that matches wins, and a key that matches none is included**.

**Public surface**: the module's `__all__` is the contract, reached by
submodule path (`from boto3_s3 import globsieve` / `from boto3_s3.globsieve
import ...`); only `GlobFilter` / `GlobPattern` are additionally re-exported at
the package root. Because the module is this self-contained, the whole engine
is public - the entry points (`compile`, `GlobFilter`, `GlobPattern`,
`PatternKind`, the `Matcher` / `SetMatcher` protocols), every matcher class
`compile` picks from (the section 2 tables), and the two building-block
helpers: `compile_set_matcher(patterns)` (the shape-specialized `SetMatcher`
that `IncludeOnly` / `ExcludeOnly` / `Sequential` consume; an empty list yields
a never-matching one) and `is_anchored(pattern)` (the root-anchored/relative
split of section 1). Kept internal: the shape predicates behind
`compile_set_matcher`'s partitioning (`_is_literal` / `_is_pure_suffix` /
`_is_pure_prefix`) - implementation detail of the specialization, not part of
the semantics.

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
- `compile(patterns) -> Matcher` - `Matcher.included(compare_key, full_key=None)
  -> bool`. A **relative** pattern matches `compare_key` (the root-relative key);
  a **root-anchored** (absolute) pattern matches `full_key` (the entry's full
  path) - see section 2. Relative-only lists ignore `full_key`.
- `GlobFilter()` - the ergonomic front end and the type the operations consume
  as `filter=`. A chainable `FileFilter` (`Callable[[FileInfo], bool]`) that
  accumulates the same rules and matches a `FileInfo` (its `compare_key`, or
  `key` for an absolute pattern):

  ```python
  from boto3_s3 import GlobFilter

  keep = GlobFilter().exclude("*").include("*.tar.gz").compile()
  s3.cp("./build", "s3://artifacts/", recursive=True, filter=keep)
  ```

  `exclude` / `include` take one or more patterns and return `self`; finish with
  `compile()`, which builds the underlying matcher eagerly and returns `self`
  (the recommended form). It is not mandatory - an un-compiled filter compiles
  lazily on first use and re-dirties when a rule is appended (no freeze). It is
  pure sugar over `compile([GlobPattern...])` - same last-match-wins semantics,
  same section 2 specialization.

### Root-anchored (absolute) patterns

aws-cli joins every pattern onto the operation root with `os.path.join` and
fnmatches the result against the entry's full path (`filters.py`). `os.path.join`
*drops* the root for an absolute right-hand side, so an absolute pattern is
effectively matched against the full path and a relative one against its
root-relative tail. globsieve mirrors that split: a pattern is **anchored** iff
`os.path.isabs` (POSIX `/foo`; Windows also `\foo` / `C:/foo` / UNC), and the
`Anchored` matcher joins each anchored pattern onto the entry's `full_key`
(`os.path.join(full_key, pattern)`, lending the entry's drive / UNC anchor to a
driveless-absolute pattern exactly like aws-cli) before fnmatching `full_key`.
With no `full_key` - an S3 listing has none - an anchored pattern can never match,
matching aws-cli (its s3 paths carry no anchor). This is what lets the **single**
`sync` filter prune the two sides per-side: a source-rooted absolute pattern
matches the local source's full path but not the S3 destination's anchorless key.
A relative pattern keeps matching `compare_key`, which is symmetric across sides.

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
| contains an absolute pattern | `Anchored` (relative -> `compare_key`, absolute -> `full_key`; section 1) |

Within each set it specializes further by **partitioning the patterns by
shape** (literal -> `LiteralSet` (frozenset), `*X` -> `SuffixSet` (tuple
endswith), `X*` -> `PrefixSet` (tuple startswith), anything else ->
`UnionRegex` (alternation of `fnmatch.translate`)). A set that is uniformly one
shape uses that shape's dedicated matcher directly. A **mixed** set - the usual
case for a real exclude list (many `dir/*` directory excludes alongside a
`*.ext` suffix and a couple of literals, e.g. a `.emacs.d` backup) - folds into
a `CompositeSet`: one matcher per populated shape, OR-ed together. The OR is
baked into a single closure (a few C-level `in` / `startswith` / `endswith`
calls, each consuming the whole tuple at once) rather than a Python loop over
sub-matcher objects, so it beats collapsing the mixed set into one big regex
(the prior behavior). This optimization presumes that the **entire glob pattern
sequence is visible**, which is why `GlobFilter` defers compilation until the
full chain of `exclude` / `include` calls has been accumulated (lazily, on
first use).

## 3. The contract of `S3.rm(filter=...)`

`filter: FileFilter | None` where `FileFilter = Callable[[FileInfo], bool]` (so
`None` = no filter). Every filter is one uniform shape - a predicate over the
entry's `FileInfo` - returning True = include (a deletion target), False = skip
(silently; as with aws, no OpResult is emitted either).

The listing stamps **`info.compare_key`** on each entry - a contract the
concrete backend's `scan_pages` producer fulfils, not the base `Storage.scan`
(the single-key path stamps it inline): the entry's key relative to the root
determined by `rm_filter_root(key,
recursive=...)`. The root is, for recursive = the prefix normalized to a
`/`-terminated form, for a single key = its parent "directory", for the bucket
root = `""` (equivalent to the composition of aws's `filters._get_s3_root` plus
`FileFormat.s3_format`). The relative form is what keeps `Exclude("*")`
recognized as the catch-all and the section 2 optimizations in effect; the
bucket name does not affect the decision under either aws's join or the
relativization, so it is not part of the root.

- **`GlobFilter`** (and any glob predicate) matches a relative pattern against
  `info.compare_key`, so it sees the same root-relative key aws-cli's `--exclude`
  / `--include` match, with the section 2 fast paths intact; an absolute pattern
  it anchors against `info.key` (section 1). For `rm` the source is always s3,
  whose key has no anchor, so an absolute pattern is inert and only the relative
  ones bite. The CLI `compile`s the patterns in their order of appearance and
  wraps the result as a `FileFilter` (`cli/src/boto3_s3_cli/filters.py`).
- **a custom predicate** can instead decide on size / mtime / storage_class
  (e.g. `filter=lambda info: info.size == 0`), or read `info.compare_key` for a
  relative-path rule of its own. On the non-recursive blind single-key path
  there is no listing, so the `FileInfo` has only `key` (and the stamped
  `compare_key`) populated (`size` / `mtime` / `storage_class` are `None`).

### Application mechanism (`ScanOptions.filter`)

`S3.rm` wraps the filter (for the folder-marker sweep) and passes it as
`ScanOptions.filter` to the enumeration; the backend's `scan_pages` producer
stamps each entry's root-relative `compare_key` (`info.key[len(root):]`) before
the predicate runs.
The evaluation is done **per page** by
`Storage.scan` (the concrete base-class method) on the listing's prefetch
worker thread - an excluded
entry is not handed to the consumer, and a page that is wiped out entirely never
even reaches the hand-off queue. A `scan_pages` implementation or override only
ever needs to return raw pages and is unaware of the filter. The filter is
invoked from a worker thread, so it must be thread-safe and lightweight.

The scan-level `ScanOptions.filter` is used by rm, cp, mv, and sync (all
implemented); ls does not apply it yet. **sync prunes each side's listing
independently as its visibility layer** (before the comparator pairs the
streams): the S3 side(s) do this through `ScanOptions.filter` (the same
scan-level mechanism), while the local walk applies the predicate inline. The
both sides are matched against the single `filter` (one symmetric predicate over
each side's compare key). A destination entry pruned here
is invisible to `--delete`, reproducing aws's "files excluded by filters are
excluded from deletion". The pair-level judgments (`compare` /
`delete`) are a separate sync-specific layer applied after the
merge-join; see sync.md.

cp / mv / sync take the same single `filter` parameter as rm; the two-parameter
form exclude= / include= is not adopted because it cannot express the
alternating order.

## 4. Path separators and the key space

Every key the filter sees is **POSIX `/`-separated on every OS** - never the
host `os.sep`. S3 keys are `/`-separated natively, and a local walk translates
`os.sep` to `/` (`LocalFileInfo`), so both backends feed one key space (the
basis of sync's merge-join). The **compare key** a glob filter is matched against
(`GlobFilter`, via `FileInfo.compare_key`) is this `/`-form key with the scan
root stripped (`info.key[len(prefix):]`); see [`glossary.md`](./glossary.md).
Matching therefore happens in `/`-space, so **a pattern must be `/`-form to
match**:

- **CLI**: patterns are written `/`-form. A **relative** pattern matches the
  `/`-form `compare_key` directly. An **absolute** pattern is joined onto the
  entry's `key` with `os.path.join` and the result folded back to `/`
  (`Anchored`); the host `os.path` thus handles a Windows user's `\` and drive /
  UNC anchoring, collapsing aws-cli's per-side rewrite into one `/`-space match.
  On Windows aws-cli additionally `normcase`s both sides, making the match
  **case-insensitive**; `cli/src/boto3_s3_cli/filters.py` reproduces this by
  lower-casing patterns at compile and keys at match (`os.name == "nt"`), and
  stays byte-exact on POSIX.
- **library**: a `GlobFilter` matches a relative pattern against the `/`-form
  `compare_key` (so that pattern must be `/`-form too) and an absolute one
  against `info.key` (the `/`-form **full** key, which differs per side - whereas
  `compare_key` is symmetric, so a relative-path rule should read it). The
  library is byte-exact; host case-folding is the CLI layer's job.
