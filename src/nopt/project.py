"""Update the distribution name for a temporary TestPyPI build."""

from pathlib import Path
import tomllib

import tomli_w


def rename_distribution(path: Path, name: str) -> None:
    """Rename an already validated project identity for a temporary TestPyPI build."""
    document = tomllib.loads(path.read_bytes().decode("utf-8"))
    document["project"]["name"] = name
    path.write_bytes(tomli_w.dumps(document).encode("utf-8"))
    return
