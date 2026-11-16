"""Actual isolated builds and bundle tampering checks, without upload mocks."""

from dataclasses import replace
from pathlib import Path
import json
import subprocess
import sys

import pytest

from nopt.bundle import make_bundle, read_bundle, sha256, validate_artifacts
from nopt.changelog import release_notes
from nopt.config import load
from nopt.source import Context, identity
from nopt.cli import check_bundle_context, context, main, parser


@pytest.mark.parametrize("backend", ["setuptools", "hatchling"])
@pytest.mark.parametrize(
    "suffix,target", [(False, "pypi"), (False, "testpypi"), (True, "testpypi")]
)
def test_checkout_build_and_bundle(
    project: Path, tmp_path: Path, suffix: bool, target: str, backend: str
) -> None:
    """Build in the checkout and resolve publishing identity from original configuration."""
    ### Configure the real backend and effective index-facing name.
    if backend == "hatchling":
        path = project / "pyproject.toml"
        text = (
            path.read_text(encoding="utf-8")
            .replace('requires = ["setuptools>=80"]', 'requires = ["hatchling>=1.27"]')
            .replace('build-backend = "setuptools.build_meta"', 'build-backend = "hatchling.build"')
        )
        text += '\n[tool.hatch.build.targets.wheel]\npackages = ["src/fixture_package"]\n'
        path.write_text(text, encoding="utf-8")
    config = replace(load(project), repository=target)
    if suffix:
        config = replace(config, repository="testpypi", testpypi_username="alice")
    context = Context(True)
    source = identity(project, config, context)
    ### Build from the checked checkout and verify exact restoration.
    original_bytes = (project / "pyproject.toml").read_bytes()
    original_pyproject = original_bytes.decode("utf-8")
    destination = tmp_path / "bundle"
    data = make_bundle(
        project, destination, config, context, source, release_notes(project, config)
    )
    assert (project / "pyproject.toml").read_bytes() == original_bytes
    # Ordinary setuptools build outputs are created in the actual checkout.
    if backend == "setuptools":
        assert (
            project
            / "src"
            / ("fixture_package_alice.egg-info" if suffix else "fixture_package.egg-info")
        ).is_dir()
    ### A later command derives the same identity from original configuration.
    verified, resolved = read_bundle(destination)
    assert verified == data
    assert resolved.distribution == config.distribution
    assert resolved.name == "fixture-package"
    # A later command uses a fresh checkout's original project name/configuration.
    fresh = tmp_path / "publishing-checkout"
    fresh.mkdir()
    (fresh / "pyproject.toml").write_text(original_pyproject, encoding="utf-8")
    fresh_config = load(
        fresh, {"repository": "testpypi", "testpypi_username": "alice"} if suffix else {}
    )
    assert fresh_config.distribution == resolved.distribution
    wheel = next((destination / "dist").glob("*.whl"))
    # Import actual built Python from the wheel, independently of the source tree.
    ### Observe the built import as JSON, then check bundle tampering failures.
    result = subprocess.check_output(
        [
            sys.executable,
            "-I",
            "-c",
            'import json, sys; sys.path.insert(0, sys.argv[1]); import fixture_package; print(json.dumps({"value": fixture_package.VALUE}))',
            str(wheel),
        ],
        text=True,
    )
    observation: object = json.loads(result)
    assert isinstance(observation, dict)
    assert observation["value"] == "original"
    unexpected = destination / "extra.txt"
    unexpected.write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="file set"):
        read_bundle(destination)
    unexpected.unlink()
    (destination / "notes.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        read_bundle(destination)
    return


def test_context_binding() -> None:
    """Reject a bundle originating from a different workflow attempt."""
    data = {
        "context": {
            "local": False,
            "github_repository": "owner/repo",
            "run_id": "1",
            "run_attempt": "1",
        }
    }
    check_bundle_context(
        data, Context(False, github_repository="owner/repo", run_id="1", run_attempt="1")
    )
    with pytest.raises(ValueError, match="run_attempt"):
        check_bundle_context(
            data, Context(False, github_repository="owner/repo", run_id="1", run_attempt="2")
        )
    return


def test_template_refuses_overwrite(tmp_path: Path) -> None:
    """Generate a workflow template without replacing an existing file."""
    path = tmp_path / "release.yml"
    assert main(["workflow-template", "--output", str(path)]) == 0
    assert "<your CI goes here>" in path.read_text(encoding="utf-8")
    assert main(["workflow-template", "--output", str(path)]) == 1
    return


def test_cli_requires_explicit_local_mode(project: Path) -> None:
    """Require explicit local mode outside a GitHub Actions release job."""
    assert main(["ci-preflight", "--project", str(project)]) == 1
    return


@pytest.mark.parametrize(
    "entry", ["../outside", "/absolute", "dist/../../outside", "dist\\outside"]
)
def test_bundle_rejects_unsafe_paths(tmp_path: Path, entry: str) -> None:
    """Reject escaping or platform-ambiguous paths in bundle checksums."""
    (tmp_path / "SHA256SUMS").write_text("a" * 64 + "  " + entry + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        read_bundle(tmp_path)
    return


def test_artifact_set_and_native_wheel_rejected(tmp_path: Path) -> None:
    """Reject incomplete artifact sets and native wheels in the pure-Python release flow."""
    with pytest.raises(ValueError, match="exactly"):
        validate_artifacts(tmp_path, "fixture", "1.2.3")
    (tmp_path / "fixture-1.2.3-cp313-cp313-linux_x86_64.whl").touch()
    (tmp_path / "fixture-1.2.3.tar.gz").touch()
    with pytest.raises(ValueError, match="pure-Python"):
        validate_artifacts(tmp_path, "fixture", "1.2.3")
    return


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("config", []),
        ("source", None),
        ("source", {"commit": None, "tag": "v1.2.3.dev4", "dirty": False}),
        ("source", {"commit": "a" * 41, "tag": "v1.2.3.dev4", "dirty": False}),
        ("context", {"local": "false"}),
    ],
)
def test_bundle_rejects_malformed_metadata(
    project: Path, tmp_path: Path, field: str, value: object
) -> None:
    """Reject malformed manifest fields after checksum verification without attribute errors."""
    ### Build a valid bundle, then alter metadata while keeping its checksum coherent.
    config = load(project)
    bundle = tmp_path / "metadata-bundle"
    context = Context(True)
    make_bundle(
        project,
        bundle,
        config,
        context,
        identity(project, config, context),
        release_notes(project, config),
    )
    manifest_path = bundle / "release.json"
    manifest: dict[str, object] = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checksum_path = bundle / "SHA256SUMS"
    lines = checksum_path.read_text(encoding="utf-8").splitlines()
    checksum_path.write_text(
        "\n".join(
            f"{sha256(manifest_path)}  release.json" if line.endswith("  release.json") else line
            for line in lines
        )
        + "\n",
        encoding="utf-8",
    )
    ### The invalid structure must fail at the metadata boundary.
    with pytest.raises(ValueError):
        read_bundle(bundle)
    return


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_failed_renamed_build_restores_original(
    project: Path, tmp_path: Path, local: bool, newline: bytes
) -> None:
    """Restore exact pre-existing edits after a real backend observes the renamed name and fails."""
    ### Configure a backend that records the name before raising from its build hook.
    backend = '''"""Intentional failure fixture for temporary-name cleanup."""
from pathlib import Path
from typing import NoReturn
import json
import tomllib


def build_sdist(sdist_directory: str, config_settings: dict[str, object] | None = None) -> NoReturn:
    """Record build-time configuration then fail without producing a distribution."""
    document = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    Path("observed-name.json").write_text(json.dumps({"name": document["project"]["name"]}), encoding="utf-8")
    raise RuntimeError("intentional backend failure")
'''
    (project / "failure_backend.py").write_text(backend, encoding="utf-8")
    path = project / "pyproject.toml"
    text = (
        path.read_text(encoding="utf-8")
        .replace('requires = ["setuptools>=80"]', 'requires = []\nbackend-path = ["."]')
        .replace('build-backend = "setuptools.build_meta"', 'build-backend = "failure_backend"')
    )
    original = (
        ("# Preserve this uncommitted comment\n" + text + "\n# Keep my local changes\n")
        .encode("utf-8")
        .replace(b"\n", newline)
    )
    path.write_bytes(original)
    config = replace(load(project), repository="testpypi", testpypi_username="alice")
    context = Context(local)
    destination = tmp_path / "failed-bundle"
    ### The original bytes must be restored even though the real build process fails.
    with pytest.raises(subprocess.CalledProcessError):
        make_bundle(
            project,
            destination,
            config,
            context,
            identity(project, config, context),
            release_notes(project, config),
        )
    observed: object = json.loads((project / "observed-name.json").read_text(encoding="utf-8"))
    assert isinstance(observed, dict)
    assert observed["name"] == "fixture-package-alice"
    assert path.read_bytes() == original
    assert not destination.exists()
    return


@pytest.mark.parametrize("explicit_tag", [None, "v1.2.3.dev4", "v9.9.9"])
def test_gha_tag_comes_only_from_environment(
    monkeypatch: pytest.MonkeyPatch, explicit_tag: str | None
) -> None:
    """Use GITHUB_REF in GHA and reject even matching explicit local-only tags."""
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_REF": "refs/tags/v1.2.3.dev4",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, value)
    arguments = ["ci-preflight"]
    if explicit_tag is not None:
        arguments.extend(["--tag", explicit_tag])
    args = parser().parse_args(arguments)
    if explicit_tag is None:
        assert context(args).tag == "v1.2.3.dev4"
    else:
        with pytest.raises(ValueError, match="local-only"):
            context(args)
    return
