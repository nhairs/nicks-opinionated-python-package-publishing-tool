"""Public command line; checks always precede nopt-owned actions."""

import argparse
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile
from urllib.parse import quote
from urllib.error import URLError

from .bundle import make_bundle, read_bundle, validate_output
from .changelog import release_notes
from .config import load
from .remote import Client
from .source import Context, identity


def parser() -> argparse.ArgumentParser:
    """Build the agreed small CLI without speculative skip/force flags."""
    result = argparse.ArgumentParser(prog="nopt")
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("ci-preflight", "Check source release eligibility before user CI"),
        ("build", "Check eligibility, build distributions and prepare a local release bundle"),
        (
            "publish-release-preflight",
            "Verify the original bundle and recheck eligibility before external upload",
        ),
        ("publish-github-release", "Verify PyPI files and publish a GitHub release"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--project",
            type=Path,
            default=Path.cwd(),
            help="project checkout (default: current directory)",
        )
        if name != "publish-github-release":
            command.add_argument(
                "--local", action="store_true", help="use local refs/inputs rather than GHA context"
            )
        command.add_argument(
            "--tag", help="existing v-prefixed tag pointing to HEAD; inferred from version locally"
        )
        if name in {"build", "publish-github-release"}:
            command.add_argument(
                "--check-only",
                action="store_true",
                help="perform checks without building or publishing",
            )
        if name == "build":
            command.add_argument(
                "--output",
                type=Path,
                default=Path("release-bundle"),
                help="new bundle directory; must be ignored if inside checkout",
            )
        if name in {"publish-release-preflight", "publish-github-release"}:
            command.add_argument(
                "--bundle", type=Path, required=True, help="original downloaded release bundle"
            )
        if name != "publish-github-release":
            command.add_argument("--repository", choices=("pypi", "testpypi"), default=None)
            command.add_argument("--testpypi-username")
            command.add_argument("--changelog")
            command.add_argument(
                "--no-changelog", action="store_const", const=False, dest="changelog"
            )
            command.add_argument(
                "--allow-dirty", nargs="*", choices=("dev", "pre", "final"), default=None
            )
            for group in ("final", "pre", "dev"):
                command.add_argument(f"--{group}-release-branches", nargs="+", default=None)
            command.add_argument(
                "--no-pre-release-branches",
                action="store_const",
                const=False,
                dest="pre_release_branches",
            )
            command.add_argument(
                "--no-dev-release-branches",
                action="store_const",
                const=False,
                dest="dev_release_branches",
            )
            toggle = command.add_mutually_exclusive_group()
            toggle.add_argument("--github-release-publish", action="store_true", default=None)
            toggle.add_argument(
                "--no-github-release-publish", action="store_false", dest="github_release_publish"
            )
            command.add_argument(
                "--github-release-types", nargs="*", choices=("dev", "pre", "final"), default=None
            )
    template = commands.add_parser(
        "workflow-template", help="Print or write an editable GHA release workflow"
    )
    template.add_argument("--output", type=Path, help="new file; never overwrite an existing file")
    return result


def context(args) -> Context:
    """Require GHA identity unless the caller explicitly selects local mode."""
    if getattr(args, "local", False):
        return Context(True, args.tag)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("outside GHA use --local; local GitHub publishing is not supported")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY must be owner/repository")
    ref = os.environ.get("GITHUB_REF", "")
    if not ref.startswith("refs/tags/"):
        raise ValueError("release workflow must run from an existing tag")
    tag = ref.removeprefix("refs/tags/")
    if args.tag is not None and args.tag != tag:
        raise ValueError("explicit tag disagrees with GITHUB_REF")
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("GHA run identity is required")
    return Context(False, tag, repository, run_id, attempt)


def overrides(args) -> dict:
    """Only explicitly supplied options override TOML."""
    keys = (
        "repository",
        "testpypi_username",
        "changelog",
        "allow_dirty",
        "final_release_branches",
        "pre_release_branches",
        "dev_release_branches",
        "github_release_publish",
        "github_release_types",
    )
    return {key: value for key in keys if (value := getattr(args, key, None)) is not None}


def check_source(root: Path, config, execution: Context, client: Client) -> tuple[dict, str | None]:
    """Refuse an existing index version before tag/GitHub diagnostics."""
    client.absent(config.repository, config.distribution, config.version)
    source = identity(root, config, execution)
    notes = release_notes(root, config)
    if not execution.local:
        if os.environ.get("GITHUB_SHA") != source["commit"]:
            raise ValueError("HEAD disagrees with GITHUB_SHA")
        client.check_tag(source["tag"], source["commit"])
        if config.github_requested:
            client.release_absent(source["tag"])
    return source, notes


