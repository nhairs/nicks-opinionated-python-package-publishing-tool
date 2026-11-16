"""Actual isolated builds and bundle tampering checks, without upload mocks."""

from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from nopt.bundle import make_bundle, read_bundle, validate_artifacts
from nopt.changelog import release_notes
from nopt.config import load
from nopt.source import Context, identity
from nopt.cli import check_bundle_context, main


@pytest.mark.parametrize("backend", ["setuptools", "hatchling"])
@pytest.mark.parametrize("suffix", [False, True])
def test_checkout_build_and_bundle(project, tmp_path, suffix, backend):
    if backend == "hatchling":
        path = project / "pyproject.toml"
        text = (
            path.read_text(encoding="utf-8")
            .replace('requires = ["setuptools>=80"]', 'requires = ["hatchling>=1.27"]')
            .replace('build-backend = "setuptools.build_meta"', 'build-backend = "hatchling.build"')
        )
        text += '\n[tool.hatch.build.targets.wheel]\npackages = ["src/fixture_package"]\n'
        path.write_text(text, encoding="utf-8")
    config = load(project)
    if suffix:
        config = replace(config, repository="testpypi", testpypi_username="alice")
    context = Context(True)
    source = identity(project, config, context)
    original_pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    destination = tmp_path / "bundle"
    data = make_bundle(
        project, destination, config, context, source, release_notes(project, config)
    )
    current_pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert current_pyproject == (
        original_pyproject.replace('name = "fixture-package"', 'name = "fixture-package-alice"')
        if suffix
        else original_pyproject
    )
    # Ordinary setuptools build outputs are created in the actual checkout.
    if backend == "setuptools":
        assert (
            project
            / "src"
            / ("fixture_package_alice.egg-info" if suffix else "fixture_package.egg-info")
        ).is_dir()
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
    result = subprocess.check_output(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import fixture_package; print(fixture_package.VALUE)",
            str(wheel),
        ],
        text=True,
    )
    assert result.strip() == "original"
    unexpected = destination / "extra.txt"
    unexpected.write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="file set"):
        read_bundle(destination)
    unexpected.unlink()
    (destination / "notes.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        read_bundle(destination)


def test_context_binding():
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


def test_template_refuses_overwrite(tmp_path):
    path = tmp_path / "release.yml"
    assert main(["workflow-template", "--output", str(path)]) == 0
    assert "<your CI goes here>" in path.read_text(encoding="utf-8")
    assert main(["workflow-template", "--output", str(path)]) == 1


def test_cli_requires_explicit_local_mode(project):
    assert main(["ci-preflight", "--project", str(project)]) == 1


@pytest.mark.parametrize(
    "entry", ["../outside", "/absolute", "dist/../../outside", "dist\\outside"]
)
def test_bundle_rejects_unsafe_paths(tmp_path, entry):
    (tmp_path / "SHA256SUMS").write_text("a" * 64 + "  " + entry + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        read_bundle(tmp_path)


def test_artifact_set_and_native_wheel_rejected(tmp_path):
    with pytest.raises(ValueError, match="exactly"):
        validate_artifacts(tmp_path, "fixture", "1.2.3")
    (tmp_path / "fixture-1.2.3-cp313-cp313-linux_x86_64.whl").touch()
    (tmp_path / "fixture-1.2.3.tar.gz").touch()
    with pytest.raises(ValueError, match="pure-Python"):
        validate_artifacts(tmp_path, "fixture", "1.2.3")
