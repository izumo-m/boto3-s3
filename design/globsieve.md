# globsieve: aws-cli's `--exclude` / `--include`, and the filter contract

`boto3_s3/globsieve.py` is the library's engine for aws-cli's `--exclude` /
`--include`. It depends only on the standard library and the pure
`boto3_s3.types` / `boto3_s3.exceptions` (design/imports.md). It is not a
general glob engine: its job is to decide every entry exactly as aws-cli's
`filters.py` does, on every OS, from the entry alone. A caller who wants
another kind of filter writes a predicate of their own (`FileFilter`), or
wraps a compiled `GlobFilter` in one.

**Public surface** (the module's `__all__`): `Exclude`, `Include`,
`GlobFilter`, `compile` and `normcase`. `Exclude`, `Include` and `GlobFilter`
are also exported from the package root.

## 1. API

```python
from boto3_s3 import Exclude, Include, globsieve

web = globsieve.compile([Exclude("*"), Include("*.html", "*.css")])
s3.sync("./site", "s3://bkt/site", filter=web)
s3.cp("./b", "s3://bkt/b/", recursive=True, filter=lambda f: web(f) and (f.size or 0) < 10**8)
web.included("x.html", "/home/u/site/x.html")  # the decision on plain strings
```

- `Exclude(*patterns)` / `Include(*patterns)`: frozen rule values with a
  `patterns` tuple. Each pattern is a rule of its own, at its own position in
  the list. Rules rather than `("exclude", p)` tuples, because a string
  should not name an operation; rules rather than a builder, because the
  list is plain data.
- `compile(rules) -> GlobFilter`: splits and translates the patterns once,
  for the host's path rules. A value that is not a rule raises
  `ValidationError`.
- `GlobFilter`: an immutable `FileFilter` (`__call__(info) -> bool`), with
  `included(relative, full=None)` for plain strings and `rules`. It pickles
  by its rules, so it crosses into a process pool.
- `normcase(text)`: aws's matching fold on this host (section 4), for a
  caller that reasons about patterns on its own (s3bak's pruning of whole
  directories).

The decision is aws's `Filter.call`: the patterns apply in order, the last
one that matches decides, and an entry no pattern matches is included. A
pattern is Python's `fnmatch` form: `*` and `?` match `/` too, `[seq]` /
`[!seq]` match one character, there is no escape character, and the whole
path must match.

## 2. What a pattern is matched against

aws joins every pattern onto its side's base (`rootdir`) with `os.path.join`
and fnmatches the entry's full path against the result
(`filters._full_path_patterns`, `_match_pattern`). The base is
`abspath(path)` for a local recursive side, `abspath(dirname(path as typed))`
for a single local source, `bucket/key` as typed for an S3 recursive side,
and `bucket/` + the key's parent for a single S3 object
(`_get_local_root` / `_get_s3_root`). For any entry, the verdict depends on
three values of the entry alone:

- **R**, the entry's path below aws's base: `FileInfo.compare_key`;
- **F**, the full path aws matches: a `LocalFileInfo`'s `key` (the absolute
  path, `/`-separated), `bucket/key` for an `S3FileInfo` (the bucket read from
  its `storage`), none for any other entry;
- **D**, the drive or UNC share of F in its native spelling (Windows only;
  empty on the S3 side, whose path has none).

So the compiled filter needs no base, and one object serves every operation,
both sides of a `sync`, `sync`'s decision filters and the caller's own
predicate. What it relies on is the stamping contract of section 5: every
entry the library hands to a filter carries aws's R in `compare_key` and
aws's F in `key`.

At compile time each pattern is split into drive, root and rest with a port
of Python 3.14's `ntpath.splitroot` (`_splitroot`; aws-cli bundles 3.14, and
3.10 / 3.11 split some forms differently), and each form becomes a rule:

| Form of the pattern | aws's `join(base, p)` | Matched | Why it equals aws |
|---|---|---|---|
| Relative (no drive, no root) | base, separator, p | `normcase(p)` against `normcase(R)` | F is base, separator, R; the fold works per character; the base is literal (section 6) |
| POSIX `/x` | `/x` | against F | the join drops the base; an S3 path starts with a bucket, never `/` |
| Windows drive-absolute `C:\x`, UNC `\\s\sh\x` | p | `normcase(p)` against `normcase(F)` | the join drops the base |
| Windows root-relative `\x` | D followed by p | `normcase(D + p)` against `normcase(F)`, D literal | the join keeps only the base's drive or share |
| Windows drive-relative `C:x`, the drive being D | base, separator, rest | `normcase(rest)` against `normcase(R)` | joins like a relative pattern |
| Windows drive-relative, another drive (or the S3 side, or no F) | p (a UNC drive: drive, separator, rest) | against F | the join ignores the base |

