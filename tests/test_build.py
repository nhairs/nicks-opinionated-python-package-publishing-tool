"""Actual isolated builds and bundle tampering checks, without upload mocks."""

from dataclasses import replace
from pathlib import Path
import os
import subprocess
import sys
import zipfile

import pytest

from nopt.bundle import make_bundle, read_bundle, validate_artifacts
from nopt.changelog import release_notes
from nopt.config import load
from nopt.source import Context, copy_working, identity
from nopt.cli import check_bundle_context, main
from tests.helpers import inventory


@pytest.mark.parametrize("suffix", [False, True])
def test_isolated_build_and_bundle(project, tmp_path, suffix):
    config = load(project)
    if suffix:
        config = replace(config, repository="testpypi", testpypi_username="alice")
    context = Context(True)
    source = identity(project, config, context)
    # Explicitly selected ignored source data is packaged without making Git dirty.
    with (project / ".gitignore").open("a", encoding="utf-8") as stream:
        stream.write("src/fixture_package/ignored-data.txt\n")
    (project / "src" / "fixture_package" / "ignored-data.txt").write_text(
        "ignored data", encoding="utf-8"
    )
    with (project / "MANIFEST.in").open("w", encoding="utf-8") as stream:
        stream.write("include src/fixture_package/ignored-data.txt\n")
    source = identity(project, config, context)
    before = inventory(project)
    destination = tmp_path / "bundle"
    data = make_bundle(
        project, destination, config, context, source, release_notes(project, config)
    )
    assert inventory(project) == before
    verified, resolved = read_bundle(destination)
    assert verified == data
    assert resolved.distribution == config.distribution
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
    with zipfile.ZipFile(wheel) as archive:
        assert "fixture_package/data.txt" in archive.namelist()
        assert "fixture_package/py.typed" in archive.namelist()
        assert archive.read("fixture_package/ignored-data.txt") == b"ignored data"
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


def test_build_with_delivered_metadata_plugin(project, tmp_path):
    """Build a local companion wheel then exercise actual off-tree provenance."""
    supplied = os.environ.get("NOPT_COMPANION_SOURCE")
    if not supplied:
        pytest.skip("set NOPT_COMPANION_SOURCE to run companion integration")
    companion = tmp_path / "companion-source"
    copy_working(Path(supplied).resolve(), companion, with_git=True)
    wheels = tmp_path / "companion-wheels"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheels), str(companion)],
        check=True,
    )
    wheel = next(wheels.glob("*.whl"))
    path = project / "pyproject.toml"
    text = path.read_text(encoding="utf-8").replace(
        'requires = ["setuptools>=80"]',
        f'requires = ["setuptools>=80", "setuptools-git-metadata @ {wheel.as_uri()}"]',
    )
    text += "\n[tool.setuptools-git-metadata]\n"
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Configure companion"], cwd=project, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "tag", "-f", "v1.2.3.dev4"], cwd=project, check=True, capture_output=True
    )
    config = load(project)
    context = Context(True)
    source = identity(project, config, context)
    before = inventory(project)
    destination = tmp_path / "bundle-with-metadata"
    make_bundle(project, destination, config, context, source, release_notes(project, config))
    assert inventory(project) == before
    with zipfile.ZipFile(next((destination / "dist").glob("*.whl"))) as archive:
        import json

        metadata = json.loads(archive.read("fixture_package/_git_metadata.json"))
    assert metadata["BUILD_GIT_HASH"] == source["commit"]
    assert metadata["BUILD_GIT_BRANCH"] == "main"
    assert metadata["BUILD_GIT_DIRTY"] is False
    assert metadata["SDIST_BUILD_TIMESTAMP"] is not None


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
