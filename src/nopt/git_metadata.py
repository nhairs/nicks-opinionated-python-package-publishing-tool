"""Verify the Git commit recorded by setuptools-git-metadata in built artifacts."""

from collections import Counter
from collections.abc import Iterable
import json
from pathlib import Path
import stat
import tarfile
import tomllib
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_PLUGIN = "setuptools-git-metadata"
_RESOURCE = "_git_metadata.json"
_MAX_METADATA_BYTES = 64 * 1024


def _configured_packages(settings: dict[str, object]) -> set[str] | None:
    """Return explicit package directories, or None for top-level discovery."""
    if "packages" not in settings:
        return None
    packages = settings["packages"]
    if (
        not isinstance(packages, list)
        or not packages
        or any(
            not isinstance(package, str)
            or not all(part.isidentifier() for part in package.split("."))
            for package in packages
        )
    ):
        raise ValueError(f"{_PLUGIN} packages must be a nonempty list of import package names")
    return {package.replace(".", "/") for package in packages}


def _resources(
    artifact: Path, names: list[str], prefix: str, packages: set[str] | None
) -> dict[str, str]:
    """Find the expected regular package resources independently of metadata presence."""
    if packages is None:
        packages = {
            parts[0]
            for name in names
            if name.startswith(prefix)
            and len(parts := name.removeprefix(prefix).split("/")) == 2
            and parts[0].isidentifier()
            and parts[1] == "__init__.py"
        }
    if not packages:
        raise ValueError(f"{artifact.name}: no top-level packages found for {_PLUGIN} verification")
    counts = Counter(names)
    resources = {package: f"{prefix}{package}/{_RESOURCE}" for package in sorted(packages)}
    for name in resources.values():
        if counts[name] != 1:
            raise ValueError(f"{artifact.name}: expected exactly one {name}, found {counts[name]}")
    return resources


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys before comparing the commit."""
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate Git metadata key")
    return fields


def _verify_commit(artifact: Path, name: str, content: bytes, commit: str) -> None:
    """Require the recorded hash to match the full release commit."""
    location = f"{artifact.name}: {name}"
    try:
        metadata = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"{location}: invalid Git metadata: {error}") from error
    if not isinstance(metadata, dict) or metadata.get("BUILD_GIT_HASH") != commit:
        raise ValueError(f"{location}: BUILD_GIT_HASH does not match release commit {commit}")


def _verify_wheel(path: Path, packages: set[str] | None, commit: str) -> set[str]:
    """List ZIP entries and decompress the selected JSON members into memory."""
    with zipfile.ZipFile(path) as wheel:
        resources = _resources(path, wheel.namelist(), "", packages)
        for name in resources.values():
            member = wheel.getinfo(name)
            if (
                stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG)
                or member.file_size > _MAX_METADATA_BYTES
            ):
                raise ValueError(f"{path.name}: {name} must be a small regular file")
            _verify_commit(path, name, wheel.read(member), commit)
    return set(resources)


def _verify_sdist(path: Path, packages: set[str] | None, commit: str) -> set[str]:
    """Scan tar headers and read the selected JSON members without writing files."""
    with tarfile.open(path) as sdist:
        names = sdist.getnames()
        # The distribution identity check has already verified the single top-level PKG-INFO.
        pkg_info = next(
            name for name in names if len(Path(name).parts) == 2 and name.endswith("/PKG-INFO")
        )
        resources = _resources(path, names, pkg_info.removesuffix("PKG-INFO") + "src/", packages)
        for name in resources.values():
            member = sdist.getmember(name)
            if not member.isfile() or member.size > _MAX_METADATA_BYTES:
                raise ValueError(f"{path.name}: {name} must be a small regular file")
            stream = sdist.extractfile(member)
            if stream is None:
                raise ValueError(f"{path.name}: cannot read {name}")
            with stream:
                _verify_commit(path, name, stream.read(), commit)
    return set(resources)


def verify_git_metadata(
    directory: Path, artifact_names: Iterable[str], pyproject: bytes, commit: str
) -> None:
    """Verify the already validated wheel and sdist for opted-in projects."""
    config = tomllib.loads(pyproject.decode("utf-8"))
    settings = config.get("tool", {}).get(_PLUGIN)
    if not isinstance(settings, dict) or not any(
        requirement.marker is None and canonicalize_name(requirement.name) == _PLUGIN
        for requirement in map(Requirement, config.get("build-system", {}).get("requires", []))
    ):
        return
    packages = _configured_packages(settings)
    artifacts = {Path(name).suffix: directory / name for name in artifact_names}
    wheel, sdist = artifacts[".whl"], artifacts[".gz"]
    if _verify_wheel(wheel, packages, commit) != _verify_sdist(sdist, packages, commit):
        raise ValueError(f"{wheel.name} and {sdist.name}: top-level package sets disagree")
