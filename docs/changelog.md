# Changelog

## [0.1.1] - UNRELEASED

### Changed

- Attach the wheel, source distribution, and a `SHA256SUMS` file covering those two files to each GitHub release. Use the bundled changelog notes as the release body and retain the full release bundle in Actions.
- Verify `release.json` against its `release.json.sha256sum` checksum, then verify the bundle payload against the manifest. Use the nopt version that built older bundles with the internal `SHA256SUMS` layout.

## [0.1.0] - 2026-11-24

### Added

- Initial release checks, isolated builds, release bundles and GitHub release publishing.
