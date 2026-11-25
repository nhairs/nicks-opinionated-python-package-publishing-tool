# Changelog

## [0.2.0](https://github.com/nhairs/nicks-opinionated-python-package-publishing-tool/compare/v0.1.0...v0.2.0) - 2026-11-25

### Added

- Allow release branch policies to use a trailing wildcard, such as `release/1.*`, to select maintenance branches.
- Verify the embedded Git commit in the wheel and source distribution at build time for projects that opt in to `setuptools-git-metadata`.
- Include build Git metadata in nopt's own distributions.

### Changed

- Attach the wheel, source distribution, and a `SHA256SUMS` file covering those two files to each GitHub release. Use the bundled changelog notes as the release body and retain the full release bundle in Actions.
- Verify `release.json` against its `release.json.sha256sum` checksum, then verify the bundle payload against the manifest. Use the nopt version that built older bundles with the internal `SHA256SUMS` layout.
- Allow publication job reruns to reuse the build job's bundle from an earlier attempt of the same GitHub Actions run. Record the build attempt in the manifest and retain the publication checks.

### Fixed

- Reject a missing or moved local release tag during package publication preflight. The check uses local Git data without fetching.

## [0.1.0](https://github.com/nhairs/nicks-opinionated-python-package-publishing-tool/releases/tag/v0.1.0) - 2026-11-24

### Added

- Initial release checks, isolated builds, release bundles and GitHub release publishing.
