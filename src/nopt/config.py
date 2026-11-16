"""Validate the deliberately small public configuration surface."""

from dataclasses import dataclass, fields, replace
from pathlib import Path
import re
import tomllib

from packaging.utils import canonicalize_name
from packaging.version import Version

GROUPS = {"dev", "pre", "final"}
VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:(?:\.dev|a|b|rc)(?:0|[1-9][0-9]*))?"
)


@dataclass(frozen=True)
class Config:  # pylint: disable=too-many-instance-attributes
    """Resolved project identity and release policy."""

    name: str
    version: str
    changelog: str | bool = "docs/changelog.md"
    repository: str = "pypi"
    allow_dirty: tuple[str, ...] = ("dev",)
    final_release_branches: tuple[str, ...] | bool = ("main",)
    pre_release_branches: tuple[str, ...] | bool = ("main",)
    dev_release_branches: tuple[str, ...] | bool = False
    testpypi_username: str | None = None
    github_release_publish: bool = True
    github_release_types: tuple[str, ...] = ("pre", "final")

    @property
    def group(self) -> str:
        """Classify the supported version subset."""
        parsed = Version(self.version)
        return "dev" if parsed.is_devrelease else "pre" if parsed.is_prerelease else "final"

    @property
    def distribution(self) -> str:
        """Return the index-facing distribution name."""
        return (
            self.name if self.testpypi_username is None else f"{self.name}-{self.testpypi_username}"
        )

    @property
    def github_requested(self) -> bool:
        """Return whether this release should be announced on GitHub."""
        return self.github_release_publish and self.group in self.github_release_types


def validate(config: Config) -> Config:  # pylint: disable=too-many-branches
    """Reject unsupported or ambiguous configuration."""
    if not isinstance(config.name, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", config.name
    ):
        raise ValueError("project.name must be a distribution name")
    if not isinstance(config.version, str) or not VERSION.fullmatch(config.version):
        raise ValueError("version must be canonical X.Y.Z with at most one .devN/aN/bN/rcN suffix")
    if str(Version(config.version)) != config.version:
        raise ValueError("version must use canonical spelling")
    if config.changelog is not False and (
        not isinstance(config.changelog, str) or not config.changelog
    ):
        raise ValueError("changelog must be a path or false")
    if not isinstance(config.repository, str) or config.repository not in {"pypi", "testpypi"}:
        raise ValueError("repository must be pypi or testpypi")
    for setting in ("allow_dirty", "github_release_types"):
        value = getattr(config, setting)
        if (
            not isinstance(value, (tuple, list))
            or any(not isinstance(item, str) or item not in GROUPS for item in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError(
                f"{setting.replace('_', '-')} must contain unique dev/pre/final groups"
            )
    for group in ("final", "pre", "dev"):
        branches = getattr(config, f"{group}_release_branches")
        if branches is False and group != "final":
            continue
        if not isinstance(branches, (tuple, list)) or not branches:
            raise ValueError(f"{group}-release-branches must be a nonempty branch list")
        for branch in branches:
            if (
                not isinstance(branch, str)
                or not branch
                or any(
                    token in branch
                    for token in ("*", "?", "[", "..", "@{", "\\", " ", ":", "~", "^", "\n", "\r")
                )
                or branch.startswith(("-", "/", "."))
                or branch.endswith(("/", ".", ".lock"))
            ):
                raise ValueError("branches must be literal valid Git branch names, without globs")
    if not isinstance(config.github_release_publish, bool):
        raise ValueError("github-release-publish must be boolean")
    if config.testpypi_username is not None:
        if config.repository != "testpypi":
            raise ValueError("testpypi-username requires repository=testpypi")
        if not isinstance(config.testpypi_username, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", config.testpypi_username
        ):
            raise ValueError(
                "testpypi-username must use lowercase letters, digits and single hyphens"
            )
        canonicalize_name(config.distribution, validate=True)
    return replace(
        config,
        **{
            key: tuple(value) if isinstance(value, list) else value
            for key, value in vars(config).items()
        },
    )


def load(root: Path, overrides: dict | None = None) -> Config:
    """Read TOML with explicit CLI overrides and preserve false values."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    if not isinstance(project, dict):
        raise ValueError("project must be a TOML table")
    if any(key in project.get("dynamic", []) for key in ("name", "version")):
        raise ValueError("project name/version must be static")
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise ValueError("tool must be a TOML table")
    table = tool.get("nopt", {})
    if not isinstance(table, dict):
        raise ValueError("tool.nopt must be a TOML table")
    allowed = {field.name for field in fields(Config)} - {"name", "version"}
    unknown = {key for key in table if key.replace("-", "_") not in allowed}
    if unknown:
        raise ValueError(f"unknown tool.nopt settings: {', '.join(sorted(unknown))}")
    config = Config(
        project.get("name"),
        project.get("version"),
        **{
            key.replace("-", "_"): tuple(value) if isinstance(value, list) else value
            for key, value in table.items()
        },
    )
    return validate(replace(config, **(overrides or {})))
