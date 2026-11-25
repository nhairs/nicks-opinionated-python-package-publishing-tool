"""Check build-time commit verification against wheel and sdist members."""

from collections.abc import Callable
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

from nopt.git_metadata import verify_git_metadata

_COMMIT = "a" * 40
_WHEEL = "fixture-1.0-py3-none-any.whl"
_SDIST = "fixture-1.0.tar.gz"
_RESOURCE = "example/_git_metadata.json"
_TOML = b"""
[build-system]
requires = ["setuptools", "Setuptools_Git.Metadata>=0.1"]
[tool.setuptools-git-metadata]
"""


def _metadata(commit: str) -> bytes:
    return json.dumps({"BUILD_GIT_HASH": commit}).encode("utf-8")


@pytest.fixture
def verify(tmp_path: Path) -> Callable[[bytes, str, str, bytes | None], None]:
    """Verify two artifacts with an optional replacement or removal of a single member."""

    def run(pyproject: bytes, artifact: str, member: str, data: bytes | None) -> None:
        files = {
            "example/__init__.py": b"",
            "example/nested/__init__.py": b"",
            "second/__init__.py": b"",
            "standalone.py": b"",
            _RESOURCE: _metadata(_COMMIT),
            "second/_git_metadata.json": _metadata(_COMMIT),
        }
        wheel_files = files.copy()
        sdist_files = files.copy()
        selected = wheel_files if artifact == _WHEEL else sdist_files
        if data is None:
            selected.pop(member, None)
        else:
            selected[member] = data
        with zipfile.ZipFile(tmp_path / _WHEEL, "w") as wheel:
            for name, content in wheel_files.items():
                wheel.writestr(name, content)
        with tarfile.open(tmp_path / _SDIST, "w:gz") as sdist:
            entries = {"fixture-1.0/PKG-INFO": b""}
            entries.update({f"fixture-1.0/src/{name}": body for name, body in sdist_files.items()})
            for name, content in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                sdist.addfile(info, io.BytesIO(content))
        verify_git_metadata(tmp_path, [_WHEEL, _SDIST], pyproject, _COMMIT)

    return run


@pytest.mark.parametrize("artifact", [_WHEEL, _SDIST])
@pytest.mark.parametrize(
    "data,message",
    [
        (None, "expected exactly one"),
        (b"{broken", "invalid Git metadata"),
        (b"\xff", "invalid Git metadata"),
        (b'{"BUILD_GIT_HASH": null}', "does not match release commit"),
        (b"{}", "does not match release commit"),
        (_metadata("b" * 40), "does not match release commit"),
        (_metadata(_COMMIT[:8]), "does not match release commit"),
        (b'{"BUILD_GIT_HASH": null, "BUILD_GIT_HASH": null}', "duplicate Git metadata key"),
    ],
)
def test_each_distribution_requires_the_release_commit(
    verify: Callable[[bytes, str, str, bytes | None], None],
    artifact: str,
    data: bytes | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message) as error:
        verify(_TOML, artifact, _RESOURCE, data)
    assert artifact in str(error.value)
    assert _RESOURCE in str(error.value)


@pytest.mark.parametrize(
    "requirement", [b"Setuptools_Git.Metadata>=0.1", b"setuptools-git-metadata @ https://x/pkg.whl"]
)
def test_discovers_top_level_packages(
    verify: Callable[[bytes, str, str, bytes | None], None], requirement: bytes
) -> None:
    pyproject = _TOML.replace(b"Setuptools_Git.Metadata>=0.1", requirement)
    verify(pyproject, _WHEEL, "example/nested/_git_metadata.json", None)
    with pytest.raises(ValueError, match="expected exactly one second/_git_metadata.json"):
        verify(pyproject, _WHEEL, "second/_git_metadata.json", None)


def test_checks_configured_packages(
    verify: Callable[[bytes, str, str, bytes | None], None],
) -> None:
    pyproject = _TOML + b'packages = ["second"]\n'
    verify(pyproject, _WHEEL, _RESOURCE, None)


@pytest.mark.parametrize(
    "pyproject",
    [
        _TOML.replace(b"[tool.setuptools-git-metadata]", b"[tool.other]"),
        _TOML.replace(b"Setuptools_Git.Metadata>=0.1", b"other-backend"),
        _TOML.replace(
            b"Setuptools_Git.Metadata>=0.1", b"setuptools-git-metadata; python_version >= '3.11'"
        ),
    ],
)
def test_activation_requires_table_and_unconditional_build_requirement(
    verify: Callable[[bytes, str, str, bytes | None], None], pyproject: bytes
) -> None:
    verify(pyproject, _WHEEL, _RESOURCE, None)
