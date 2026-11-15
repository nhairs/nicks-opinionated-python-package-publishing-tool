"""Apply the single supported TOML staging edit without rewriting other settings."""

from pathlib import Path
import re
import tomllib


def rename_distribution(path: Path, name: str) -> None:
    """Replace a conventional static project.name assignment, or fail closed."""
    text = path.read_text(encoding="utf-8")
    # Refuse unusual TOML representations instead of guessing at their semantics.
    sections = list(re.finditer(r"(?m)^\[project\][ \t]*(?:#.*)?$", text))
    if len(sections) != 1:
        raise ValueError("TestPyPI suffixing requires a conventional [project] table")
    start = sections[0].end()
    next_table = re.search(r"(?m)^\[", text[start:])
    end = len(text) if next_table is None else start + next_table.start()
    table = text[start:end]
    pattern = r"""(?m)^(name[ \t]*=[ \t]*)(?:"[^"\n]*"|'[^'\n]*')([ \t]*(?:#.*)?)$"""
    table, count = re.subn(pattern, lambda match: match[1] + '"' + name + '"' + match[2], table)
    if count != 1:
        raise ValueError("TestPyPI suffixing requires one simple project.name string assignment")
    result = text[:start] + table + text[end:]
    if tomllib.loads(result)["project"]["name"] != name:
        raise ValueError("staged name mismatch")
    path.write_text(result, encoding="utf-8")
