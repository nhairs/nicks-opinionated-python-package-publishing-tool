"""Public command line; checks always precede nopt-owned actions."""

import argparse
from collections.abc import Mapping
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile
from urllib.error import URLError

from .bundle import make_bundle, read_bundle, validate_output
from .changelog import release_notes
from .config import Config, load
from .remote import GitHubClient, PyPIClient
from .source import Context, SourceIdentity, identity


def parser() -> argparse.ArgumentParser:
    """Build the agreed small CLI without speculative skip/force flags."""
    result = argparse.ArgumentParser(prog="nopt")
    commands = result.add_subparsers(dest="command", required=True)
    ### Release workflow commands share policy overrides and explicit execution mode.
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
        if name != "publish-github-release":
            command.add_argument(
                "--tag", help="local-only existing tag pointing to HEAD; inferred from version"
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
    ### Template generation does not require workflow context.
    template = commands.add_parser(
        "workflow-template", help="Print or write an editable GHA release workflow"
    )
    template.add_argument("--output", type=Path, help="new file; never overwrite an existing file")
    return result


def context(args: argparse.Namespace) -> Context:
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
    if getattr(args, "tag", None) is not None:
        raise ValueError("--tag is local-only; GHA reads GITHUB_REF")
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError("GHA run identity is required")
    return Context(False, tag, repository, run_id, attempt)


def overrides(args: argparse.Namespace) -> dict[str, object]:
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


def check_source(
    root: Path, config: Config, execution: Context, index: PyPIClient, github: GitHubClient
) -> tuple[SourceIdentity, str | None]:
    """Refuse an existing index version before tag/GitHub diagnostics."""
    index.absent(config.repository, config.distribution, config.version)
    source = identity(root, config, execution)
    notes = release_notes(root, config)
    if not execution.local:
        if os.environ.get("GITHUB_SHA") != source["commit"]:
            raise ValueError("HEAD disagrees with GITHUB_SHA")
        github.check_tag(source["tag"], source["commit"])
        if config.github_requested:
            github.release_absent(source["tag"])
    return source, notes


def check_bundle_context(data: Mapping[str, object], execution: Context) -> None:
    """Bind downloaded bytes to this exact workflow run, including reruns."""
    original = data.get("context")
    if not isinstance(original, dict):
        raise ValueError("bundle context must be an object")
    if execution.local:
        return
    for key in ("github_repository", "run_id", "run_attempt"):
        if original.get(key) != getattr(execution, key):
            raise ValueError(f"bundle belongs to a different workflow {key}")
    if original.get("local") is not False:
        raise ValueError("local build bundles cannot be published by the GHA workflow")
    return


def publish_github(
    args: argparse.Namespace, execution: Context, index: PyPIClient, github: GitHubClient
) -> dict[str, str | int | bool]:
    """Gate announcement on exact index bytes; existing releases always fail."""
    ### Validate the original bundle and workflow identity before network checks.
    data, config = read_bundle(args.bundle)
    check_bundle_context(data, execution)
    if not config.github_requested:
        return {"github_release": "skipped"}
    if data["source"]["tag"] != execution.tag or data["source"]["commit"] != os.environ.get(
        "GITHUB_SHA"
    ):
        raise ValueError("bundle source disagrees with workflow identity")
    github.check_tag(data["source"]["tag"], data["source"]["commit"])
    github.release_absent(data["source"]["tag"])
    expected = {
        name.removeprefix("dist/"): digest
        for name, digest in data["files"].items()
        if name.startswith("dist/")
    }
    ### Gate GitHub publication on exact index filenames and hashes.
    index.verify_index(config.repository, config.distribution, config.version, expected)
    # Recheck tag and existence after bounded metadata waiting.
    github.check_tag(data["source"]["tag"], data["source"]["commit"])
    github.release_absent(data["source"]["tag"])
    latest = github.is_latest_final(config.version)
    if args.check_only:
        return {"checked": "publish-github-release", "latest": latest}
    notes = (
        (args.bundle / "notes.md").read_text(encoding="utf-8")
        if config.changelog is not False
        else ""
    )
    ### Create the release once; existing-release recovery remains deferred.
    release = github.github(
        "releases",
        "POST",
        {
            "tag_name": data["source"]["tag"],
            "target_commitish": data["source"]["commit"],
            "name": data["source"]["tag"],
            "body": notes,
            "draft": False,
            "prerelease": config.group != "final",
            "make_latest": "true" if latest else "false",
        },
    )
    if not isinstance(release, dict):
        raise ValueError("GitHub did not return a release identity")
    release_id = release.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool):
        raise ValueError("GitHub did not return an integer release identity")
    release_url = release.get("html_url")
    if not isinstance(release_url, str):
        raise ValueError("GitHub did not return a release URL")
    ### Attach only the verified original files.
    assets = [p for p in args.bundle.rglob("*") if p.is_file()]
    for path in sorted(assets):
        github.upload_asset(release_id, path)
    return {"github_release": release_url, "assets": len(assets)}


def execute(args: argparse.Namespace) -> dict[str, str | int | bool]:
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
    github = GitHubClient(execution.github_repository)
    if args.command == "publish-github-release":
        return publish_github(args, execution, PyPIClient(), github)
    root = args.project.resolve()
    config = load(root, overrides(args))
    source, notes = check_source(root, config, execution, PyPIClient(), github)
    result: dict[str, str | int | bool] = {
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
