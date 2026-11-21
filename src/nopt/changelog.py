"""Extract the full base-version changelog section without modifying it."""

from datetime import date
from pathlib import Path
import re

from packaging.version import Version

from .config import Config


def release_notes(root: Path, config: Config) -> str | None:
    """Validate section count, content and the approved date policy."""
    if config.changelog is False:
        return None
    path = (root / str(config.changelog)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("changelog path must be inside the project")
    text = path.read_text(encoding="utf-8")
    base = Version(config.version).base_version
    sections = re.split(r"(?m)(?=^## )", text)
    matches = [
        section for section in sections if re.match(rf"^## \[{re.escape(base)}\](?:\(|\s)", section)
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one changelog section for {base}")
    heading, _, content = matches[0].partition("\n")
    stamp = heading.rsplit(" - ", 1)[-1]
    if config.release_type == "final":
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", stamp):
            raise ValueError("final releases require a YYYY-MM-DD changelog date")
        date.fromisoformat(stamp)
    elif stamp != "UNRELEASED":
        raise ValueError("dev/pre releases require an UNRELEASED changelog section")
    if not any(line.strip() and not line.startswith("#") for line in content.splitlines()):
        raise ValueError("changelog section requires nonempty content")
    return matches[0].rstrip() + "\n"
