# boto3-s3 Glossary (current state / source of truth)

This document is the **authoritative reference (source of truth)** for the
terminology used in the `boto3-s3` project: each term below has one meaning,
used consistently across code, docs, and discussion.

Related: for the project overview, see [`overview.md`](./overview.md).

---

## Distinguishing the names

The single phrase `boto3-s3` can, depending on context, refer to the library
itself, the distribution package name, or the command name. To avoid confusion,
this project distinguishes them as follows.

**Notation conventions**

- **Underscore** `boto3_s3` / `boto3_s3_cli` always refers to the **import
  name** (Python module).
- **Hyphen** `boto3-s3` refers by default to the **library** itself (or the
  boto3-s3 project as a whole). When you need to identify the distribution
  package or the command, qualify it as "`boto3-s3` (dist)" or "the `boto3-s3`
  command". Likewise, `boto3-s3-cli` refers by default to the **CLI** itself.
- In prose, "**library**" = boto3-s3, "**CLI**" = boto3-s3-cli.

### Software entities

- **boto3-s3 library** (= simply "the **library**") - the Python library
  itself that provides `aws s3`-equivalent operations through its own Python API
  (the `S3` entry point; an independent implementation, not a boto3 client
  wrapper).
- **boto3-s3-cli** (= simply "the **CLI**") - the CLI itself that sits on top
  of the boto3-s3 library. It provides `aws s3`-compatible commands.

### Distribution package names (PyPI distribution)

- **`boto3-s3` (dist)** - the distribution package name of the library.
- **`boto3-s3-cli` (dist)** - the distribution package name of the CLI. It
  depends on `boto3-s3` (dist).

### Import names (Python module / import package)

- **`boto3_s3`** - the import name of the library. Used as `import boto3_s3`.
- **`boto3_s3_cli`** - the import name of the CLI.

### Commands (the name run in the shell)

- **the `boto3-s3` command** - the shell command (console script) exposed by
  the CLI (boto3-s3-cli). The command name is **`boto3-s3`** (`aws s3`-
  compatible). Example: `boto3-s3 cp ...` (equivalent to `aws s3 cp ...`).

### Comparison target (the aws-cli side)

The aws-cli side mirrors the boto3-s3 split. Just as boto3-s3 distinguishes the
implementation (the CLI, boto3-s3-cli) from the command (`boto3-s3`), aws-cli
distinguishes the tool (aws-cli), the command (`aws`), and the S3 subcommand
group (`aws s3`).

- **aws-cli** - Amazon's official AWS command-line tool (the project /
  distribution). Unless otherwise noted it refers to **AWS CLI v2**. It is the
  upstream that boto3-s3 takes as its baseline for functional compatibility
  (parity). It is the counterpart of boto3-s3-cli.
- **the `aws` command** - the shell command exposed by aws-cli. The counterpart
  of the `boto3-s3` command (`aws` <-> `boto3-s3`).
- **`aws s3`** - the S3 subcommand group of the `aws` command (`cp` / `ls` /
  `mb` / `mv` / `presign` / `rb` / `rm` / `sync` / `website`). It is what
  boto3-s3 reproduces, with `aws s3 <sub>` corresponding to `boto3-s3 <sub>`.

## General terms

- **parity** - under identical settings, each boto3-s3 operation produces the
  same functional result (final state of S3 objects, return values, error
  conditions) as the corresponding `aws s3` subcommand. Parity does not extend
  to internal processing order, the exact console output text, or features that
  cannot be embedded in a library. (Exit codes are held to a stricter charter
  at the CLI layer; see [`overview.md`](./overview.md).)
- **key** - the full, `/`-separated identifier of a listing entry (an S3 object
  key / prefix / bucket name, or a local path with the host `os.sep` translated
  to `/`). It is `/`-form on **every OS**, never the host separator, so the two
  sides of a sync share one merge/sort key space. (`FileInfo.key`.)
- **compare key** - the relative form of the **key**, `/`-separated: for a local
  scan, the key relative to the directory being enumerated; for an S3 object
  listing, the entry key with the `ListObjectsV2` `Prefix` removed. A bucket
  listing uses the bucket name unchanged. It is what sync's merge-join pairs on
  and what a relative `GlobFilter` pattern is matched against (a root-anchored
  pattern matches the full **key** instead; the CLI's `--exclude` / `--include`
  follow aws-cli's base-joined matching, which delegates to `compare_key`
  matching only when provably equivalent - see [`cli.md`](./cli.md) section 3).
  It is carried on `FileInfo.compare_key`: each backend's
  listing stamps it on every entry it yields (a `scan` contract of the backend,
  not something the base `Storage.scan` does - see [`storage.md`](./storage.md)
  section 2), so a custom filter can read it directly. The name mirrors
  aws-cli's `FileInfo.compare_key`. Pattern form and OS handling are in
  [`globsieve.md`](./globsieve.md) section 4.
