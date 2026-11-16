# Nicks Opinionated (Python Package) Publishing Tool

`nopt` checks Python release eligibility, builds a verified release bundle and optionally publishes a GitHub release. GitHub Actions handles CI, approvals and artifact storage; the PyPA publishing action uploads distributions to PyPI or TestPyPI.

This initial implementation requires Python 3.11.4+ and a Python project in a Git checkout. Any PEP 517 build backend supported by python-build can be used. It supports one pure-Python wheel plus one source distribution. There is no PyPI upload command.

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

Dirty status comes directly from `git status --porcelain=v1 -z --untracked-files=all`. All tracked changes and nonignored untracked files in the checkout count, even when they are not packaged. Git's ignore rules determine what is ignored; nopt does not guess build/cache directories or inspect backend source manifests. Default dirty allowance is dev only. This is independent of tag-to-HEAD equality.

### Changelog

Use a base-version section, shared by dev/pre/final releases:

```markdown
## [1.2.3] - UNRELEASED

### Added

- Describe the changes.
```

Exactly one matching level-two section is required and the complete section is used as release notes. Its body must contain a nonblank line that does not start with `#`. Dev/pre require `UNRELEASED`; final releases require a real `YYYY-MM-DD` date. Nopt never changes dates. `changelog = false` or `--no-changelog` disables all changelog policy.

## Command flow

| Command or workflow step | Responsibility |
| --- | --- |
| `nopt ci-preflight` | Reject an existing index version, then check source, tag, branch, dirty policy and changelog |
| Project CI | Your lint and tests |
| `nopt build` | Repeat eligibility checks, build from the checkout using isolated `python -m build --installer=uv`, validate artifacts and create the bundle |
| Actions upload / approval / download | Preserve the original bundle and gate publishing |
| `nopt publish-release-preflight --bundle PATH` | Verify bundle integrity and current commit/tag/workflow identity, then recheck index absence and live GitHub state |
| PyPA publishing action | OIDC authentication, attestations and PyPI/TestPyPI upload |
| `nopt publish-github-release --bundle PATH` | If configured, wait for exact PyPI filenames/hashes, then publish the GitHub release and original assets |

GitHub latest marking uses Git tag refs, not index versions, tag dates or GitHub release creation order. Only the greatest canonical final-version v-prefixed tag is eligible; older maintenance versions and all dev/pre releases use make_latest=false. Unsupported tags are ignored. The current final tag must appear in the reference snapshot. GitHub publication reads matching tag refs from the repository's Git database API, so this decision has no PyPI dependency. GitHub-only publication remains future planning; the current command still verifies the preceding index upload.

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

Local mode derives project/version/commit from the checkout and the expected tag from the version. `--project PATH` selects another checkout; `--tag` is local-only and must identify the same existing canonical tag pointing to HEAD. Local mode checks local branches without fetching. GHA checks fetched repository branches and requires matching `GITHUB_REF`, `GITHUB_SHA`, repository, run ID and attempt. Local package preflight uses only local Git HEAD as its expected commit; policy and notes come from the bundle. A local bundle is not accepted for GHA publishing. Local GitHub release publishing is not supported.

Builds run directly from the checkout with the project's configured backend. Build-dependency isolation and the frontend's default sdist-then-wheel behavior remain enabled. Backend-generated files such as build/ or egg-info should be ignored by the client project's Git rules. Nopt neither copies the source tree nor excludes directories itself.

For optional TestPyPI username suffixing, eligibility checks run first against the original project name. Nopt saves the raw pyproject.toml bytes, parses with tomllib, changes project.name and writes temporary TOML using tomli-w. The temporary file may lose comments and formatting. After the build, finally cleanup restores the exact original bytes in both local and GHA modes, including when rewriting or building fails. Pre-existing uncommitted edits are preserved; nopt does not reset the file to HEAD. Other backend-generated checkout files remain the client's responsibility.