Notes:

- "The drive being D" is `ntpath.join`'s test: equal ignoring case
  (`str.lower`). Where Windows' fold and `str.lower` part (a UNC share name),
  the joined path carries a prefix no entry under D has, and the rule
  matches nothing - as on aws.
- D is compared in aws's native spelling: a UNC drive written `//s/sh` is
  another drive than `\\s\sh` for `ntpath.join`.
- A glob in the drive part is matched as written: `--exclude '*:*'` on
  Windows matches every drive-letter path, every S3 key that holds a `:`,
  and every key under an access point or Outposts ARN, whose `bucket/key`
  starts with `arn:` (`a:b` on the S3 side is a drive `a:`, never the key).
  A bare `C:` on drive C: is the empty pattern. Measured on `aws.exe`
  2.36.40, except the ARN case (the dev stack has no access point), which
  follows from the same join.
- On POSIX a pattern is anchored exactly when it starts with `/`; `\` and
  `C:` are ordinary characters.

## 3. Matching and its cost

The rules of one form of match are compiled into one regular expression: the
alternation of their `fnmatch.translate` forms, latest rule first, each
alternative ending in an empty marker group whose index (`match.lastindex`)
names the rule that decided. Relative rules match R and full-path rules match
F; the later of the two matches wins, and the F regex is skipped when R's
match is later than every full-path rule. When the list holds a Windows
root-relative or drive-relative pattern, the two regexes depend on D, so they
are built per drive on first sight and kept (up to 64 drives; an operation
meets one or two).

Per entry this is one regex call (two when the list has full-path rules), F
is built only when full-path rules exist, and on Windows each string matched
is folded once. The old general engine's shape specializations (literal /
prefix / suffix sets, macro shapes) are gone; measured on a 20-pattern
exclude list the new engine costs about 0.4 µs per entry against the old
0.35, and half the old cost on mixed include/exclude lists - small next to a
local walk's 4.6 to 5.7 µs per entry.

## 4. Case and separators: `normcase`

aws's `fnmatch.fnmatch` puts both the path and the pattern through
`os.path.normcase`. On POSIX that is the identity: matching is byte-exact and
`\` is an ordinary character. On Windows it turns `/` into `\` and lower-cases
with the operating system's own locale-invariant table (`LCMapStringEx`), so
matching is case-insensitive and the two separators are one - in an S3 key
that holds a literal backslash as much as in a pattern (measured against
`aws.exe` 2.36.40 by real deletion: `--exclude a/b` keeps the key `a\b`). The
fold is not `str.lower` (447 code points differ on Windows 11; the Kelvin
sign and U+0130 are kept). `ntpath.normcase` is that table wherever
`_winapi` offers the call (3.11, and 3.10 from a patch release on); on a
Windows host whose Python lacks it (an early 3.10 release) the engine calls
`LCMapStringEx` itself (`_lcmap_lower`), a Python-version difference that
changes which entries a normal run selects and is therefore matched
(overview.md section 3). Matching in the `\` form is what keeps a character
class such as `[+-9]`, which spans `/` but not `\`, deciding as aws
decides.

The host model (POSIX or Windows rules and fold) is bound when a filter is
compiled, from `globsieve._WINDOWS`; the tests simulate the other family by
replacing it.

## 5. The stamping contract: what each route shows a filter

Every entry the library hands to a filter carries aws's R in `compare_key`
and, for a local or S3 entry, aws's F in `key` / `bucket/key`. Where the
record the operation keeps differs from that (its `compare_key` names the
destination and pairs `sync`'s sides), the filter is shown a copy (a *view*)
and the record is handed on unchanged.

| Route | R shown to the filter |
|---|---|
| Recursive local walk (`cp` / `mv` source, the `sync` local side (source or destination), the case-conflict gate's destination pre-walk) | the path below the walked directory, `abspath(path)` - the record's own, except for a filesystem root (`/`, `D:\`, `\\srv\share\`): aws walks from `abspath` plus a separator, so its F reads `//bin/x` (as the record's `key` does) while its base `abspath(path)` already ends in one, and the view adds a leading `/` (`producers.local_walk_filter`) |
| Recursive S3 listing (`cp` / `mv` / `sync` S3 sides, `rm --recursive`) | the key below the `/`-normalized `Prefix` - the record's own, except on Windows for a key typed ending in `\`: aws's `ntpath.join` takes that backslash for a separator and joins the patterns flush, so the view adds a leading `/` (`producers.s3_listing_filter`) |
| Single S3 object (`cp` / `mv` source, `rm`'s blind delete) | the key below `single_object_filter_root` (aws's parent; `a//b` is `/b`, and on Windows `a\/b` is `/b`) - a view on `cp` / `mv` (`_filter_single_s3_source`), the stamped record on `rm` |
| Single local source (`cp` / `mv` upload) | a view (`_filter_single_local_source`): `key` is aws's `local_format` path (`/`-separated, a trailing `/` for a directory or a path typed with one), `compare_key` the part below `abspath(dirname(path as typed))` - `d` is `d/`; `d/`, `.` and `../` are `""`; a bare `..` lies outside and gets `None`, so only full-path patterns apply |
| A custom backend's entry | whatever the backend stamps; it has no F, so only relative patterns apply (and a drive-relative pattern, which needs D, never does) |

