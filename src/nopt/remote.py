"""Bounded index checks and minimal GitHub release operations; no package upload."""

import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


class Client:
    """Read public index metadata and interact with one authenticated repository."""

    def __init__(self, repository: str | None = None):
        self.repository = repository

    @staticmethod
    def request(
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        github: bool = False,
        content_type: str = "application/json",
    ):
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
                return json.loads(raw) if raw else None
        except HTTPError as error:
            if error.code == 404:
                return None
            # Do not expose response bodies or authenticated request headers.
            raise ValueError(f"HTTP {error.code} from {url.split('?')[0]}") from None

    def index(self, target: str, name: str, version: str):
        """Look up exactly the target version on the chosen index."""
        host = "pypi.org" if target == "pypi" else "test.pypi.org"
        return self.request(
            f"https://{host}/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
        )

    def absent(self, target: str, name: str, version: str) -> None:
        """Any record, even empty or yanked, prevents a new upload."""
        if self.index(target, name, version) is not None:
            raise ValueError(f"{target} already has a record for {name} {version}")

    def github(self, path: str, method: str = "GET", data: dict | None = None):
        """Use a fixed API host and validated owner/repository identity."""
        if self.repository is None:
            raise ValueError("GitHub repository identity is required")
        return self.request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            method,
            None if data is None else json.dumps(data).encode(),
            github=True,
        )

    def check_tag(self, tag: str, commit: str) -> None:
        """Resolve the live remote tag, including annotated tags, through commits API."""
        data = self.github(f"commits/{quote(tag, safe='')}")
        if not isinstance(data, dict) or data.get("sha") != commit:
            raise ValueError(f"GitHub tag {tag} is missing or no longer points to {commit}")

    def release_absent(self, tag: str) -> None:
        """Fail on an existing release; recovery is deliberately deferred."""
        if self.github(f"releases/tags/{quote(tag, safe='')}") is not None:
            raise ValueError("GitHub release already exists; automatic recovery is not implemented")

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


def publication_matches(data, expected: dict[str, str]) -> bool:
    """Return incomplete versus complete metadata; reject malformed/conflicting data."""
    if data is None:
        return False
    if not isinstance(data, dict) or not isinstance(data.get("urls"), list):
        raise ValueError("malformed index response")
    observed = {}
    for item in data["urls"]:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError("malformed index file metadata")
        name = item["filename"]
        digest = item.get("digests", {}).get("sha256")
        if name in observed or name not in expected or digest != expected[name]:
            raise ValueError(
                f"index conflict: expected {expected}; observed conflicting file {name}: {digest}"
            )
        if item.get("yanked", False):
            raise ValueError(f"index file is yanked: {name}")
        observed[name] = digest
    return observed == expected