Static project.name is still required, but alternate valid TOML spellings such as quoted tables/keys or dotted keys are supported. No staging copy is made, and import names remain unchanged. Later commands independently derive the same effective name from the original project.name and configured suffix. Both publishing commands read configuration from the original pyproject.toml stored in the verified bundle. Each invocation applies only its own CLI overrides; overrides are never carried between steps. Overrides must be compatible with the saved distribution name and available release notes; publishing never rebuilds or regenerates missing content.

Output must be a new directory; if it is inside the checkout, it must be Git-ignored. Add release-bundle/ to .gitignore to use the default output path.

The bundle contains `dist/` (wheel + sdist), the original `pyproject.toml`, optional `release-notes.md`, `release.json` and standard `SHA256SUMS`. `release-notes.md` holds the extracted changelog section used as the GitHub release body, so publication uses the same checked text as the build. Manifest hash keys are relative to the bundle root. Release identity binds original hashes, actual distribution identity, source commit/tag and pre-build dirty status, and GHA run identity when available. It does not save resolved configuration or CLI overrides. SHA256SUMS includes release.json; the GitHub artifact/run identity supplies the workflow trust boundary. Checksums alone are not signatures. The package does not inspect generated runtime `_version.py` or require setuptools-git-metadata.

Both GHA publishing jobs consume the bundle without a source checkout. Source dirty, branch and changelog checks and artifact metadata inspection are completed before bundling. Publishing checks bundle integrity and expected identity, then repeats only mutable external checks: existing index/release records, live tag refs, uploaded filenames/hashes and latest-final tag ranking. Local GitHub-release bundle acquisition remains deferred.

### Generate a workflow

```sh
nopt workflow-template --output .github/workflows/release.yml
```

The command refuses to overwrite an existing file. Without `--output`, it prints the template. Replace the failing `<your CI goes here>` placeholder with your CI. The template uses tag triggers, full checkout history, separate jobs and 90-day artifact retention. The build job records the immutable artifact ID; downstream jobs download that exact ID rather than resolving an artifact name again. Edit the template directly; generation has no persistent TOML options.

Before enabling publication:

- Pin tool/action revisions and review the workflow.
- Configure the `pypi` GitHub environment and approvals.
- Register the repository/workflow/environment as a PyPI Trusted Publisher.
- For TestPyPI, set `repository = "testpypi"`, use the TestPyPI environment/publisher, and set the publishing action's TestPyPI upload URL.

Generation does not change settings. Keep distributions passed to the upload action in a separate copy so generated attestations do not mutate the original verified bundle. The template archives that publication directory as evidence, including on upload failure. Only the publisher has `id-token: write`; only the GitHub release job has `contents: write`.

## Limits and deferred recovery

Existing GitHub releases cause failure. Automatic release/asset reconciliation and partial PyPI upload recovery are deferred. If release creation succeeds but an asset upload fails, a retry also fails because the release exists; an owner must review the original evidence and handle recovery manually. Do not rebuild or re-upload a published version. Expired Actions artifacts limit recovery; retain original bytes/evidence separately when needed. The initial template also binds bundles to a run attempt and does not implement failed-job rerun recovery; do not use reruns to attempt another package upload. Same-tag workflow runs are serialized without automatic cancellation.

Backend-specific source selection is delegated to the backend through python-build. Nopt does not calculate a source-file fingerprint or promise that backend builds leave the checkout untouched. Build hooks run project code; only run nopt on trusted projects. No production package migrations are included.

## Develop

```sh
uv sync --group dev
uvx tox -e lint
uvx tox
```

CI runs tox with tox-uv on Linux, macOS and Windows for CPython 3.11–3.15 and PyPy 3.11–3.12. Integration tests use real local Git repositories and isolated wheel/sdist builds. Build tests exercise both setuptools and Hatchling, with and without TestPyPI name rewriting. No tests perform package publication, create GitHub releases or change remote settings.
