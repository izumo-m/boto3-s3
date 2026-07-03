# Release

Every PyPI upload is triggered from this repo and nothing else: a matching tag
publishes, an ordinary push builds nothing. The two packages are versioned
independently ([`overview.md`](./overview.md) section 3) and the tag selects
which. Publishing runs in
[`.github/workflows/release.yml`](../.github/workflows/release.yml) via Trusted
Publishing (OIDC), so no token is needed.

| Tag pushed | Package published (version source) |
|---|---|
| `boto3-s3-v<version>` | `boto3-s3` library (`pyproject.toml`) |
| `boto3-s3-cli-v<version>` | `boto3-s3-cli` CLI (`cli/pyproject.toml`) |

What ships is the `version` in that package's `pyproject.toml`, **not** the tag.
The tag only triggers the run and must agree with it, or the workflow stops and
publishes nothing. So edit `pyproject.toml` first, then tag to match.

## Releasing

Work happens on `develop`; releases are cut on `main`, and the tag lives on
`main`'s merge commit. Both packages share this one line — the tag prefix selects
which publishes — so they can go out together (two tags on one commit) or
independently (one tag).

To release `boto3-s3` X.Y.Z (the CLI is identical with `cli/pyproject.toml`,
`cli/CHANGELOG.md`, and a `boto3-s3-cli-vX.Y.Z` tag; a joint release puts both
tags on the same merge commit — but pushes them in order, see step 3):

1. On `develop`, make a single `chore: release X.Y.Z` commit that bumps:
   - `pyproject.toml` `version` to `X.Y.Z`
   - `uv.lock` (run `uv lock` to refresh)
   - `CHANGELOG.md`: rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, then update
     the links at the bottom — add `[X.Y.Z]: .../compare/...vX.Y.Z` and repoint
     `[Unreleased]` to `.../compare/boto3-s3-vX.Y.Z...HEAD` (the CLI changelog
     uses the `boto3-s3-cli-vX.Y.Z` tag).
   - For a CLI release: check that `cli/pyproject.toml`'s `boto3-s3` range
     covers the library version the CLI code actually needs — when the CLI uses
     an API added in the library version being released alongside it, the floor
     **must** be that version (`boto3-s3>=X.Y.Z,<X.Y+1.0`). A stale floor
     resolves an older library from PyPI and crashes at runtime.
2. Merge into `main`, keeping the merge commit as a clear release boundary:
   ```bash
   git switch main && git merge --no-ff develop
   ```
3. Tag that merge commit and push — **this push is what publishes**. For a
   joint release, publish the **library first and wait for its Actions run to
   finish** (the CLI's install resolves `boto3-s3` from PyPI, so the dependency
   must exist there before the CLI lands). The workflow enforces this: a CLI
   release aborts before upload unless the target index already serves a
   `boto3-s3` version satisfying the CLI's dependency range (a TestPyPI
   dry run of the CLI therefore also needs the library dry-published there
   first):
   ```bash
   git tag boto3-s3-vX.Y.Z
   git push origin main boto3-s3-vX.Y.Z
   # joint release: after the boto3-s3 run is green on the Actions tab
   git tag boto3-s3-cli-vA.B.C
   git push origin boto3-s3-cli-vA.B.C
   ```
4. Bring `develop` back level with `main` so the two lines stay in sync:
   ```bash
   git switch develop && git merge --ff-only main && git push origin develop
   ```

Watch the run in the Actions tab; if it fails the version check, nothing was
uploaded.
