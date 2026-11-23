"""Exercise draft publication over local HTTP without creating remote releases."""

from collections.abc import Iterator
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tarfile
from threading import Thread
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile

import pytest

from nopt import remote
from nopt.cli import build_parser, execute


@dataclass
class APIState:
    """Observed requests and the local service's release/asset state."""

    requests: list[tuple[str, str]] = field(default_factory=list)
    release: dict[str, object] | None = None
    listed_releases: list[dict[str, object]] = field(default_factory=list)
    assets: dict[str, bytes] = field(default_factory=dict)
    expected_index: dict[str, str] = field(default_factory=dict)
    fail_upload: bool = False
    publish_stays_draft: bool = False


@pytest.fixture
def github_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[APIState]:
    """Route real client HTTP calls to a local GitHub/PyPI contract fixture."""
    state = APIState()

    class Handler(BaseHTTPRequestHandler):
        """Implement only endpoints involved in release publication."""

        def log_message(self, format: str, *args: object) -> None:
            """Keep request headers and routine server logs out of test output."""
            return

        def respond(self, status: int, data: object) -> None:
            """Send an actual JSON HTTP response to the production transport."""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
            return

        def do_GET(self) -> None:
            """Expose live tags, publication metadata and paginated releases."""
            state.requests.append(("GET", self.path))
            parts = urlsplit(self.path)
            if parts.path == "/repos/owner/repo/commits/tags/v1.2.3":
                self.respond(200, {"sha": "a" * 40})
            elif parts.path.endswith("/git/matching-refs/tags/"):
                self.respond(200, [{"ref": "refs/tags/v1.2.3"}])
            elif "/releases/tags/" in parts.path:
                if state.release and state.release["draft"] is False:
                    self.respond(200, state.release)
                else:
                    self.respond(404, {"message": "Not found"})
            elif parts.path.endswith("/releases"):
                page = int(parse_qs(parts.query)["page"][0])
                records = state.listed_releases + ([state.release] if state.release else [])
                self.respond(200, records[(page - 1) * 100 : page * 100])
            elif parts.path.startswith("/pypi/"):
                self.respond(
                    200,
                    {
                        "urls": [
                            {"filename": name, "digests": {"sha256": digest}}
                            for name, digest in state.expected_index.items()
                        ]
                    },
                )
            else:
                self.respond(404, {"message": "Unknown fixture route"})
            return

        def do_POST(self) -> None:
            """Create a draft or attach bytes, rejecting uploads after publication."""
            state.requests.append(("POST", self.path))
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            parts = urlsplit(self.path)
            if parts.path.endswith("/releases"):
                payload = json.loads(body)
                assert payload["draft"] is True
                assert payload["make_latest"] == "false"
                assert payload["body"] == "## [1.2.3] - 2026-11-23\n\n- A change.\n"
                state.release = {"id": 7, "draft": True, "tag_name": payload["tag_name"]}
                self.respond(201, state.release)
            else:
                assert parts.path == "/repos/owner/repo/releases/7/assets"
                assert state.release and state.release["draft"] is True
                if state.fail_upload:
                    self.respond(500, {"message": "Upload interrupted"})
                else:
                    name = unquote(parse_qs(parts.query)["name"][0])
                    state.assets[name] = body
                    self.respond(201, {"state": "uploaded"})
            return

        def do_PATCH(self) -> None:
            """Publish only after every original asset has been uploaded."""
            state.requests.append(("PATCH", self.path))
            assert self.path == "/repos/owner/repo/releases/7"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert payload == {"draft": False, "make_latest": "true"}
            assert len(state.assets) == 6
            assert state.release is not None
            state.release.update(
                draft=state.publish_stays_draft,
                html_url="https://github.com/owner/repo/releases/tag/v1.2.3",
            )
            self.respond(200, state.release)
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    original_request = remote.request

    def local_request(
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        github: bool = False,
        content_type: str = "application/json",
    ) -> object:
        """Redirect transport hosts only; preserve real HTTP, bodies and validation."""
        parts = urlsplit(url)
        assert parts.netloc in {"api.github.com", "uploads.github.com", "pypi.org"}
        local_url = f"http://127.0.0.1:{server.server_port}{parts.path}"
        if parts.query:
            local_url += "?" + parts.query
        return original_request(local_url, method, data, github, content_type)

    monkeypatch.setattr(remote, "request", local_request)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    for key, value in {
        "GH_TOKEN": "local-test-token",
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_REF": "refs/tags/v1.2.3",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, value)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return


def checked_release_bundle(root: Path, include_notes: bool = True) -> Path:
    """Write a standalone checked bundle without invoking the build implementation."""
    (root / "dist").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    metadata = b"Metadata-Version: 2.1\nName: fixture\nVersion: 1.2.3\n"
    with zipfile.ZipFile(root / "dist/fixture-1.2.3-py3-none-any.whl", "w") as archive:
        archive.writestr("fixture-1.2.3.dist-info/METADATA", metadata)
    with tarfile.open(root / "dist/fixture-1.2.3.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("fixture-1.2.3/PKG-INFO")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))
    if include_notes:
        (root / "release-notes.md").write_text(
            "## [1.2.3] - 2026-11-23\n\n- A change.\n", encoding="utf-8"
        )
    hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    manifest = {
        "schema_version": 1,
        "distribution_name": "fixture",
        "files": hashes,
        "source": {"commit": "a" * 40, "tag": "v1.2.3", "dirty": False},
        "context": {
            "local": False,
            "tag": "v1.2.3",
            "github_repository": "owner/repo",
            "run_id": "123",
            "run_attempt": "1",
        },
    }
    manifest_path = root / "release.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    hashes["release.json"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())),
        encoding="utf-8",
    )
    return root


def publish_arguments(release_bundle: Path, state: APIState, check_only: bool = False) -> list[str]:
    """Configure index observations from original bytes and select publication inputs."""
    state.expected_index = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (release_bundle / "dist").iterdir()
    }
    arguments = ["publish-github-release", "--bundle", str(release_bundle)]
    if check_only:
        arguments.append("--check-only")
    return arguments


