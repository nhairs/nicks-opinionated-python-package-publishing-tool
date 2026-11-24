"""Actual isolated builds and bundle tampering checks, without upload mocks."""

from dataclasses import replace
from pathlib import Path
import json
from importlib.metadata import version
import io
import tarfile
import zipfile
import subprocess
import sys

import pytest

from nopt.bundle import (
    build_release_bundle,
    read_and_verify_release_bundle,
    sha256,
    validate_artifacts,
)
from nopt.changelog import release_notes
from nopt.config import load
from nopt.source import Context, identity
from nopt.cli import check_bundle_context, check_publish_bundle, build_context, main, build_parser


@pytest.mark.parametrize("backend", ["setuptools", "hatchling"])
@pytest.mark.parametrize("suffix,target", [(False, "pypi"), (True, "testpypi"), (True, "pypi")])
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
        config = replace(config, testpypi_username="alice")
    rename_expected = target == "testpypi" and suffix
    context = Context(True)
    source = identity(project, config, context)
    ### Build from the checked checkout and verify exact restoration.
    original_bytes = (project / "pyproject.toml").read_bytes()
    original_pyproject = original_bytes.decode("utf-8")
    destination = tmp_path / "bundle"
    data = build_release_bundle(
        project, destination, config, context, source, release_notes(project, config)
    )
    assert (project / "pyproject.toml").read_bytes() == original_bytes
    assert (destination / "pyproject.toml").read_bytes() == original_bytes
    assert (destination / "release.json.sha256sum").read_bytes() == (
        f"{sha256(destination / 'release.json')}  release.json\n".encode("ascii")
    )
    assert not (destination / "SHA256SUMS").exists()
    # Ordinary setuptools build outputs are created in the actual checkout.
    if backend == "setuptools":
        assert (
            project
            / "src"
            / ("fixture_package_alice.egg-info" if rename_expected else "fixture_package.egg-info")
        ).is_dir()
    ### A later command derives the same identity from original configuration.
    policy = {"repository": target, **({"testpypi_username": "alice"} if suffix else {})}
    if rename_expected:
        with pytest.raises(ValueError, match="invocation configuration"):
            read_and_verify_release_bundle(destination)
    verified, resolved = read_and_verify_release_bundle(destination, policy)
    assert verified == data
    assert resolved.distribution_name == config.distribution_name
    assert resolved.name == "fixture-package"
    # A later command uses a fresh checkout's original project name/configuration.
    fresh = tmp_path / "publishing-checkout"
    fresh.mkdir()
    (fresh / "pyproject.toml").write_text(original_pyproject, encoding="utf-8")
    fresh_config = load(fresh, policy)
    assert fresh_config.distribution_name == resolved.distribution_name
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
    with pytest.raises(ValueError, match="unexpected=.*extra.txt"):
        read_and_verify_release_bundle(destination, policy)
    unexpected.unlink()
    (destination / "release-notes.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        read_and_verify_release_bundle(destination, policy)
    (destination / "release-notes.md").unlink()
    with pytest.raises(ValueError, match="missing=.*release-notes.md"):
        read_and_verify_release_bundle(destination, policy)
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
    assert path.read_text(encoding="utf-8").strip()
    with pytest.raises(FileExistsError):
        main(["workflow-template", "--output", str(path)])
    return


def test_cli_requires_explicit_local_mode(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require explicit local mode outside a GitHub Actions release job."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError, match="--local"):
        main(["ci-preflight", "--project", str(project)])
    return


def write_test_manifest(bundle: Path, contents: bytes) -> None:
    """Write standalone manifest bytes with a matching checksum."""
    manifest = bundle / "release.json"
    manifest.write_bytes(contents)
    (bundle / "release.json.sha256sum").write_bytes(
        f"{sha256(manifest)}  release.json\n".encode("ascii")
    )
    return


@pytest.mark.parametrize(
    "contents",
    [b"", b"a" * 64 + b"  other.json\n", (b"a" * 64 + b"  release.json\n") * 2],
)
def test_bundle_rejects_malformed_sidecar(tmp_path: Path, contents: bytes) -> None:
    """Require exactly one checksum line for the fixed manifest name."""
    (tmp_path / "release.json.sha256sum").write_bytes(contents)
    with pytest.raises(ValueError, match=r"invalid release\.json\.sha256sum"):
        read_and_verify_release_bundle(tmp_path)
    return


def test_bundle_requires_manifest_sidecar(tmp_path: Path) -> None:
    """Direct readers of older bundles to the nopt version that built them."""
    write_test_manifest(tmp_path, b"{}")
    (tmp_path / "release.json.sha256sum").rename(tmp_path / "SHA256SUMS")
    with pytest.raises(ValueError, match=r"missing release\.json\.sha256sum.*version that built"):
        read_and_verify_release_bundle(tmp_path)
    return


def test_bundle_checks_manifest_bytes_before_decoding(tmp_path: Path) -> None:
    """Report tampering before attempting to decode unusable manifest bytes."""
    write_test_manifest(tmp_path, b"{}")
    (tmp_path / "release.json").write_bytes(b"invalid json")
    with pytest.raises(ValueError, match=r"checksum mismatch: release\.json"):
        read_and_verify_release_bundle(tmp_path)
    return


@pytest.mark.parametrize(
    "contents,error",
    [
        (b"invalid json", "Expecting value"),
        (b'{"schema_version": 1, "files": []}', "files must be an object"),
        (
            b'{"schema_version": 1, "files": {"pyproject.toml": false}}',
            "invalid release bundle checksum: pyproject.toml",
        ),
        (
            b'{"schema_version": 1, "files": {"pyproject.toml": "", "pyproject.toml": ""}}',
            "duplicate key",
        ),
    ],
)
def test_bundle_rejects_invalid_inventory(tmp_path: Path, contents: bytes, error: str) -> None:
    """Reject invalid manifests even when their checksum matches."""
    write_test_manifest(tmp_path, contents)
    with pytest.raises(ValueError, match=error):
        read_and_verify_release_bundle(tmp_path)
    return


@pytest.mark.parametrize(
    "entry", ["../outside", "/absolute", "dist/../../outside", "dist\\outside", "C:outside"]
)
def test_bundle_rejects_unsafe_paths(tmp_path: Path, entry: str) -> None:
    """Reject escaping or platform-ambiguous paths in the manifest."""
    manifest = {"schema_version": 1, "files": {entry: "a" * 64}}
    write_test_manifest(tmp_path, json.dumps(manifest).encode("utf-8"))
    with pytest.raises(ValueError, match="unsafe"):
        read_and_verify_release_bundle(tmp_path)
    return


def test_artifact_set_requires_wheel_and_sdist(tmp_path: Path) -> None:
    """Reject incomplete artifact sets before reading archive metadata."""
    with pytest.raises(ValueError, match="exactly"):
        validate_artifacts(tmp_path, "fixture", "1.2.3")
    return


@pytest.mark.parametrize("wheel_tag", ["py3-none-any", "cp313-cp313-linux_x86_64"])
def test_artifact_metadata_for_pure_and_platform_wheels(tmp_path: Path, wheel_tag: str) -> None:
    """Verify archive identity equally for platform and pure-Python wheel tags."""
    metadata = b"Metadata-Version: 2.1\nName: fixture\nVersion: 1.2.3\n"
    wheel = tmp_path / f"fixture-1.2.3-{wheel_tag}.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fixture-1.2.3.dist-info/METADATA", metadata)
    sdist = tmp_path / "fixture-1.2.3.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo("fixture-1.2.3/PKG-INFO")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))
    assert validate_artifacts(tmp_path, "fixture", "1.2.3") == {
        wheel.name: sha256(wheel),
        sdist.name: sha256(sdist),
    }
    with pytest.raises(ValueError, match="filename disagrees"):
        validate_artifacts(tmp_path, "fixture", "9.9.9")
    return


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("distribution_name", []),
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
    build_release_bundle(
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
    write_test_manifest(bundle, json.dumps(manifest).encode("utf-8"))
    ### The invalid structure must fail at the metadata boundary.
    with pytest.raises(ValueError):
        read_and_verify_release_bundle(bundle)
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
from importlib.metadata import version
import io
import tarfile
import zipfile
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
        build_release_bundle(
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
    args = build_parser().parse_args(arguments)
    if explicit_tag is None:
        assert build_context(args).tag == "v1.2.3.dev4"
    else:
        with pytest.raises(ValueError, match="local-only"):
            build_context(args)
    return


@pytest.mark.parametrize("with_notes", [False, True])
def test_publish_bundle_needs_no_checkout(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_notes: bool
) -> None:
    """Consume original inputs from the bundle alone and reject changed workflow identity."""
    if not with_notes:
        with (project / "pyproject.toml").open("a", encoding="utf-8") as stream:
            stream.write("changelog = false\n")
    config = load(project)
    execution = Context(False, "v1.2.3.dev4", "owner/repo", "123", "1")
    source = identity(project, config, Context(True))
    original = (project / "pyproject.toml").read_bytes()
    bundle = tmp_path / "bundle"
    build_release_bundle(
        project,
        bundle,
        replace(config, github_release_publish=False),
        execution,
        source,
        release_notes(project, config),
    )
    project.rename(tmp_path / "unused-source")
    empty = tmp_path / "publishing-job"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert not (empty / "pyproject.toml").exists()
    assert not (empty / ".git").exists()
    data, bundled_config = check_publish_bundle(bundle, execution, source["commit"])
    assert data["source"] == source
    assert bundled_config == config
    assert bundled_config.github_release_publish is True
    assert "config" not in data
    _, per_step = check_publish_bundle(
        bundle, execution, source["commit"], {"github_release_publish": False}
    )
    assert per_step.github_release_publish is False
    assert check_publish_bundle(bundle, execution, source["commit"])[1] == config
    assert (bundle / "pyproject.toml").read_bytes() == original
    assert (bundle / "release-notes.md").exists() is with_notes
    assert not (bundle / "notes.md").exists()
    with pytest.raises(ValueError, match="current commit"):
        check_publish_bundle(bundle, execution, "0" * 40)
    with pytest.raises(ValueError, match="current tag"):
        check_publish_bundle(bundle, replace(execution, tag="v9.9.9"), source["commit"])
    with pytest.raises(ValueError, match="run_id"):
        check_publish_bundle(bundle, replace(execution, run_id="456"), source["commit"])
    (bundle / "pyproject.toml").write_bytes(original + b"# edited\n")
    with pytest.raises(ValueError, match="checksum"):
        check_publish_bundle(bundle, execution, source["commit"])
    return


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_cli_information_needs_no_project(tmp_path: Path, option: str) -> None:
    """Expose program identity without requiring checkout, bundle or GHA context."""
    result = subprocess.run(
        [sys.executable, "-m", "nopt", option],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    if option == "--help":
        assert "Nicks Opinionated" in result.stdout
        assert "Copyright (c) 2026 Nicholas Hairs" in result.stdout
        assert "github.com/nhairs/nicks-opinionated-python-package-publishing-tool" in result.stdout
    else:
        assert result.stdout.strip() == f"nopt {version('nopt')}"
    return