The single local view is what keeps `cp d s3://b/k --exclude d` transferring
(and failing on the directory, rc 1) while `--exclude 'd/'` excludes it (rc 0),
as measured on aws 2.36.40.

## 6. Deliberate differences

- **The base is literal.** aws joins the operation's own path into every
  pattern, so glob characters there are interpreted: `cp --recursive 'dir[1]'
  s3://b/ --exclude '*'` still transfers on 2.36.40 (aws-cli #7330, #5452).
  The engine matches relative patterns against R and takes D literally, so the
  base never globs.
- **No cross-side application.** aws applies the source's and the
  destination's joined patterns to every entry. Each entry here meets its own
  side's patterns only. That differs from aws in three places: nested s3->s3
  prefixes (`cp s3://b/d s3://b/d/backup --exclude 'x*'` excludes the source's
  `d/backup/x1.txt` on aws), single-object s3->s3 copies whose parents nest,
  and, on Windows, a root-relative or drive-relative pattern joined onto the
  S3 side - which has no drive, so it stays as written - reaching a local
  entry (`C:?b*` matching `C:\b...`, `\?srv\...` matching a UNC path).

Both are in docs/cli/aws-differences.md.

## 7. Where the filter runs

`cp`, `mv`, `rm` and `sync` take one `filter: FileFilter | None`
(`FileFilter = Callable[[FileInfo], bool]`, `True` = include). A recursive
side applies it as `ScanOptions.filter`: the backend's `scan_pages` stamps
`compare_key` and runs the predicate on the listing's prefetch worker, page by
page (or on the consuming thread where the scan runs without read-ahead;
[`storage.md`](./storage.md)), so an excluded entry is absent downstream and
the predicate must be thread-safe and cheap. The local backend applies it
inside the walk, after aws's vetting, so an excluded file still emits the
warnings aws would. The single-object routes apply it inline on the calling
thread. `ls` takes no filter.

**`sync`** prunes each side's listing with the one filter before pairing (its
visibility layer). A relative pattern prunes both sides alike, except where a
side's R carries the leading `/` of the table above (a local side at a
filesystem root, a Windows S3 base ending in `\`): there a name-led pattern
prunes the other side only, as on aws; an absolute one
matches each side's own F, so a pattern rooted at the local source prunes
that side only and leaves the destination visible to `--delete`. A
destination entry pruned here is invisible to `--delete` (aws: files excluded
by filters are excluded from deletion). The pair-level `create_filter` /
`update_filter` / `delete_filter` are a separate layer after the merge-join
(sync.md); a `GlobFilter` works in `create_filter` / `delete_filter`, and in
`update_filter` wrapped: `lambda pair: web(pair.src)`. A `pair_filter` also
receives a `DestOnlyPair`, which has only `dest`, so a wrap there picks the
side (`web(pair.dest if isinstance(pair, DestOnlyPair) else pair.src)`) -
though `filter=` already prunes both sides before pairing.

**`rm`** wraps the filter for the bucket-root folder-marker sweep (marker test
first, then the filter - aws's order). Its prefix is `rm_filter_root(key,
recursive=...)`: the target normalized to end in `/` for a recursive delete
(also the listing `Prefix`), the key's parent for a single key, `""` for the
bucket root. On the blind single-key path no listing runs, so the `FileInfo`
carries only `key`, the stamped `compare_key` and `storage`.

The stream route (an `IOStorage` on either side) consults no filter, as aws
applies none to a stream.

## 8. Verification

`tests/lib/test_globsieve.py` pins the measured aws 2.36.40 cases (Linux and
`aws.exe`) at the `included()` level, the forms table on both host models, the
Windows fold (the `LCMapStringEx` code points on a Windows host), and runs a
seeded fuzz against a verbatim port of aws's `filters.py` (`create_filter`,
`_get_*_root`, `Filter.call`, `_match_pattern`) on `posixpath` and a 3.14
`ntpath` port, over bases without glob characters that do not nest: every
entry's verdict equals aws's with its own side's patterns, and with both
sides' except the Windows cross-side case of section 6. The `splitroot` /
`join` ports are cross-checked against Python 3.14 when uv finds one. The
operation tests pin each route's view (section 5) and the two deliberate
differences.
