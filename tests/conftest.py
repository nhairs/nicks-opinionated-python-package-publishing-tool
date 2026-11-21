"""Real local source repositories for behavioral tests."""

from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a minimal tagged setuptools project with an excluded root file."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=80"]
build-backend = "setuptools.build_meta"
[project]
name = "fixture-package"
version = "1.2.3.dev4"
[tool.setuptools.packages.find]
where = ["src"]
[tool.setuptools.package-data]
fixture_package = ["data.txt", "py.typed"]
[tool.nopt]
""",
        encoding="utf-8",
    )
    package = root / "src" / "fixture_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('VALUE = "original"\n', encoding="utf-8")
    (package / "data.txt").write_text("data\n", encoding="utf-8")
    (package / "py.typed").touch()
    (root / "README.md").write_text("Fixture\n", encoding="utf-8")
    (root / "excluded.txt").write_text("excluded\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        "*.egg-info/\nignored.txt\nrelease-bundle/\n", encoding="utf-8"
    )
    (root / "docs").mkdir()
    (root / "docs" / "changelog.md").write_text(
        "## [1.2.3] - UNRELEASED\n\n### Added\n\n- A change.\n", encoding="utf-8"
    )
    for args in [
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "."),
        ("commit", "-m", "Initial fixture"),
        ("tag", "v1.2.3.dev4"),
    ]:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root
