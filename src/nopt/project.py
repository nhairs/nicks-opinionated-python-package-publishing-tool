"""Update the distribution name for a temporary TestPyPI build."""

from pathlib import Path
import tomllib

import tomli_w


def rename_distribution(path: Path, name: str) -> None:
    """Serialize a static project.name change without preserving temporary formatting."""
    document = tomllib.loads(path.read_bytes().decode("utf-8"))
    project = document.get("project")
    if (
        not isinstance(project, dict)
        or not isinstance(project.get("name"), str)
        or not project["name"].strip()
        or not name.strip()
    ):
        raise ValueError("TestPyPI renaming requires a static project.name string")
    project["name"] = name
    encoded = tomli_w.dumps(document).encode("utf-8")
    # Check the serialized value before changing the working file.
    if tomllib.loads(encoded.decode("utf-8"))["project"]["name"] != name:
        raise ValueError("build name mismatch")
    path.write_bytes(encoded)
    return
