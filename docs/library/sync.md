# `sync`

`sync` mirrors a source into a destination in any direction — upload, download,
or S3-to-S3 — making the same decisions `aws s3 sync` makes by default.

```python
s3.sync("./site", "s3://my-bucket/site/")       # upload
s3.sync("s3://my-bucket/site/", "./site")       # download
s3.sync("s3://src/data/", "s3://dest/data/")    # S3-to-S3
```

## 1. The three decisions

`sync` pairs up the two sides by key and asks one question per entry, depending
on which side the entry is on. Each question has its own argument:

| The entry is… | Question | Argument | Default |
| --- | --- | --- | --- |
| only in the source | create it? | `create_filter` | `True` — create every one |
| on both sides | overwrite it? | `update_filter` | `None` — the `aws s3` size + mtime rule |
| only at the destination | delete it? | `delete_filter` | `False` — keep every one |

Those three defaults together are exactly `aws s3 sync`. `delete_filter=True` is
`--delete`.

```python
# Full mirror: create new, overwrite changed, delete removed.
s3.sync("./site", "s3://my-bucket/site/", delete_filter=True)

# Update-only mirror: refresh and prune, but never publish new files.
s3.sync("./site", "s3://my-bucket/site/", create_filter=False, delete_filter=True)
```

Each accepts `True`, `False`, or a callable. For `create_filter` and
`delete_filter` the callable receives the entry's `FileInfo` and returns whether
to act on it:

```python
s3.sync(src, dest, delete_filter=lambda info: info.mtime < cutoff)
```

`update_filter` is different because its entry exists on both sides, so its
callable receives a **pair** and returns whether to copy. `None` — the default —
means the `aws s3` rule. `True` re-copies everything, `False` never overwrites
(additive only). Note that `None` and `False` are not the same thing.

`create_filter` is the knob `aws s3` does not have: aws always creates.

## 2. The default overwrite rule

With `update_filter=None`, an entry present on both sides is copied when **the
size differs, or the modification times call for it**. The mtime
rule is not symmetric, and this is the single most surprising thing about
`sync` — in both tools:

- **Upload and S3-to-S3 copy** skip when the destination is at or after the
  source's time.
- **Download** skips when the destination is at or *before* it. In other words,
  **a same-size download runs only when the local copy is newer**. An object of
  unchanged size that was updated only on the S3 side is *not* pulled down by
  default.

That asymmetry is `aws s3 sync`'s, reproduced deliberately. If you need updates
to follow content rather than timestamps, use a content strategy —
[`sync-content.md`](./sync-content.md).

An entry missing either a size or an mtime is treated as differing, and copied.

### Tuning it

The two `aws s3` tuners are constructor arguments of the strategy object, not
arguments of `sync`:

```python
from boto3_s3.awsclicompare import AwsCliComparison

s3.sync(src, dest, update_filter=AwsCliComparison(size_only=True))
s3.sync(src, dest, update_filter=AwsCliComparison(exact_timestamps=True))
```

`size_only` compares sizes alone. `exact_timestamps` tightens the download rule
so any difference in timestamp counts. If you set both, `exact_timestamps`
wins — matching `aws s3`.

They live on the strategy rather than on `sync` because a content strategy
replaces the judgment wholesale, leaving nothing for them to tune. The
combination is therefore not expressible, which is intended.

## 3. `filter` narrows what participates

`filter=` is separate from the three above. It decides which entries are
**visible at all**, and it is applied to both sides before pairing — the
equivalent of `--exclude` / `--include`:

```python
from boto3_s3 import GlobFilter

only_html = GlobFilter().exclude("*").include("*.html").compile()
s3.sync(src, dest, filter=only_html, delete_filter=True)
```

The distinction matters when deleting. An orphan hidden by `filter` is not
visible, so it is **never deleted** — exactly as `aws s3 sync` behaves. `filter`
decides *who takes part*; the three lane filters decide *what to do* with those
who did.

## 4. Refusing to overwrite

`no_overwrite=True` is a write guard applied before `update_filter`: an entry
that already exists at the destination is never overwritten, whatever the
strategy would have said. New entries still get created.

Unlike `cp` and `mv`, `sync` keeps this decision-only — it does not send a
conditional-write header, so it works against older SDKs where `cp` would be
refused. See [`compatibility.md`](../compatibility.md).

## 5. What happens before the scan

- **Downloading** creates the destination directory before scanning, so it
  exists even if the sync transfers nothing. If a *file* already exists at that
  path, the run proceeds and then every item fails.
- **Uploading** from a path that does not exist raises. If the source is a file
  rather than a directory, `sync` warns and completes with warnings rather than
  failing — `sync` is a directory operation.
- **Syncing a path onto itself** does nothing and succeeds silently; there is no
  self-reference guard like `mv`'s.
- **S3 Express directory buckets** are rejected on either side. Their listings
  are not lexicographically ordered, so pairing could match the wrong keys and
  `delete_filter` could remove keys that exist on both sides.

## 6. Results and failure

`sync` streams an `OpResult` per item to `on_result` and raises `BatchError` at
the end if anything failed — see [`results.md`](./results.md) and
[`errors.md`](./errors.md). Warnings from walking either side, including the
destination side of a download, are reported as warnings on the run.

Deletions of orphans are reported with `transfer_type` `delete`, and their
ordering relative to transfers is not deterministic.

## 7. Where it differs from `aws s3 sync`

- **Deletes are batched.** Orphans on an S3 destination are removed with S3's
  batch delete API rather than one call per key, so the deletions surface
  together on flush. The final state, the set of reported lines, and the outcome
  are the same.
- **Archived objects are skipped even when restored.** `sync` decides from the
  listing, which does not carry restore status, so a restored Glacier object is
  still skipped with a warning. `force_glacier_transfer=True` is the only way
  through. `aws s3 sync` behaves the same way for the same reason.
