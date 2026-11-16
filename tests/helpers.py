"""Shared filesystem evidence helpers."""

from pathlib import Path


def inventory(root: Path) -> dict[str, bytes]:
    """Include ignored/untracked files; exclude only Git internals."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }
