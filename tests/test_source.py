"""Real Git refs and working-tree status checks."""

from pathlib import Path
from dataclasses import replace
import subprocess

import pytest

from nopt.config import load
from nopt.source import Context, check_branches, git, identity, is_latest_final
from tests.helpers import inventory


def test_detached_tag_and_dirty_status(project: Path) -> None:
    """Permit detached tag checkouts while enforcing ordinary Git dirty policy."""
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
    return


def test_untracked_changes(project: Path) -> None:
    """Count a nonignored untracked file as a dirty checkout."""
    config = load(project)
    (project / "src" / "fixture_package" / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert identity(project, config, Context(True))["dirty"]
    return


@pytest.mark.parametrize("selector", ["main", "ma*", "main*"])
def test_local_and_remote_branch_rules(project: Path, selector: str) -> None:
    """Check local or remote ancestry without requiring the release commit to be the tip."""
    config = replace(load(project), dev_release_branches=(selector,))
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
    return


def test_moved_tag_rejected(project: Path) -> None:
    """Reject a release tag that no longer points to the checkout commit."""
    config = load(project)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Other commit"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="HEAD"):
        identity(project, config, Context(True))
    return


def test_any_allowed_branch_can_satisfy_policy(project: Path) -> None:
    """Accept ancestry through an existing allowed branch despite another missing branch."""
    config = replace(load(project), dev_release_branches=("missing", "release/1.*", "main"))
    assert identity(project, config, Context(True))["dirty"] is False
    return


@pytest.mark.parametrize(
    "ref,symbolic",
    [
        ("refs/remotes/origin/release/1.6/hotfix", False),
        ("refs/remotes/origin/Release/1.6", False),
        ("refs/remotes/origin/release/1.alias", True),
        ("refs/remotes/upstream/release/1.6", False),
        ("refs/tags/release/1.6", False),
        ("refs/pull/release/1.6", False),
    ],
)
def test_pattern_excludes_ineligible_remote_refs(project: Path, ref: str, symbolic: bool) -> None:
    """Exclude nested names, case mismatches, symbolic refs, and other namespaces."""
    args = ("symbolic-ref", ref, "refs/heads/main") if symbolic else ("update-ref", ref, "HEAD")
    subprocess.run(["git", *args], cwd=project, check=True)
    config = replace(load(project), dev_release_branches=("release/1.*",))
    with pytest.raises(ValueError, match="reachable"):
        identity(project, config, Context(False))
    return


@pytest.mark.parametrize("selector", ["H*", "HEAD"])
def test_remote_head_alias_does_not_qualify(project: Path, selector: str) -> None:
    """Require a concrete branch when a policy matches the origin/HEAD alias."""
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=project, check=True
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=project,
        check=True,
    )
    config = replace(load(project), dev_release_branches=(selector,))
    with pytest.raises(ValueError, match="reachable"):
        identity(project, config, Context(False))
    return


def test_pattern_requires_ancestry_in_selected_namespace(project: Path) -> None:
    """Reject a divergent local branch even when its remote-tracking peer qualifies."""
    divergent = subprocess.check_output(
        ["git", "commit-tree", "HEAD^{tree}", "-m", "Unrelated root"], cwd=project, text=True
    ).strip()
    subprocess.run(["git", "branch", "release/1.6", divergent], cwd=project, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/release/1.6", "HEAD"], cwd=project, check=True
    )
    config = replace(load(project), dev_release_branches=("release/1.*",))
    with pytest.raises(ValueError, match="reachable"):
        identity(project, config, Context(True))
    assert identity(project, config, Context(False))["commit"] == git(project, "rev-parse", "HEAD")
    return


def test_branch_query_error(project: Path, tmp_path: Path) -> None:
    """Surface a failed Git containment query independently of ordinary non-matches."""
    config = replace(load(project), dev_release_branches=("release/1.*",))
    commit = git(project, "rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError, match="for-each-ref"):
        check_branches(tmp_path, config, Context(False), commit)
    return


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("HEAD:README.md", id="blob"),
        pytest.param("refs/tags/annotation", id="annotated-tag"),
    ],
)
def test_non_commit_remote_ref_does_not_qualify(project: Path, target: str) -> None:
    """Ignore blob refs and annotated tags stored under the remote-tracking namespace."""
    subprocess.run(
        ["git", "tag", "-a", "annotation", "-m", "Tag on release commit"], cwd=project, check=True
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/release/1.bad", target], cwd=project, check=True
    )
    config = replace(load(project), dev_release_branches=("release/1.*",))
    with pytest.raises(ValueError, match="reachable"):
        identity(project, config, Context(False))
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/release/1.good", "HEAD"],
        cwd=project,
        check=True,
    )
    assert identity(project, config, Context(False))["commit"] == git(project, "rev-parse", "HEAD")
    return


def test_root_changes_count_even_when_not_packaged(project: Path) -> None:
    """Count tracked root changes regardless of backend packaging selection."""
    config = load(project)
    (project / "excluded.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty"):
        identity(project, replace(config, allow_dirty=()), Context(True))
    return


@pytest.mark.parametrize("change", ["staged", "untracked", "deleted", "renamed"])
def test_git_status_changes(project: Path, change: str) -> None:
    """Report staged edits, deletions, renames and nonignored additions through Git status."""
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
    return


def test_worktree_respects_gitignore_without_directory_guesses(
    project: Path, tmp_path: Path
) -> None:
    """Respect Git ignore rules in linked worktrees without excluding directory names."""
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
    return


@pytest.mark.parametrize("version", ["1.2.3.dev1", "1.2.3a1", "1.2.3b1", "1.2.3rc1"])
def test_nonfinal_is_never_latest(version: str) -> None:
    """Exclude development and prerelease versions from GitHub latest marking."""
    assert is_latest_final(version, []) is False
    return


def test_latest_final_uses_git_versions_not_dates_or_names(project: Path) -> None:
    """Select the greatest canonical final Git tag, including annotated tags and maintenance versions."""
    tags = [
        "v1.2.3",
        "v1.10.0",
        "v1.9.0",
        "v2.0.0rc1",
        "v2.0.0.dev1",
        "v1.11.0.post1",
        "v01.20.0",
        "v1.2",
        "unrelated",
    ]
    for tag in tags:
        subprocess.run(["git", "tag", tag], cwd=project, check=True)
    subprocess.run(
        ["git", "tag", "-a", "v1.10.1", "-m", "Annotated final"], cwd=project, check=True
    )
    refs = subprocess.check_output(
        ["git", "for-each-ref", "--format=%(refname)", "refs/tags/"], cwd=project, text=True
    ).splitlines()
    assert is_latest_final("1.10.1", refs) is True
    assert is_latest_final("1.10.0", refs) is False
    assert is_latest_final("1.2.3", refs) is False
    return


def test_latest_requires_current_final_tag() -> None:
    """Fail closed when a current final release is absent from the Git reference snapshot."""
    with pytest.raises(ValueError, match="missing"):
        is_latest_final("1.2.3", ["refs/tags/v1.2.2"])
    return
