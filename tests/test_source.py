"""Real Git refs and real manifest-based dirty selection."""

from dataclasses import replace
import subprocess
import os

import pytest

from nopt.config import load
from nopt.source import Context, copy_working, identity, manifest
from conftest import inventory


def test_detached_tag_and_dirty_selection(project):
    subprocess.run(["git", "checkout", "--detach"], cwd=project, check=True, capture_output=True)
    config = load(project)
    (project / "ignored.txt").write_text("ignored", encoding="utf-8")
    (project / "excluded.txt").write_text("excluded changed", encoding="utf-8")
    before = inventory(project)
    source = identity(project, config, Context(True))
    assert not source["dirty"]
    assert inventory(project) == before
    package = project / "src" / "fixture_package"
    (package / "data.txt").unlink()
    assert identity(project, config, Context(True))["dirty"]
    with pytest.raises(ValueError, match="dirty"):
        identity(project, replace(config, allow_dirty=()), Context(True))


def test_untracked_selected_and_deleted(project):
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


def test_dirty_changes_are_bound_to_bundle_identity(project):
    config = load(project)
    path = project / "src" / "fixture_package" / "__init__.py"
    path.write_text('VALUE = "first"\n', encoding="utf-8")
    first = identity(project, config, Context(True))
    path.write_text('VALUE = "second"\n', encoding="utf-8")
    second = identity(project, config, Context(True))
    assert first["dirty"] is second["dirty"] is True
    assert first["inputs_sha256"] != second["inputs_sha256"]


def test_git_worktree_and_stale_manifest(project, tmp_path):
    worktree = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    cache = worktree / "src" / "fixture_package.egg-info"
    cache.mkdir()
    (cache / "SOURCES.txt").write_text("excluded.txt\n", encoding="utf-8")
    (worktree / "excluded.txt").write_text("changed but excluded", encoding="utf-8")
    before = inventory(worktree)
    assert not identity(worktree, load(worktree), Context(True))["dirty"]
    assert inventory(worktree) == before


@pytest.mark.skipif(
    os.name == "nt", reason="Windows hosted runners cannot create directory symlinks"
)
def test_manifest_resolves_aliased_temp_root(project, tmp_path):
    staged = tmp_path / "real-stage"
    copy_working(project, staged)
    alias = tmp_path / "stage-alias"
    alias.symlink_to(staged, target_is_directory=True)
    assert "src/fixture_package/__init__.py" in manifest(alias)
