"""Real Git refs and working-tree status checks."""

from dataclasses import replace
import subprocess

import pytest

from nopt.config import load
from nopt.source import Context, identity
from tests.helpers import inventory


def test_detached_tag_and_dirty_status(project):
    subprocess.run(["git", "checkout", "--detach"], cwd=project, check=True, capture_output=True)
    config = load(project)
    (project / "ignored.txt").write_text("ignored", encoding="utf-8")
    before = inventory(project)
    source = identity(project, config, Context(True))
    assert not source["dirty"]
    assert inventory(project) == before
    package = project / "src" / "fixture_package"
    (package / "data.txt").unlink()
    assert identity(project, config, Context(True))["dirty"]
    with pytest.raises(ValueError, match="dirty"):
        identity(project, replace(config, allow_dirty=()), Context(True))


def test_untracked_changes(project):
    config = load(project)
    (project / "src" / "fixture_package" / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert identity(project, config, Context(True))["dirty"]


def test_local_and_remote_branch_rules(project):
    config = replace(load(project), dev_release_branches=("main",))
    original = identity(project, config, Context(True))["commit"]
    # A newer main tip must still permit the older tagged commit.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Later tip"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "--detach", original], cwd=project, check=True, capture_output=True
    )
    assert identity(project, config, Context(True))["commit"] == original
    with pytest.raises(ValueError, match="reachable"):
        identity(project, config, Context(False))
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=project, check=True
    )
    assert identity(project, config, Context(False))["commit"] == original


def test_moved_tag_rejected(project):
    config = load(project)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Other commit"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="HEAD"):
        identity(project, config, Context(True))


def test_any_allowed_branch_can_satisfy_policy(project):
    config = replace(load(project), dev_release_branches=("missing", "main"))
    assert identity(project, config, Context(True))["dirty"] is False


def test_root_changes_count_even_when_not_packaged(project):
    config = load(project)
    (project / "excluded.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty"):
        identity(project, replace(config, allow_dirty=()), Context(True))


@pytest.mark.parametrize("change", ["staged", "untracked", "deleted", "renamed"])
def test_git_status_changes(project, change):
    path = project / "excluded.txt"
    if change == "deleted":
        path.unlink()
    elif change == "renamed":
        subprocess.run(["git", "mv", "excluded.txt", "renamed.txt"], cwd=project, check=True)
    elif change == "untracked":
        (project / "untracked.txt").write_text("new", encoding="utf-8")
    else:
        path.write_text("staged", encoding="utf-8")
        subprocess.run(["git", "add", "excluded.txt"], cwd=project, check=True)
    assert identity(project, load(project), Context(True))["dirty"]


def test_worktree_respects_gitignore_without_directory_guesses(project, tmp_path):
    worktree = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    (worktree / "ignored.txt").write_text("ignored", encoding="utf-8")
    assert not identity(worktree, load(worktree), Context(True))["dirty"]
    (worktree / "dist").mkdir()
    (worktree / "dist" / "file.whl").write_text("not ignored", encoding="utf-8")
    assert identity(worktree, load(worktree), Context(True))["dirty"]
