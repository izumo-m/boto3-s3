# Contributing

Thanks for your interest in boto3-s3. This document covers local setup, the test
suite, and the coding and commit conventions. For the project's purpose, scope,
and design policy, start with [`docs/overview.md`](docs/overview.md).

The project is in early development (pre-1.0). Breaking changes are expected and
backward compatibility is not yet a goal.

## Project layout

The repository is a [uv](https://docs.astral.sh/uv/) workspace with two packages:

- `src/boto3_s3/` — **`boto3-s3`**, the library.
- `cli/` — **`boto3-s3-cli`**, the `boto3-s3` command (an `aws s3` drop-in).
- `tests/` — the test suite (see [`docs/testing.md`](docs/testing.md)).
- `docs/` — design and reference documentation.
- `scripts/` — helpers for the e2e environment (the local MinIO stack and the
  pinned aws-cli install).

## Setup

Prerequisites:

- Python 3.10+ (the floor is 3.10; see `.python-version`).
- [uv](https://docs.astral.sh/uv/getting-started/installation/).
- For the end-to-end suite only: Docker (with Compose), plus the `aws` CLI v2
  matching the pinned aws-cli source revision — installed into `.venv/bin` by
  `scripts/install-awscli.sh` (below), not an arbitrary `PATH` aws, which can
  drift the goldens.

Install the workspace and its dev tools into a local virtualenv:

```bash
uv sync --all-packages
```

(A bare `uv sync` installs only the library; without the `cli` workspace member
the `tests/cli` suite fails at collection.)

Run any tool through `uv run` so it uses that environment, e.g. `uv run pytest`.

## Quality gates

Run these before every commit; all must pass:

```bash
uv run ruff format       # format
uv run ruff check        # lint (add --fix to autofix)
uv run basedpyright      # type check — must be clean
uv run pytest            # tests
```

Ruff is configured for a line length of 100 and a Python 3.10 target.

## Tests

`uv run pytest` runs the whole suite **except** the end-to-end parity tests,
which self-skip unless pointed at a live endpoint — so no Docker is needed for
the default run.

The e2e suite differentially compares `boto3-s3` against the real `aws` command
against a local MinIO endpoint:

```bash
scripts/install-awscli.sh    # pin aws to the reference source version — idempotent
scripts/compose-up.sh        # start MinIO, wait for buckets to be provisioned
source scripts/minio-env.sh  # point AWS tooling and the e2e gate at MinIO
uv run pytest tests/cli/e2e  # run the parity suite
scripts/compose.sh down      # tear the stack down
```

Goldens (the recorded real-aws behavior the in-process suite replays) are
regenerated against a clean MinIO with `UPDATE_GOLDENS=1 uv run pytest
tests/cli/e2e`; review the diff before committing. The full picture — test
tiers, the exit-code charter, and the golden contract — is in
[`docs/testing.md`](docs/testing.md).

## Conventions

- **Parity is the contract.** Behavior, options, and exit codes follow `aws s3`.
  When you add or change a subcommand or option, extend the matching scenarios
  and tests so parity stays enforced (see [`docs/testing.md`](docs/testing.md)
  and the exit-code charter in [`docs/overview.md`](docs/overview.md)).
- **Typed.** The library ships type information (`py.typed`); keep public APIs
  typed and basedpyright clean.
- **English.** All version-controlled files are written in English.
- **No dead weight.** Remove implementation and docs that are no longer used
  rather than leaving them behind; git history is the record.

## Commits

- Use [Conventional Commits](https://www.conventionalcommits.org/): `type(scope):
  subject` (scope optional).
- Make sure the quality gates above pass first.
- Commit `uv.lock` whenever dependencies change.

## Submitting changes

The best first step for a bug or a proposed change is to **open an
[issue](https://github.com/izumo-m/boto3-s3/issues)** so it can be discussed
before any code is written. Keep each change focused and follow the conventions
above.

## License

By contributing, you agree that your contributions are licensed under the
project's
[Apache-2.0](https://github.com/izumo-m/boto3-s3/blob/main/LICENSE) license.
