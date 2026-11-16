"""Build and verify immutable release bundles using archive metadata and hashes."""

from dataclasses import asdict
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

from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename

from .config import Config, validate
from .source import Context
from .project import rename_distribution

SCHEMA = 1


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


def make_bundle(  # pylint: disable=too-many-locals
    root: Path, destination: Path, config: Config, context: Context, source: dict, notes: str | None
) -> dict:
    """Build from the checkout and prepare a complete local release bundle."""
    destination = validate_output(root, destination)
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary) / "bundle"
        if config.testpypi_username:
            # Eligibility is checked against the original name before this uncommitted edit.
            rename_distribution(root / "pyproject.toml", config.distribution)
        distributions = bundle / "dist"
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
        hashes = validate_artifacts(distributions, config.distribution, config.version)
        if notes is not None:
            (bundle / "notes.md").write_text(notes, encoding="utf-8")
            hashes["notes.md"] = sha256(bundle / "notes.md")
        data = {
            "schema_version": SCHEMA,
            "config": asdict(config),
            "source": source,
            "distribution": config.distribution,
            "context": asdict(context),
            "files": {
                ("dist/" + name if name.endswith((".whl", ".tar.gz")) else name): digest
                for name, digest in hashes.items()
            },
        }
        data = json.loads(json.dumps(data))
        (bundle / "release.json").write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        checksums = dict(data["files"])
        checksums["release.json"] = sha256(bundle / "release.json")
        (bundle / "SHA256SUMS").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())),
            encoding="utf-8",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, destination)
    return data


def read_bundle(root: Path) -> tuple[dict, Config]:  # pylint: disable=too-many-branches
    """Verify every byte, reject extra files, and validate the bound release policy."""
    root = root.resolve()
    if (root / "SHA256SUMS").is_symlink():
        raise ValueError("SHA256SUMS must be a regular file")
    checksums = {}
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
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()
    }
    if actual != set(checksums) | {"SHA256SUMS"} or "release.json" not in checksums:
        raise ValueError("bundle file set disagrees with SHA256SUMS")
    data = json.loads((root / "release.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), dict) for key in ("config", "source", "context", "files")
    ):
        raise ValueError("invalid release manifest structure")
    if data.get("schema_version") != SCHEMA or data.get("files") != {
        k: v for k, v in checksums.items() if k != "release.json"
    }:
        raise ValueError("release manifest disagrees with checksums/schema")
    allowed_files = {
        "dist/" + name
        for name in validate_artifacts(
            root / "dist", data["distribution"], data["config"]["version"]
        )
    }
    if data["config"].get("changelog") is not False:
        allowed_files.add("notes.md")
    if set(data["files"]) != allowed_files:
        raise ValueError("unexpected release bundle member")
    config = validate(Config(**data["config"]))
    if (
        not isinstance(data["source"].get("dirty"), bool)
        or data.get("distribution") != config.distribution
        or data["source"].get("tag") != "v" + config.version
        or not re.fullmatch(r"[0-9a-f]{40,64}", data["source"].get("commit", ""))
    ):
        raise ValueError("invalid release identity")
    hashes = validate_artifacts(root / "dist", config.distribution, config.version)
    if {"dist/" + name: digest for name, digest in hashes.items()} != {
        k: v for k, v in data["files"].items() if k.startswith("dist/")
    }:
        raise ValueError("distribution hashes disagree with manifest")
    if (config.changelog is not False) != ("notes.md" in data["files"]):
        raise ValueError("release notes disagree with configured policy")
    return data, config
