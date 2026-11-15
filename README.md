# Nicks Opinionated (Python Package) Publishing Tool

`nopt` checks Python release eligibility, builds a verified release bundle and optionally publishes a GitHub release. GitHub Actions handles CI, approvals and artifact storage; the PyPA publishing action uploads distributions to PyPI or TestPyPI.

This initial implementation requires Python 3.11.4+ and a setuptools project at the root of a Git repository. It supports one pure-Python wheel plus one source distribution. There is no PyPI upload command.

## Install

Install from a reviewed checkout:

```sh
python -m pip install .
nopt --help
```

The project has not been published to PyPI. Workflow templates install nopt from its Git repository; pin that requirement and action references to reviewed immutable revisions before enabling production publishing.

## Configure a project

Declare a static project name and version. This example shows all optional settings and their defaults, except `testpypi-username`, which has no default:

```toml
[project]
name = "example-package"
version = "1.2.3.dev4"

[tool.nopt]
changelog = "docs/changelog.md" # false disables extraction and date/content checks
repository = "pypi" # or "testpypi"
allow-dirty = ["dev"] # [] forbids all dirty releases
final-release-branches = ["main"] # nonempty list; false is invalid
pre-release-branches = ["main"] # false allows any branch
dev-release-branches = false # any branch
# testpypi-username = "alice" # TestPyPI only: example-package-alice
github-release-publish = true
github-release-types = ["pre", "final"] # [] creates no GitHub releases
```

CLI policy options override TOML, which overrides defaults. Boolean false and empty lists retain their meaning. Unknown settings fail. Use `--help` on each command for options; there are no force or skip-check flags. Workflow generation settings do not belong in pyproject.toml.

Accepted versions are exactly canonical `X.Y.Z`, `X.Y.Z.devN`, `X.Y.ZaN`, `X.Y.ZbN`, or `X.Y.ZrcN`. Alternatives such as `1.2.3.rc1` are rejected. Epochs, local versions, post releases, compound suffixes and two-component versions are rejected. Release tags must be `v<version>` and point to the current HEAD. Nopt never creates or moves tags.

A restricted release group requires the commit to be reachable from at least one configured branch; it need not be the latest commit. Names are literal, with no globs. Empty branch lists are invalid. Final releases always require a restriction; pre/dev accept explicit false. A detached checkout is valid.

Dirty status uses the union of fresh setuptools sdist source manifests for the working tree and HEAD under current selection controls. Selected tracked changes/deletions and nonignored untracked files count; excluded root files do not. Default dirty allowance is dev only. This is independent of tag-to-HEAD equality.

### Changelog

Use a base-version section, shared by dev/pre/final releases:

```markdown
## [1.2.3] - UNRELEASED

### Added

- Describe the changes.
```

Exactly one matching level-two section is required and the complete section is used as release notes. Its body must contain a nonblank line that does not start with `#`. Dev/pre may use `UNRELEASED`; final releases require a real `YYYY-MM-DD` date. Nopt never changes dates. `changelog = false` or `--no-changelog` disables all changelog policy.

## Command flow

| Command or workflow step | Responsibility |
| --- | --- |
| `nopt ci-preflight` | Reject an existing index version, then check source, tag, branch, dirty policy and changelog |
| Project CI | Your lint and tests |
| `nopt build` | Repeat eligibility checks, stage sources, run isolated `python -m build --installer=uv`, validate artifacts and create the bundle |
| Actions upload / approval / download | Preserve the original bundle and gate publishing |
| `nopt publish-release-preflight --bundle PATH` | Verify exact bundle bytes, policy, selected source identity, tag and index eligibility |
| PyPA publishing action | OIDC authentication, attestations and PyPI/TestPyPI upload |
| `nopt publish-github-release --bundle PATH` | If configured, wait for exact PyPI filenames/hashes, then publish the GitHub release and original assets |

When GitHub release publication is disabled or the release group is excluded, successful PyPI upload ends publishing. There is no post-upload index polling in that path. Preflight refuses any index record for the target version, even an empty, partial or yanked record; only HTTP 404 means absent. Network/API errors fail immediately.

Use `--check-only` when nopt owns the action (`build`, `publish-github-release`); normal actions always run their checks too. Standalone preflights gate external steps. Build checks-only checks release eligibility without producing artifacts; it cannot validate artifacts that have not been built.

