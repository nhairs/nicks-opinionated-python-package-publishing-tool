"""Bounded index checks and minimal GitHub release operations; no package upload."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


from .config import parse_version
from .source import is_latest_final


def decode_response(raw: bytes) -> object:
    """Decode a successful JSON response without confusing empty/null with HTTP 404."""
    try:
        decoded: object = json.loads(raw)
    except ValueError:
        raise ValueError("successful API response must contain valid JSON") from None
    if decoded is None:
        raise ValueError("successful API response must not contain JSON null")
    return decoded


def request(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    github: bool = False,
    content_type: str = "application/json",
) -> object:
    """Perform one request; only HTTP 404 is treated as absence."""
    headers = {"User-Agent": "nopt/0.1", "Accept": "application/json"}
    if github:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GitHub API access requires GH_TOKEN or GITHUB_TOKEN")
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if data is not None:
        headers["Content-Type"] = content_type
    try:
        with urlopen(
            Request(url, data=data, headers=headers, method=method), timeout=30
        ) as response:
            raw = response.read()
            return decode_response(raw)
    except HTTPError as error:
        if error.code == 404:
            return None
        # Do not expose response bodies or authenticated request headers.
        raise ValueError(f"HTTP {error.code} from {url.split('?')[0]}") from None


class PyPIClient:
    """Read public package-index records without GitHub credentials."""

    def index(self, target: str, name: str, version: str) -> object:
        """Look up exactly the target version on the chosen index."""
        host = "pypi.org" if target == "pypi" else "test.pypi.org"
        return request(f"https://{host}/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json")

    def absent(self, target: str, name: str, version: str) -> None:
        """Any record, even empty or yanked, prevents a new upload."""
        if self.index(target, name, version) is not None:
            raise ValueError(f"{target} already has a record for {name} {version}")
        return

    def verify_index(
        self, target: str, name: str, version: str, expected: dict[str, str], timeout: float = 300
    ) -> None:
        """Wait only for missing/partial matching metadata; fail promptly on conflicts."""
        deadline = time.monotonic() + timeout
        while True:
            if publication_matches(self.index(target, name, version), expected):
                return
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"timed out waiting for {target} filenames/hashes: expected {expected}"
                )
            time.sleep(min(5, max(0, deadline - time.monotonic())))


class GitHubClient:
    """Interact with one authenticated GitHub repository."""

    def __init__(self, repository: str | None) -> None:
        """Bind authenticated operations to one repository."""
        self.repository = repository
        return

    def github(
        self, path: str, method: str = "GET", data: Mapping[str, object] | None = None
    ) -> object:
        """Use a fixed API host and validated owner/repository identity."""
        if self.repository is None:
            raise ValueError("GitHub repository identity is required")
        return request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            method,
            None if data is None else json.dumps(data).encode(),
            github=True,
        )

    def upload_asset(self, release_id: int, path: Path) -> None:
        """Attach one original bundle file and require a completed upload."""
        if self.repository is None:
            raise ValueError("GitHub repository identity is required")
        result = request(
            f"https://uploads.github.com/repos/{self.repository}"
            f"/releases/{release_id}/assets?name={quote(path.name, safe='')}",
            "POST",
            path.read_bytes(),
            github=True,
            content_type="application/octet-stream",
        )
        if not isinstance(result, dict) or result.get("state") != "uploaded":
            raise ValueError(
                f"GitHub asset upload did not complete: {path.name}; recovery is deferred"
            )
        return

    def check_tag(self, tag: str, commit: str) -> None:
        """Resolve the live remote tag, including annotated tags, through commits API."""
        data = self.github(f"commits/tags/{quote(tag, safe='')}")
        if not isinstance(data, dict) or data.get("sha") != commit:
            raise ValueError(f"GitHub tag {tag} is missing or no longer points to {commit}")
        return

    def is_latest_final(self, version: str) -> bool:
        """Read Git tag refs and identify the highest canonical final release."""
        parsed = parse_version(version)
        if parsed.is_devrelease or parsed.is_prerelease:
            return False
        data = self.github("git/matching-refs/tags/")
        if not isinstance(data, list):
            raise ValueError("GitHub tag references must be an array")
        refs: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("malformed GitHub tag reference")
            ref = item.get("ref")
            if not isinstance(ref, str) or not ref.startswith("refs/tags/"):
                raise ValueError("malformed GitHub tag reference name")
            refs.append(ref)
        return is_latest_final(version, refs)

    def release_absent(self, tag: str) -> None:
        """Fail on an existing release; recovery is deliberately deferred."""
        if self.github(f"releases/tags/{quote(tag, safe='')}") is not None:
            raise ValueError("GitHub release already exists; automatic recovery is not implemented")
        return


def publication_matches(data: object, expected: Mapping[str, str]) -> bool:
    """Return incomplete versus complete metadata; reject malformed/conflicting data."""
    if data is None:
        return False
    if not isinstance(data, dict) or not isinstance(data.get("urls"), list):
        raise ValueError("malformed index response")
    observed: dict[str, str] = {}
    for item in data["urls"]:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError("malformed index file metadata")
        name = item["filename"]
        digests = item.get("digests")
        if not isinstance(digests, dict) or not isinstance(digests.get("sha256"), str):
            raise ValueError("malformed index file digest metadata")
        digest = digests["sha256"]
        if name in observed or name not in expected or digest != expected[name]:
            raise ValueError(
                f"index conflict: expected {expected}; observed conflicting file {name}: {digest}"
            )
        if item.get("yanked", False):
            raise ValueError(f"index file is yanked: {name}")
        observed[name] = digest
    return observed == expected
