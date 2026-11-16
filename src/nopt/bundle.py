"""Build and verify immutable release bundles using archive metadata and hashes."""

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

from .config import Config
from .source import Context, ContextData, SourceIdentity
from .project import rename_distribution

_SCHEMA_VERSION = 1


class ReleaseManifest(TypedDict):
    """Verified metadata binding the release policy, source and original bundle files."""

    schema_version: int
    config: dict[str, object]
    source: SourceIdentity
    distribution: str
    context: ContextData
    files: dict[str, str]


def sha256(path: Path) -> str:
    """Return the digest of original bytes."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_artifacts(
    directory: Path, name: str, version: str
) -> dict[str, str]:  # pylint: disable=too-many-locals
    """Validate exact wheel/sdist identity, metadata and pure-Python wheel tags."""
    files = sorted(directory.iterdir())
    wheels = [p for p in files if p.name.endswith(".whl")]
    sdists = [p for p in files if p.name.endswith(".tar.gz")]
    if len(files) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one .tar.gz sdist")
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError("distribution must be a regular file")
        if path in wheels:
            dist, parsed, _, tags = parse_wheel_filename(path.name)
            if any(tag.abi != "none" or tag.platform != "any" for tag in tags):
                raise ValueError("v1 requires a pure-Python wheel")
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
        if canonicalize_name(name) != dist or str(parsed) != version:
            raise ValueError("distribution filename disagrees with release identity")
        if (
            canonicalize_name(metadata.get("Name", "")) != canonicalize_name(name)
            or metadata.get("Version") != version
        ):
            raise ValueError("distribution metadata disagrees with release identity")
    return {path.name: sha256(path) for path in sorted(files)}


def validate_output(root: Path, destination: Path) -> Path:
    """Validate build output location without creating it."""
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("bundle output already exists; choose a new output path")
    if destination.is_relative_to(root):
        # Avoid making our own future output an untracked source input.
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(destination)], cwd=root, check=False
        )
        if ignored.returncode != 0:
            raise ValueError("bundle output inside checkout must be Git-ignored")
    return destination


# pylint: disable-next=too-many-locals,too-many-arguments,too-many-positional-arguments
def make_bundle(
    root: Path,
    destination: Path,
    config: Config,
    context: Context,
    source: SourceIdentity,
    notes: str | None,
) -> ReleaseManifest:
    """Build from the checkout and prepare a complete local release bundle."""
    ### Build from the checkout after eligibility has been checked.
    destination = validate_output(root, destination)
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary) / "bundle"
        project_file = root / "pyproject.toml"
        original = (
            project_file.read_bytes()
            if config.repository == "testpypi" and config.distribution != config.name
            else None
        )
        distributions = bundle / "dist"
        try:
            if original is not None:
                # Eligibility is checked before the temporary name/formatting changes.
                rename_distribution(project_file, config.distribution)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--installer=uv",
                    "--outdir",
                    str(distributions),
                    str(root),
                ],
                check=True,
            )
        finally:
            # Restore original comments, formatting and pre-existing uncommitted edits.
            if original is not None:
                project_file.write_bytes(original)
        ### Validate artifacts and bind them to the original project configuration.
        hashes = {
            "dist/" + name: digest
            for name, digest in validate_artifacts(
                distributions, config.distribution, config.version
            ).items()
        }
        if notes is not None:
            (bundle / "notes.md").write_text(notes, encoding="utf-8")
            hashes["notes.md"] = sha256(bundle / "notes.md")
        data: ReleaseManifest = {
            "schema_version": _SCHEMA_VERSION,
            "config": config.to_mapping(),
            "source": source,
            "distribution": config.distribution,
            "context": cast(ContextData, asdict(context)),
            "files": hashes,
        }
        (bundle / "release.json").write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        ### Preserve checksum text and original bytes in the final local bundle.
        checksums = dict(data["files"])
        checksums["release.json"] = sha256(bundle / "release.json")
        (bundle / "SHA256SUMS").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())),
            encoding="utf-8",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, destination)
    return data


# pylint: disable-next=too-many-branches,too-many-locals,too-many-statements
def read_bundle(
    root: Path,
) -> tuple[ReleaseManifest, Config]:
    """Verify every byte and narrow decoded release metadata before using it."""
    ### Verify checksums and reject paths outside the original bundle.
    root = root.resolve()
    if (root / "SHA256SUMS").is_symlink():
        raise ValueError("SHA256SUMS must be a regular file")
    checksums: dict[str, str] = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ValueError("invalid SHA256SUMS")
        digest, name = match.groups()
        if name in checksums or name.startswith("/") or "\\" in name or ".." in Path(name).parts:
            raise ValueError("unsafe or duplicate bundle path")
        path = root / name
        if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise ValueError("bundle entry must be a regular file inside bundle")
        if sha256(path) != digest:
            raise ValueError(f"bundle checksum mismatch: {name}")
        checksums[name] = digest
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != set(checksums) | {"SHA256SUMS"} or "release.json" not in checksums:
        raise ValueError("bundle file set disagrees with SHA256SUMS")

    ### Narrow untrusted JSON to the schema's configuration and identity fields.
    decoded: object = json.loads((root / "release.json").read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("release manifest must be an object")
    data = cast(dict[str, object], decoded)
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != _SCHEMA_VERSION:
        raise ValueError("unsupported release manifest schema")
    expected_files = {name: digest for name, digest in checksums.items() if name != "release.json"}
    if data.get("files") != expected_files:
        raise ValueError("release manifest disagrees with checksums")
    config_data = data.get("config")
    if not isinstance(config_data, dict):
        raise ValueError("release configuration must be an object")
    config = Config.from_mapping(cast(dict[str, object], config_data))
    distribution = data.get("distribution")
    if not isinstance(distribution, str) or distribution != config.distribution:
        raise ValueError("invalid release distribution name")
    source_data = data.get("source")
    if not isinstance(source_data, dict):
        raise ValueError("release source identity must be an object")
    commit = source_data.get("commit")
    tag = source_data.get("tag")
    dirty = source_data.get("dirty")
    if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise ValueError("invalid release source commit")
    if not isinstance(tag, str) or tag != "v" + config.version or not isinstance(dirty, bool):
        raise ValueError("invalid release tag or dirty status")
    context_data = data.get("context")
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

    ### Check distribution metadata and the configured optional notes file.
    hashes = validate_artifacts(root / "dist", distribution, config.version)
    artifact_files = {"dist/" + name: digest for name, digest in hashes.items()}
    allowed_files = set(artifact_files)
    if config.changelog is not False:
        allowed_files.add("notes.md")
    if set(expected_files) != allowed_files:
        raise ValueError("unexpected release bundle member")
    if artifact_files != {
        name: digest for name, digest in expected_files.items() if name.startswith("dist/")
    }:
        raise ValueError("distribution hashes disagree with manifest")
    manifest: ReleaseManifest = {
        "schema_version": schema,
        "config": config.to_mapping(),
        "source": {"commit": commit, "tag": tag, "dirty": dirty},
        "distribution": distribution,
        "context": cast(ContextData, context_data),
        "files": expected_files,
    }
    return manifest, config
