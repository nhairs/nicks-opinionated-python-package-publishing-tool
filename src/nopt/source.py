"""Read release identity and inspect real setuptools source selections off-tree."""

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from .config import Config


@dataclass(frozen=True)
class Context:
    """Inputs supplied by the caller rather than guessed from another run."""

    local: bool
    tag: str | None = None
    github_repository: str | None = None
    run_id: str | None = None
    run_attempt: str | None = None


def git(root: Path, *args: str) -> str:
    """Run a read-only Git query."""
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, stderr=subprocess.PIPE, timeout=60
    ).strip()


def paths(root: Path, *args: str) -> set[str]:
    """Read NUL-separated paths without losing spaces or non-ASCII names."""
    result = subprocess.check_output(["git", *args, "-z"], cwd=root, timeout=60)
    return {os.fsdecode(item) for item in result.split(b"\0") if item}


def working_paths(root: Path) -> set[str]:
    """List tracked and nonignored untracked source files."""
    return paths(root, "ls-files", "--cached", "--others", "--exclude-standard")


# Disposable tooling/output directories are not supported distribution inputs.
# Tracked files are never excluded by this rule.
CACHE_PARTS = {
    ".git",
    ".venv",
    "venv",
    ".tox",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    "release-bundle",
    "node_modules",
}


def source_paths(root: Path) -> set[str]:
    """Include ignored source data too, while excluding disposable tool caches."""
    tracked = paths(root, "ls-files", "--cached")
    visible = working_paths(root)
    ignored = paths(root, "ls-files", "--others", "--ignored", "--exclude-standard")
    return (
        tracked
        | visible
        | {
            name
            for name in ignored
            if not any(
                part in CACHE_PARTS or part.endswith(".egg-info") for part in Path(name).parts
            )
        }
    )


def snapshot(root: Path) -> dict[str, str]:
    """Fingerprint working inputs to detect source mutation across a build."""
    result = {}
    for name in sorted(source_paths(root)):
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"source input escapes checkout: {name}")
        if path.is_symlink():
            result[name] = "symlink:" + os.readlink(path)
        elif path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def copy_working(root: Path, destination: Path, with_git: bool = False) -> None:
    """Copy working sources including ignored data, excluding disposable tool caches."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in source_paths(root):
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"source input escapes checkout: {name}")
        if not path.is_file():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    if with_git:
        (destination / ".git").write_text(
            f"gitdir: {git(root, 'rev-parse', '--absolute-git-dir')}\n", encoding="utf-8"
        )


def manifest(root: Path) -> set[str]:
    """Obtain a fresh real setuptools manifest in a disposable source copy.

    Install the project's declared build dependencies in an isolated environment.
    Custom hook failures abort rather than approximating their selection.
    """
    with tempfile.TemporaryDirectory() as temporary:
        egg_base = Path(temporary)
        from build import ProjectBuilder  # pylint: disable=import-outside-toplevel
        from build.env import DefaultIsolatedEnv  # pylint: disable=import-outside-toplevel

        with DefaultIsolatedEnv(installer="uv") as environment:
            builder = ProjectBuilder.from_isolated_env(environment, root)
            environment.install(builder.build_system_requires)
            environment.install(builder.get_requires_for_build("sdist"))
            script = (
                "import pathlib, runpy; from setuptools import setup; "
                "runpy.run_path('setup.py', run_name='__main__') "
                "if pathlib.Path('setup.py').is_file() else setup()"
            )
            subprocess.run(
                [
                    environment.python_executable,
                    "-c",
                    script,
                    "egg_info",
                    "--egg-base",
                    str(egg_base),
                ],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=120,
            )
        manifests = list(egg_base.glob("*.egg-info/SOURCES.txt"))
        if len(manifests) != 1:
            raise ValueError("expected exactly one setuptools source manifest")
        selected = set(manifests[0].read_text(encoding="utf-8").splitlines())
        result = set()
        for name in selected:
            if ".egg-info/" in name:
                continue
            if not (root / name).resolve().is_relative_to(root):
                raise ValueError(f"selected source input escapes project: {name}")
            if (root / name).is_file():
                result.add(name)
        return result


def selected_inputs(root: Path) -> set[str]:
    """Union current and HEAD manifests using current build selection controls."""
    names = source_paths(root)
    if any("\n" in name or "\r" in name for name in names):
        raise ValueError("newline source filenames cannot be represented in setuptools manifests")
    if git(root, "rev-parse", "--show-prefix"):
        raise ValueError("initial source adapter requires the project at the Git repository root")
    with tempfile.TemporaryDirectory() as temporary:
        current, head = Path(temporary) / "current", Path(temporary) / "head"
        copy_working(root, current, with_git=True)
        head.mkdir()
        archive = subprocess.check_output(["git", "archive", "HEAD"], cwd=root, timeout=60)
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(head, filter="data")
        # Selection uses the same current configuration for both file inventories.
        controls = {"pyproject.toml", "MANIFEST.in", "setup.py", "setup.cfg"}
        controls |= {name for name in names if Path(name).name == ".gitignore"}
        for name in controls:
            target = head / name
            if (root / name).is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / name, target)
            elif target.is_file():
                target.unlink()
        return manifest(current) | manifest(head)


def check_branches(root: Path, config: Config, context: Context, commit: str) -> None:
    """Require ancestry from any existing allowed branch, never branch-tip equality."""
    branches = getattr(config, f"{config.group}_release_branches")
    if branches is False:
        return
    for branch in branches:
        ref = f"refs/heads/{branch}" if context.local else f"refs/remotes/origin/{branch}"
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", ref], cwd=root, check=False
        )
        if exists.returncode == 1:
            continue
        if exists.returncode != 0:
            raise ValueError(f"unable to resolve branch {ref}")
        status = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, ref], cwd=root, check=False
        )
        if status.returncode == 0:
            return
        if status.returncode != 1:
            raise ValueError(f"unable to establish ancestry for {ref}")
    raise ValueError("release commit is not reachable from any existing allowed branch")


def identity(root: Path, config: Config, context: Context) -> dict:
    """Verify tag/branch/dirty policy and return source identity."""
    commit = git(root, "rev-parse", "HEAD")
    expected = "v" + config.version
    tag = context.tag or expected
    if tag != expected:
        raise ValueError(f"expected canonical tag {expected}, got {tag}")
    if git(root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != commit:
        raise ValueError("release tag does not point to HEAD")
    check_branches(root, config, context, commit)
    selected = selected_inputs(root)
    changed = paths(root, "diff", "--name-only", "--relative", "HEAD")
    changed |= paths(root, "ls-files", "--others", "--exclude-standard")
    dirty = bool(selected & changed)
    if dirty and config.group not in config.allow_dirty:
        raise ValueError(f"selected source is dirty; {config.group} is not in allow-dirty")
    inputs = {}
    for name in sorted(selected):
        path = root / name
        inputs[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return {"commit": commit, "tag": tag, "dirty": dirty, "inputs_sha256": digest}
