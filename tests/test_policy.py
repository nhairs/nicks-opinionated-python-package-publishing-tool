"""Configuration, version, changelog and index policy boundaries."""

from dataclasses import replace
import pytest

from nopt.changelog import release_notes
from nopt.cli import overrides, parser
from nopt.config import Config, load, validate
from nopt.remote import publication_matches
from nopt.staging import rename_distribution


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
def test_versions(version, group):
    config = validate(Config("fixture", version, "setuptools.build_meta"))
    assert config.group == group


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
def test_rejected_versions(version):
    with pytest.raises(ValueError):
        validate(Config("fixture", version, "setuptools.build_meta"))


def test_defaults_and_explicit_false(project):
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
def test_bad_configuration(changes):
    with pytest.raises(ValueError):
        validate(replace(Config("fixture", "1.2.3", "setuptools.build_meta"), **changes))


def test_unknown_settings(project):
    with (project / "pyproject.toml").open("a") as stream:
        stream.write('version-mode = "declared"\n')
    with pytest.raises(ValueError, match="unknown"):
        load(project)


@pytest.mark.parametrize("version", ["1.2.3.dev1", "1.2.3a1", "1.2.3rc1"])
def test_unreleased_pre_and_dev(project, version):
    config = replace(load(project), version=version)
    assert "### Added" in release_notes(project, config)


def test_changelog_rules(project):
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


def test_testpypi_staging_preserves_other_content(project):
    path = project / "pyproject.toml"
    before = path.read_text(encoding="utf-8")
    rename_distribution(path, "fixture-package-alice")
    assert path.read_text(encoding="utf-8") == before.replace(
        'name = "fixture-package"', 'name = "fixture-package-alice"'
    )


def test_index_partial_complete_conflicting():
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
