"""Read release identity and Git working-tree status."""

from dataclasses import dataclass
from pathlib import Path
import subprocess

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
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        timeout=60,
    )
    dirty = bool(status)
    if dirty and config.group not in config.allow_dirty:
        raise ValueError(f"working tree is dirty; {config.group} is not in allow-dirty")
    return {"commit": commit, "tag": tag, "dirty": dirty}