### Local checks and builds

Select local mode explicitly:

```sh
nopt ci-preflight --local
nopt build --local --check-only
nopt build --local --output /absolute/path/to/new-bundle
nopt publish-release-preflight --local --bundle /absolute/path/to/new-bundle
```

Local mode derives project/version/commit from the checkout and the expected tag from the version. `--project PATH` selects another checkout; `--tag` must identify the same existing canonical tag pointing to HEAD. Local mode checks local branches without fetching. GHA checks fetched repository branches and requires matching `GITHUB_REF`, `GITHUB_SHA`, repository, run ID and attempt. A local bundle is not accepted for GHA publishing. Local GitHub release publishing is not supported.

Builds run in disposable staging directories. Optional TestPyPI username suffixing changes only the staged distribution name, preserving import names. Backend selection runs in isolated environments using declared build dependencies. Neither operation rewrites the checkout. Output must be a new directory; if it is inside the checkout, it must be Git-ignored. Add `release-bundle/` to `.gitignore` to use the default output path.

The bundle contains `dist/` (wheel + sdist), optional `notes.md`, `release.json` and standard `SHA256SUMS`. Release identity binds original hashes, resolved policy, source commit/tag, a fingerprint of selected inputs, and GHA run identity when available. SHA256SUMS includes release.json; the GitHub artifact/run identity supplies the workflow trust boundary. Checksums alone are not signatures. The package does not inspect generated runtime `_version.py` or require setuptools-git-metadata.

### Generate a workflow

```sh
nopt workflow-template --output .github/workflows/release.yml
```

The command refuses to overwrite an existing file. Without `--output`, it prints the template. Replace the failing `<your CI goes here>` placeholder with your CI. The template uses tag triggers, full checkout history, separate jobs and 90-day artifact retention. Edit the template directly; generation has no persistent TOML options.

Before enabling publication:

- Pin tool/action revisions and review the workflow.
- Configure the `pypi` GitHub environment and approvals.
- Register the repository/workflow/environment as a PyPI Trusted Publisher.
- For TestPyPI, set `repository = "testpypi"`, use the TestPyPI environment/publisher, and set the publishing action's TestPyPI upload URL.

Generation does not change settings. Keep distributions passed to the upload action in a separate copy so generated attestations do not mutate the original verified bundle. The template archives that publication directory as evidence, including on upload failure. Only the publisher has `id-token: write`; only the GitHub release job has `contents: write`.

## Limits and deferred recovery

Existing GitHub releases cause failure. Automatic release/asset reconciliation and partial PyPI upload recovery are deferred. If release creation succeeds but an asset upload fails, a retry also fails because the release exists; an owner must review the original evidence and handle recovery manually. Do not rebuild or re-upload a published version. Expired Actions artifacts limit recovery; retain original bytes/evidence separately when needed. The initial template also binds bundles to a run attempt and does not implement failed-job rerun recovery; do not use reruns to attempt another package upload. Same-tag workflow runs are serialized without automatic cancellation.

Manifest enumeration is deliberately conservative and may be expensive. Unsupported backends, monorepo subprojects, source inputs escaping the root and newline filenames fail. Symlinks escaping the checkout fail. Arbitrary custom selection hooks, concurrent changes outside observable source snapshots and cross-platform filename behavior require additional adoption validation. Build hooks run project code; only run nopt on trusted projects. Ignored source data is copied into staging and can be selected by the backend, but ignored untracked files do not set Git dirty status. Disposable tooling/output directories (.venv, venv, .tox, __pycache__, .pytest_cache, build, dist, release-bundle, node_modules and egg-info) are excluded when ignored and are not supported distribution input locations; tracked files are never excluded by that rule. Fresh manifests avoid stale checkout egg-info caches. No production package migrations are included.

## Develop

```sh
uv sync --group dev
uvx tox -e lint
uvx tox
```

CI runs tox with tox-uv on Linux, macOS and Windows for CPython 3.11–3.15 and PyPy 3.11–3.12. Integration tests use real local Git repositories and isolated wheel/sdist builds. Optional companion integration uses `NOPT_COMPANION_SOURCE=/path/to/setuptools-git-metadata uvx tox -e companion`. No tests perform package publication, create GitHub releases or change remote settings.
