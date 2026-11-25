"""Build and verify immutable release bundles using archive metadata and hashes."""

from collections.abc import Mapping
from dataclasses import asdict, fields
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from typing import TypedDict, cast

from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename

from .config import Config, load
from .git_metadata import verify_git_metadata
from .source import Context, ContextData, SourceIdentity
from .project import rename_distribution

_SCHEMA_VERSION = 1
_MANIFEST_NAME = "release.json"
_MANIFEST_CHECKSUM = "release.json.sha256sum"


class ReleaseManifest(TypedDict):
    """Verified metadata binding release identity, source and original release bundle files."""

    schema_version: int
    source: SourceIdentity
    distribution_name: str
    context: ContextData
    files: dict[str, str]


def sha256(path: Path) -> str:
    """Return the digest of original bytes."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_artifacts(
    directory: Path, name: str, version: str
) -> dict[str, str]:  # pylint: disable=too-many-locals
    """Validate exact wheel/sdist identity and metadata.

    Returns:
        A mapping from artifact filenames to their SHA256 sums.
    """
    files = sorted(directory.iterdir())
    wheels = [p for p in files if p.name.endswith(".whl")]
    sdists = [p for p in files if p.name.endswith(".tar.gz")]
    if len(files) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one .tar.gz sdist")
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError("distribution must be a regular file")
        if path in wheels:
            dist, parsed, _, _ = parse_wheel_filename(path.name)
            with zipfile.ZipFile(path) as archive:
                members = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
                if len(members) != 1:
                    raise ValueError("expected one wheel METADATA")
                metadata = BytesParser().parsebytes(archive.read(members[0]))
        else:
            dist, parsed = parse_sdist_filename(path.name)
            with tarfile.open(path) as archive:
                members = [
                    m
                    for m in archive.getmembers()
                    if len(Path(m.name).parts) == 2 and m.name.endswith("/PKG-INFO")
                ]
                if len(members) != 1 or not members[0].isfile():
                    raise ValueError("expected one top-level sdist PKG-INFO")
                stream = archive.extractfile(members[0])
                if stream is None:
                    raise ValueError("missing PKG-INFO")
                metadata = BytesParser().parsebytes(stream.read())
        # Catch backend output for a different project/version (for example, stale
        # backend version configuration or a backend that ignores the temporary name).
        if canonicalize_name(name) != dist or str(parsed) != version:
            raise ValueError("distribution filename disagrees with release identity")
        if (
            canonicalize_name(metadata.get("Name", "")) != canonicalize_name(name)
            or metadata.get("Version") != version
        ):
            raise ValueError("distribution metadata disagrees with release identity")
    return {path.name: sha256(path) for path in sorted(files)}


def validate_build_output_path(project_path: Path, release_bundle_path: Path) -> None:
    """Validate build output location without creating it."""
    release_bundle_path = release_bundle_path.resolve()
    if release_bundle_path.exists():
        raise ValueError("release bundle output already exists; choose a new output path")
    if release_bundle_path.is_relative_to(project_path):
        # Avoid making our own future output an untracked source input.
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(release_bundle_path)], cwd=project_path, check=False
        )
        if ignored.returncode != 0:
            raise ValueError("release bundle output inside checkout must be Git-ignored")
    return


# pylint: disable-next=too-many-locals,too-many-arguments,too-many-positional-arguments
def build_release_bundle(
    project_path: Path,
    release_bundle_path: Path,
    config: Config,
    context: Context,
    source: SourceIdentity,
    release_notes: str | None,
) -> ReleaseManifest:
    """Build a release bundle; the caller must first check source eligibility."""
    ### The caller checks eligibility; this function builds and packages its inputs.
    release_bundle_path = release_bundle_path.resolve()
    validate_build_output_path(project_path, release_bundle_path)
    with tempfile.TemporaryDirectory() as temporary:
        release_bundle = Path(temporary) / "release_bundle"
        project_file = project_path / "pyproject.toml"
        original_pyproject_bytes = project_file.read_bytes()
        requires_testpypi_rename = (
            config.repository == "testpypi" and config.distribution_name != config.name
        )
        distributions = release_bundle / "dist"
        try:
            if requires_testpypi_rename:
                # Change only build-time distribution identity, restoring the source below.
                rename_distribution(project_file, config.distribution_name)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--installer=uv",
                    "--outdir",
                    str(distributions),
                    str(project_path),
                ],
                check=True,
            )
        finally:
            # Restore original comments, formatting and pre-existing uncommitted edits.
            if requires_testpypi_rename:
                project_file.write_bytes(original_pyproject_bytes)
        ### Validate artifacts and bind them to the original project configuration.
        artifact_hashes = validate_artifacts(
            distributions, config.distribution_name, config.version
        )
        verify_git_metadata(
            distributions, artifact_hashes, original_pyproject_bytes, source["commit"]
        )
        hashes = {"dist/" + name: digest for name, digest in artifact_hashes.items()}
        # Preserve original configuration, not the temporary TestPyPI build name.
        # Publishing derives the same effective name without rebuilding distributions.
        (release_bundle / "pyproject.toml").write_bytes(original_pyproject_bytes)
        hashes["pyproject.toml"] = sha256(release_bundle / "pyproject.toml")
        if release_notes is not None:
            (release_bundle / "release-notes.md").write_text(release_notes, encoding="utf-8")
            hashes["release-notes.md"] = sha256(release_bundle / "release-notes.md")
        manifest: ReleaseManifest = {
            "schema_version": _SCHEMA_VERSION,
            "source": source,
            "distribution_name": config.distribution_name,
            "context": cast(ContextData, asdict(context)),
            "files": hashes,
        }
        manifest_path = release_bundle / _MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (release_bundle / _MANIFEST_CHECKSUM).write_bytes(
            f"{sha256(manifest_path)}  {_MANIFEST_NAME}\n".encode("ascii")
        )
        release_bundle_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(release_bundle, release_bundle_path)
    return manifest


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys in the manifest and its payload inventory."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate key in release manifest")
    return result


def _read_release_manifest(release_bundle: Path) -> dict[str, object]:
    """Verify the manifest bytes before parsing them."""
    checksum_path = release_bundle / _MANIFEST_CHECKSUM
    if not checksum_path.exists() and not checksum_path.is_symlink():
        raise ValueError(
            f"missing {_MANIFEST_CHECKSUM}; use the nopt version that built the bundle"
        )
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise ValueError(f"{_MANIFEST_CHECKSUM} must be a regular file")
    match = re.fullmatch(rb"([0-9a-f]{64})  release\.json\n", checksum_path.read_bytes())
    if not match:
        raise ValueError(f"invalid {_MANIFEST_CHECKSUM}")
    manifest_path = release_bundle / _MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"{_MANIFEST_NAME} must be a regular file")
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest().encode("ascii") != match[1]:
        raise ValueError(f"release bundle checksum mismatch: {_MANIFEST_NAME}")
    decoded: object = json.loads(
        manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object
    )
    if not isinstance(decoded, dict):
        raise ValueError("release manifest must be an object")
    return cast(dict[str, object], decoded)


def _verify_bundle_payload(release_bundle: Path, inventory: object) -> dict[str, str]:
    """Check the exact set of payload files and their recorded digests."""
    if not isinstance(inventory, dict):
        raise ValueError("release manifest files must be an object")
    checksums: dict[str, str] = {}
    for name, digest in inventory.items():
        if any(part in {"", ".", ".."} for part in name.split("/")) or re.search(
            r"[\\:\x00-\x1f]", name
        ):
            raise ValueError(f"unsafe release bundle path: {name!r}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid release bundle checksum: {name}")
        checksums[name] = digest

    ### Trust build-time metadata checks; verify only the checked release bundle layout here.
    artifact_files = {name for name in checksums if name.startswith("dist/")}
    if (
        len(artifact_files) != 2
        or sum(name.endswith(".whl") for name in artifact_files) != 1
        or sum(name.endswith(".tar.gz") for name in artifact_files) != 1
        or any(name.count("/") != 1 for name in artifact_files)
    ):
        raise ValueError("release bundle must contain exactly one wheel and one sdist under dist/")
    allowed_files = artifact_files | {"pyproject.toml", "release-notes.md"}
    if unexpected_files := set(checksums) - allowed_files:
        raise ValueError(f"unexpected release bundle members: {sorted(unexpected_files)}")
    if "pyproject.toml" not in checksums:
        raise ValueError("release bundle manifest must list pyproject.toml")

    actual = {
        path.relative_to(release_bundle).as_posix()
        for path in release_bundle.rglob("*")
        if path.is_symlink() or not path.is_dir()
    }
    expected = set(checksums) | {_MANIFEST_NAME, _MANIFEST_CHECKSUM}
    if actual != expected:
        raise ValueError(
            "release bundle file set disagrees with manifest: "
            f"unexpected={sorted(actual - expected)}, missing={sorted(expected - actual)}"
        )
    for name, digest in checksums.items():
        path = release_bundle / name
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(release_bundle)
        ):
            raise ValueError("release bundle entry must be a regular file inside release bundle")
        if sha256(path) != digest:
            raise ValueError(f"release bundle checksum mismatch: {name}")
    return checksums


def read_and_verify_release_bundle(
    release_bundle: Path, overrides: Mapping[str, object] | None = None
) -> tuple[ReleaseManifest, Config]:
    """Verify every byte and narrow decoded release metadata before using it."""
    release_bundle = release_bundle.resolve()
    manifest_data = _read_release_manifest(release_bundle)
    schema = manifest_data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != _SCHEMA_VERSION:
        raise ValueError("unsupported release manifest schema")
    expected_files = _verify_bundle_payload(release_bundle, manifest_data.get("files"))
    # Re-read project policy from the bundled TOML for this invocation only.
    config = load(release_bundle, overrides)
    distribution_name = manifest_data.get("distribution_name")
    if not isinstance(distribution_name, str) or distribution_name != config.distribution_name:
        raise ValueError(
            "release bundle distribution name disagrees with this invocation configuration"
        )

    # Validate recorded source identity without repeating checkout eligibility checks.
    source_data = manifest_data.get("source")
    if not isinstance(source_data, dict):
        raise ValueError("release source identity must be an object")
    commit = source_data.get("commit")
    tag = source_data.get("tag")
    dirty = source_data.get("dirty")
    if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise ValueError("invalid release source commit")
    if not isinstance(tag, str) or tag != "v" + config.version or not isinstance(dirty, bool):
        raise ValueError("invalid release tag or dirty status")

    # Execution identity is checked separately against the current publishing job.
    context_data = manifest_data.get("context")
    if not isinstance(context_data, dict) or set(context_data) != {
        field.name for field in fields(Context)
    }:
        raise ValueError("invalid release execution context")
    if not isinstance(context_data.get("local"), bool) or any(
        value is not None and not isinstance(value, str)
        for key, value in context_data.items()
        if key != "local"
    ):
        raise ValueError("invalid release execution context values")

    if config.github_release_requested and "release-notes.md" not in expected_files:
        raise ValueError("GitHub release publication requires bundled release-notes.md")
    manifest: ReleaseManifest = {
        "schema_version": schema,
        "source": {"commit": commit, "tag": tag, "dirty": dirty},
        "distribution_name": distribution_name,
        "context": cast(ContextData, context_data),
        "files": expected_files,
    }
    return manifest, config
