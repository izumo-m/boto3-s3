# Release

Every package-index upload is triggered from this repo and nothing else: a
matching tag publishes to PyPI, a manual workflow dispatch publishes only to
TestPyPI, and an ordinary push publishes nothing. The two packages are
versioned independently ([`overview.md`](./overview.md) section 3), and the tag
selects which one publishes. Publishing runs in
[`.github/workflows/release.yml`](../.github/workflows/release.yml) via Trusted
Publishing (OIDC), so no token is needed.

| Tag pushed | Package published (version source) |
|---|---|
| `boto3-s3-v<version>` | `boto3-s3` library (`pyproject.toml`) |
| `boto3-s3-cli-v<version>` | `boto3-s3-cli` CLI (`cli/pyproject.toml`) |

What ships is the `version` in that package's `pyproject.toml`, **not** the tag.
The tag only triggers the run and must agree with it, or the workflow stops and
publishes nothing. So edit `pyproject.toml` first, then tag to match.

## One-time setup

Trusted Publishing needs matching registrations on the GitHub side and the
index side; no API token is stored anywhere.

1. **GitHub environments**: create `pypi` and `testpypi` in the repository
   settings (Settings > Environments; empty, no protection rules required).
   The workflow's `environment:` resolves to one of them - `pypi` for a tag
   push, `testpypi` for a `workflow_dispatch` dry run - and the OIDC token
   names that environment, which (Test)PyPI verifies.
2. **(Test)PyPI trusted publishers**: on PyPI, register a trusted publisher
   for **each package** (`boto3-s3` and `boto3-s3-cli`): owner `izumo-m`,
   repository `boto3-s3`, workflow `release.yml`, environment `pypi`. On
   TestPyPI, the same two registrations with environment `testpypi` (needed
   only for dry runs). A package that has never been uploaded is registered
   as a *pending* publisher; the project name is claimed on its first
   publish.

The environment name registered on (Test)PyPI must match the workflow's
`environment:` value exactly, or the token exchange is refused and nothing
uploads.

## Releasing

Work happens on `develop`; releases are cut on `main`, and the tag lives on
`main`'s merge commit. Both packages release from this same branch line — the
tag prefix selects which one publishes — so they can go out together (two tags
on one commit) or independently (one tag).

The steps below release `boto3-s3` X.Y.Z. A CLI release follows the same steps
with `cli/pyproject.toml`, `cli/CHANGELOG.md`, and a `boto3-s3-cli-vX.Y.Z` tag.
A joint release puts both tags on the same merge commit, but pushes them in
order (see step 3).

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
     resolves an older library from PyPI and crashes at runtime. Between
     releases, `develop` intentionally keeps the last *released* floor (the
     workspace resolves the library from source, so development never needs
     an early bump); updating the range is this step, done at release time —
     a pre-release audit that sees the older floor on `develop` is looking at
     the intended state, not a pending action.
2. Merge into `main`, keeping the merge commit as a clear release boundary:
   ```bash
   git switch main && git merge --no-ff develop
   ```
3. Tag that merge commit and push — **this push is what publishes**. For a
   joint release, publish the **library first and wait for its Actions run to
   finish**: the CLI's install resolves `boto3-s3` from PyPI, so the dependency
   must exist there before the CLI lands. The workflow enforces this — a CLI
   release aborts before upload unless the target index already serves a
   `boto3-s3` version satisfying the CLI's dependency range. (For the same
   reason, a TestPyPI dry run of the CLI needs the library dry-published there
   first.)
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

Watch the run in the Actions tab. Before building, the release workflow syncs
the locked workspace and reruns formatting, lint, type checking, and the default
test suite; a version, dependency, or source-quality failure uploads nothing.