def check_bundle_context(data: dict, execution: Context) -> None:
    """Bind downloaded bytes to this exact workflow run, including reruns."""
    original = data.get("context", {})
    if execution.local:
        return
    for key in ("github_repository", "run_id", "run_attempt"):
        if original.get(key) != getattr(execution, key):
            raise ValueError(f"bundle belongs to a different workflow {key}")
    if original.get("local") is not False:
        raise ValueError("local build bundles cannot be published by the GHA workflow")


def publish_github(args, execution: Context, client: Client) -> dict:
    """Gate announcement on exact index bytes; existing releases always fail."""
    data, config = read_bundle(args.bundle)
    check_bundle_context(data, execution)
    if not config.github_requested:
        return {"github_release": "skipped"}
    if data["source"]["tag"] != execution.tag or data["source"]["commit"] != os.environ.get(
        "GITHUB_SHA"
    ):
        raise ValueError("bundle source disagrees with workflow identity")
    client.check_tag(data["source"]["tag"], data["source"]["commit"])
    client.release_absent(data["source"]["tag"])
    expected = {
        name.removeprefix("dist/"): digest
        for name, digest in data["files"].items()
        if name.startswith("dist/")
    }
    client.verify_index(config.repository, config.distribution, config.version, expected)
    # Recheck tag and existence after bounded metadata waiting.
    client.check_tag(data["source"]["tag"], data["source"]["commit"])
    client.release_absent(data["source"]["tag"])
    if args.check_only:
        return {"checked": "publish-github-release"}
    notes = (
        (args.bundle / "notes.md").read_text(encoding="utf-8")
        if config.changelog is not False
        else ""
    )
    release = client.github(
        "releases",
        "POST",
        {
            "tag_name": data["source"]["tag"],
            "target_commitish": data["source"]["commit"],
            "name": data["source"]["tag"],
            "body": notes,
            "draft": False,
            "prerelease": config.group != "final",
            "make_latest": "true" if config.group == "final" else "false",
        },
    )
    if not isinstance(release, dict) or not isinstance(release.get("id"), int):
        raise ValueError("GitHub did not return a release identity")
    assets = [p for p in args.bundle.rglob("*") if p.is_file()]
    for path in sorted(assets):
        url = (
            f"https://uploads.github.com/repos/{execution.github_repository}"
            f"/releases/{release['id']}/assets?name={quote(path.name, safe='')}"
        )
        result = client.request(
            url,
            "POST",
            path.read_bytes(),
            github=True,
            content_type="application/octet-stream",
        )
        if not isinstance(result, dict) or result.get("state") != "uploaded":
            raise ValueError(
                f"GitHub asset upload did not complete: {path.name}; recovery is deferred"
            )
    return {"github_release": release.get("html_url"), "assets": len(assets)}


def execute(args) -> dict:
    """Run a selected command and return machine-readable result metadata."""
    if args.command == "workflow-template":
        content = files("nopt").joinpath("workflow.yml").read_text(encoding="utf-8")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(content)
            return {"workflow": str(args.output)}
        print(content, end="")
        return {}
    execution = context(args)
    client = Client(execution.github_repository)
    if args.command == "publish-github-release":
        return publish_github(args, execution, client)
    root = args.project.resolve()
    config = load(root, overrides(args))
    source, notes = check_source(root, config, execution, client)
    result = {
        "version": config.version,
        "distribution": config.distribution,
        "tag": source["tag"],
        "github_requested": config.github_requested,
    }
    if args.command == "build":
        validate_output(root, args.output)
    if args.command == "build" and not args.check_only:
        make_bundle(root, args.output, config, execution, source, notes)
        result["bundle"] = str(args.output.resolve())
    elif args.command == "publish-release-preflight":
        data, bundled_config = read_bundle(args.bundle)
        check_bundle_context(data, execution)
        if config != bundled_config:
            raise ValueError("bundle policy disagrees with checkout/CLI configuration")
        if source != data["source"]:
            raise ValueError("bundle source identity differs from current tag/working-tree status")
        if notes is not None and (args.bundle / "notes.md").read_text(encoding="utf-8") != notes:
            raise ValueError("bundle release notes differ from current source")
    output = os.environ.get("GITHUB_OUTPUT") if not execution.local else None
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    return result


def main(argv: list[str] | None = None) -> int:
    """Render clean diagnostics and return a failing status without leaking credentials."""
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
        URLError,
    ) as error:
        print(f"nopt: {error}", file=sys.stderr)
        return 1
    if result:
        print(json.dumps(result, sort_keys=True))
    return 0
