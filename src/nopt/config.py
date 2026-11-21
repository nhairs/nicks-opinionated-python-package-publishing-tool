"""Validate the deliberately small public configuration surface."""

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
import re
import tomllib
from typing import Literal

from packaging.utils import canonicalize_name
from packaging.version import Version

_GROUPS = {"dev", "pre", "final"}
_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:(?:\.dev|a|b|rc)(?:0|[1-9][0-9]*))?"
)


def parse_version(value: object) -> Version:
    """Parse only the canonical version forms accepted by nopt release policy."""
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise ValueError("version must be canonical X.Y.Z with at most one .devN/aN/bN/rcN suffix")
    parsed = Version(value)
    if str(parsed) != value:
        raise ValueError("version must use canonical spelling")
    return parsed


@dataclass(frozen=True)
class Config:  # pylint: disable=too-many-instance-attributes
    """Resolved project identity and release policy."""

    name: str
    version: str
    changelog: str | Literal[False] = "docs/changelog.md"
    repository: str = "pypi"
    allow_dirty: tuple[str, ...] = ("dev",)
    final_release_branches: tuple[str, ...] = ("main",)
    pre_release_branches: tuple[str, ...] | Literal[False] = ("main",)
    dev_release_branches: tuple[str, ...] | Literal[False] = False
    testpypi_username: str | None = None
    github_release_publish: bool = True
    github_release_types: tuple[str, ...] = ("pre", "final")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "Config":
        """Resolve untrusted policy values into a validated immutable configuration."""
        name = values.get("name")
        version = values.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("project.name and project.version must be static strings")
        allowed = {field.name for field in fields(cls)}
        if unknown := set(values) - allowed:
            raise ValueError(f"unknown configuration fields: {', '.join(sorted(unknown))}")
        policy = {key: value for key, value in values.items() if key not in {"name", "version"}}
        return validate(cls(name, version, **policy))

    @property
    def release_type(self) -> str:
        """Classify the supported version subset."""
        parsed = Version(self.version)
        return "dev" if parsed.is_devrelease else "pre" if parsed.is_prerelease else "final"

    @property
    def distribution_name(self) -> str:
        """Return the index-facing distribution name."""
        return (
            self.name if self.testpypi_username is None else f"{self.name}-{self.testpypi_username}"
        )

    @property
    def github_release_requested(self) -> bool:
        """Return whether this release should be announced on GitHub."""
        return self.github_release_publish and self.release_type in self.github_release_types


def validate(config: Config) -> Config:  # pylint: disable=too-many-branches
    """Reject unsupported or ambiguous configuration."""
    if not isinstance(config.name, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", config.name
    ):
        raise ValueError("project.name must be a distribution name")
    parse_version(config.version)
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
            or any(not isinstance(item, str) or item not in _GROUPS for item in value)
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
        canonicalize_name(config.distribution_name, validate=True)
    return replace(
        config,
        **{
            key: tuple(value) if isinstance(value, list) else value
            for key, value in vars(config).items()
        },
    )


def load(root: Path, overrides: Mapping[str, object] | None = None) -> Config:
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
    values: dict[str, object] = {
        "name": project.get("name"),
        "version": project.get("version"),
        **{key.replace("-", "_"): value for key, value in table.items()},
        **(overrides or {}),
    }
    return Config.from_mapping(values)
