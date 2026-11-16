"""Configuration, version, changelog and index policy boundaries."""

from pathlib import Path
from dataclasses import replace
import tomllib
import pytest

from nopt.changelog import release_notes
from nopt.cli import overrides, parser
from nopt.config import Config, load, validate
from nopt.remote import publication_matches
from nopt.project import rename_distribution


@pytest.mark.parametrize(
    "version,group",
    [
        ("1.2.3", "final"),
        ("1.2.3.dev0", "dev"),
        ("1.2.3a1", "pre"),
        ("1.2.3b1", "pre"),
        ("1.2.3rc1", "pre"),
    ],
)
def test_versions(version: str, group: str) -> None:
    """Accept canonical version forms and classify their release groups."""
    config = validate(Config("fixture", version))
    assert config.group == group
    return


@pytest.mark.parametrize(
    "version",
    [
        "1.2",
        "01.2.3",
        "1.2.3dev1",
        "1.2.3.rc1",
        "1.2.3alpha1",
        "1.2.3a01",
        "1.2.3.post1",
        "1.2.3rc1.dev1",
        "1!1.2.3",
        "1.2.3+local",
        "v1.2.3",
        "1.2.3.dev",
        "1.2.3c1",
    ],
)
def test_rejected_versions(version: str) -> None:
    """Reject unsupported or noncanonical version spellings."""
    with pytest.raises(ValueError):
        validate(Config("fixture", version))
    return


def test_defaults_and_explicit_false(project: Path) -> None:
    """Preserve explicit CLI false and empty values ahead of configuration defaults."""
    args = parser().parse_args(
        [
            "build",
            "--local",
            "--no-changelog",
            "--no-github-release-publish",
            "--allow-dirty",
            "--no-pre-release-branches",
        ]
    )
    config = load(project, overrides(args))
    assert config.changelog is False
    assert config.github_release_publish is False
    assert config.pre_release_branches is False
    assert config.allow_dirty == ()
    assert overrides(parser().parse_args(["build", "--local"])) == {}
    return


@pytest.mark.parametrize(
    "changes",
    [
        {"final_release_branches": False},
        {"final_release_branches": []},
        {"dev_release_branches": []},
        {"pre_release_branches": []},
        {"pre_release_branches": ["release/*"]},
        {"changelog": True},
        {"github_release_types": ["stable"]},
        {"allow_dirty": False},
        {"testpypi_username": "alice"},
    ],
)
def test_bad_configuration(changes: dict[str, object]) -> None:
    """Reject invalid policy values and attempts to disable mandatory branch restrictions."""
    with pytest.raises(ValueError):
        validate(replace(Config("fixture", "1.2.3"), **changes))
    return


def test_unknown_settings(project: Path) -> None:
    """Reject removed configuration options instead of silently accepting them."""
    with (project / "pyproject.toml").open("a") as stream:
        stream.write('version-mode = "declared"\n')
    with pytest.raises(ValueError, match="unknown"):
        load(project)
    return


@pytest.mark.parametrize("version", ["1.2.3.dev1", "1.2.3a1", "1.2.3rc1"])
def test_unreleased_pre_and_dev(project: Path, version: str) -> None:
    """Allow unreleased changelog sections for development and prerelease versions."""
    config = replace(load(project), version=version)
    assert "### Added" in release_notes(project, config)
    return


def test_changelog_rules(project: Path) -> None:
    """Reject invalid dates, duplicate sections and sections containing only headings."""
    config = load(project)
    path = project / "docs" / "changelog.md"
    with pytest.raises(ValueError, match="date"):
        release_notes(project, replace(config, version="1.2.3"))
    path.write_text("## [1.2.3] - 2026-11-15\n\n### Only headings\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nonempty"):
        release_notes(project, config)
    path.write_text("## [1.2.3] - 2026-02-30\n\n- Change\n", encoding="utf-8")
    with pytest.raises(ValueError):
        release_notes(project, config)
    path.write_text(
        "## [1.2.3] - 2026-11-15\n\n- Change\n## [1.2.3] - 2026-11-15\n- Duplicate\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly one"):
        release_notes(project, config)
    assert release_notes(project, replace(config, changelog=False)) is None
    return


@pytest.mark.parametrize(
    "source",
    [
        '[project]\nname = "example" # comment\nversion = "1.2.3"\n',
        '["project"]\n"name" = \'example\'\nversion = "1.2.3"\n',
        'project.name = "example"\nproject.version = "1.2.3"\n',
    ],
)
def test_testpypi_rename_preserves_values(tmp_path: Path, source: str) -> None:
    """Change the static name for different TOML spellings without changing other values."""
    path = tmp_path / "pyproject.toml"
    path.write_bytes(source.encode("utf-8"))
    expected = tomllib.loads(source)
    expected["project"]["name"] = "example-alice"
    rename_distribution(path, "example-alice")
    assert tomllib.loads(path.read_text(encoding="utf-8")) == expected
    return


def test_index_partial_complete_conflicting() -> None:
    """Distinguish incomplete index metadata from complete and conflicting file sets."""
    expected = {"one.whl": "a" * 64, "two.tar.gz": "b" * 64}
    one = {"filename": "one.whl", "digests": {"sha256": "a" * 64}}
    two = {"filename": "two.tar.gz", "digests": {"sha256": "b" * 64}}
    assert publication_matches(None, expected) is False
    assert publication_matches({"urls": []}, expected) is False
    assert publication_matches({"urls": [one]}, expected) is False
    assert publication_matches({"urls": [one, two]}, expected)
    for files in [
        [one, one],
        [{**two, "digests": {"sha256": "c" * 64}}],
        [{**one, "yanked": True}],
    ]:
        with pytest.raises(ValueError):
            publication_matches({"urls": files}, expected)
    return