def test_draft_upload_then_publish(tmp_path: Path, github_api: APIState) -> None:
    """Publish after all original assets, with tag/latest checks after upload."""
    release_bundle = checked_release_bundle(tmp_path / "release-bundle")
    result = execute(build_parser().parse_args(publish_arguments(release_bundle, github_api)))
    assert result["assets"] == 6
    assert github_api.release and github_api.release["draft"] is False
    assert github_api.assets == {
        path.name: path.read_bytes() for path in release_bundle.rglob("*") if path.is_file()
    }
    methods = [method for method, _ in github_api.requests]
    assert methods.count("POST") == 7
    assert methods[-3:] == ["GET", "GET", "PATCH"]
    return


def test_failed_upload_leaves_draft_and_blocks_retry(tmp_path: Path, github_api: APIState) -> None:
    """Never publish an incomplete release or silently resume its draft."""
    release_bundle = checked_release_bundle(tmp_path / "release-bundle")
    arguments = publish_arguments(release_bundle, github_api)
    github_api.fail_upload = True
    with pytest.raises(ValueError, match="HTTP 500"):
        execute(build_parser().parse_args(arguments))
    assert github_api.release and github_api.release["draft"] is True
    assert not any(method == "PATCH" for method, _ in github_api.requests)
    with pytest.raises(ValueError, match="already exists"):
        execute(build_parser().parse_args(arguments))
    assert (
        sum(path.endswith("/releases") for method, path in github_api.requests if method == "POST")
        == 1
    )
    return


def test_draft_collision_pagination(github_api: APIState) -> None:
    """Find existing drafts beyond the first listing page before creating anything."""
    github_api.listed_releases = [
        {"tag_name": f"v0.0.{number}", "draft": False} for number in range(100)
    ] + [{"tag_name": "v1.2.3", "draft": True}]
    with pytest.raises(ValueError, match="already exists"):
        remote.GitHubClient("owner/repo").release_absent("v1.2.3")
    assert any("page=2" in path for _, path in github_api.requests)
    assert all(method == "GET" for method, _ in github_api.requests)
    return


def test_publication_check_only_makes_no_draft(tmp_path: Path, github_api: APIState) -> None:
    """Run the same publication eligibility checks without any writes."""
    release_bundle = checked_release_bundle(tmp_path / "release-bundle")
    result = execute(build_parser().parse_args(publish_arguments(release_bundle, github_api, True)))
    assert result == {"checked": "publish-github-release", "latest": True}
    assert all(method == "GET" for method, _ in github_api.requests)
    assert github_api.release is None
    return


def test_unconfirmed_publish_is_an_error(tmp_path: Path, github_api: APIState) -> None:
    """Do not report success for HTTP 200 that still describes an unpublished draft."""
    release_bundle = checked_release_bundle(tmp_path / "release-bundle")
    github_api.publish_stays_draft = True
    with pytest.raises(ValueError, match="did not confirm publication"):
        execute(build_parser().parse_args(publish_arguments(release_bundle, github_api)))
    return


def test_missing_release_notes_fail_before_http(tmp_path: Path, github_api: APIState) -> None:
    """Require the checked notes without extracting or generating them at publication."""
    release_bundle = checked_release_bundle(tmp_path / "release-bundle", include_notes=False)
    with pytest.raises(ValueError, match="requires bundled release-notes.md"):
        execute(build_parser().parse_args(publish_arguments(release_bundle, github_api)))
    assert github_api.requests == []
    return
