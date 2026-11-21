"""Public command line; checks always precede nopt-owned actions."""

import argparse
from collections.abc import Mapping
from importlib.resources import files
from importlib.metadata import version
import json
import os
from pathlib import Path
import re

from .bundle import (
    ReleaseManifest,
    build_release_bundle,
    read_and_verify_release_bundle,
    validate_build_output_path,
)
from .changelog import release_notes as extract_release_notes
from .config import Config, load
from .remote import GitHubClient, PyPIClient
from .source import Context, SourceIdentity, git, identity


def build_parser() -> argparse.ArgumentParser:
    """Build the agreed small CLI without speculative skip/force flags."""
    parser = argparse.ArgumentParser(
        prog="nopt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Nicks Opinionated (Python Package) Publishing Tool: "
        "check, build and publish releases.",
        epilog="Copyright (c) 2026 Nicholas Hairs.\n"
        "https://github.com/nhairs/nicks-opinionated-python-package-publishing-tool",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('nopt')}")
    commands = parser.add_subparsers(dest="command", required=True)
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
                "--bundle",
                dest="release_bundle",
                type=Path,
                required=True,
                help="original downloaded release bundle",
            )
        command.add_argument("--repository", choices=("pypi", "testpypi"), default=None)
        command.add_argument("--testpypi-username")
        command.add_argument("--changelog")
        command.add_argument("--no-changelog", action="store_const", const=False, dest="changelog")
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
    return parser


def build_context(args: argparse.Namespace) -> Context:
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


def get_cli_config_overrides(args: argparse.Namespace) -> dict[str, object]:
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
    index.absent(config.repository, config.distribution_name, config.version)
    source = identity(root, config, execution)
    release_notes = extract_release_notes(root, config)
    if not execution.local:
        if os.environ.get("GITHUB_SHA") != source["commit"]:
            raise ValueError("HEAD disagrees with GITHUB_SHA")
        github.check_tag(source["tag"], source["commit"])
        if config.github_release_requested:
            github.release_absent(source["tag"])
    return source, release_notes


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


def check_publish_bundle(
    release_bundle: Path,
    execution: Context,
    commit: str,
    policy: Mapping[str, object] | None = None,
) -> tuple[ReleaseManifest, Config]:
    """Verify the checked build handoff against independently supplied identity."""
    data, config = read_and_verify_release_bundle(release_bundle, policy)
    check_bundle_context(data, execution)
    if data["source"]["commit"] != commit:
        raise ValueError("bundle source disagrees with current commit")
    if execution.tag is not None and data["source"]["tag"] != execution.tag:
        raise ValueError("bundle source disagrees with current tag")
    return data, config


def publish_preflight(
    args: argparse.Namespace, execution: Context, index: PyPIClient, github: GitHubClient
) -> dict[str, str | bool]:
    """Trust checked bundle policy and repeat only mutable external-system checks."""
    commit = (
        git(args.project.resolve(), "rev-parse", "HEAD^{commit}")
        if execution.local
        else os.environ.get("GITHUB_SHA", "")
    )
    data, config = check_publish_bundle(
        args.release_bundle, execution, commit, get_cli_config_overrides(args)
    )
    index.absent(config.repository, config.distribution_name, config.version)
    if not execution.local:
        github.check_tag(data["source"]["tag"], data["source"]["commit"])
        if config.github_release_requested:
            github.release_absent(data["source"]["tag"])
    return {
        "version": config.version,
        "distribution_name": config.distribution_name,
        "tag": data["source"]["tag"],
        "github_release_requested": config.github_release_requested,
    }


def publish_github(
    args: argparse.Namespace, execution: Context, index: PyPIClient, github: GitHubClient
) -> dict[str, str | int | bool]:
    """Gate announcement on exact index bytes; existing releases always fail."""
    ### Validate the original bundle and workflow identity before network checks.
    data, config = check_publish_bundle(
        args.release_bundle,
        execution,
        os.environ.get("GITHUB_SHA", ""),
        get_cli_config_overrides(args),
    )
    if not config.github_release_requested:
        return {"github_release": "skipped"}
    github.check_tag(data["source"]["tag"], data["source"]["commit"])
    github.release_absent(data["source"]["tag"])
    expected = {
        name.removeprefix("dist/"): digest
        for name, digest in data["files"].items()
        if name.startswith("dist/")
    }
    ### Gate GitHub publication on exact index filenames and hashes.
    index.verify_index(config.repository, config.distribution_name, config.version, expected)
    # Recheck tag and existence after bounded metadata waiting.
    github.check_tag(data["source"]["tag"], data["source"]["commit"])
    github.release_absent(data["source"]["tag"])
    latest = github.is_latest_final(config.version)
    if args.check_only:
        return {"checked": "publish-github-release", "latest": latest}
    release_notes = (
        (args.release_bundle / "release-notes.md").read_text(encoding="utf-8")
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
            "body": release_notes,
            "draft": False,
            "prerelease": config.release_type != "final",
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
    assets = [p for p in args.release_bundle.rglob("*") if p.is_file()]
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
    execution = build_context(args)
    github = GitHubClient(execution.github_repository)
    if args.command == "publish-github-release":
        return publish_github(args, execution, PyPIClient(), github)
    if args.command == "publish-release-preflight":
        return publish_preflight(args, execution, PyPIClient(), github)
    root = args.project.resolve()
    config = load(root, get_cli_config_overrides(args))
    source, release_notes = check_source(root, config, execution, PyPIClient(), github)
    result: dict[str, str | int | bool] = {
        "version": config.version,
        "distribution_name": config.distribution_name,
        "tag": source["tag"],
        "github_release_requested": config.github_release_requested,
    }
    if args.command == "build":
        validate_build_output_path(root, args.output)
    if args.command == "build" and not args.check_only:
        build_release_bundle(root, args.output, config, execution, source, release_notes)
        result["bundle"] = str(args.output.resolve())
    output = os.environ.get("GITHUB_OUTPUT") if not execution.local else None
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in result.items():
                stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    return result


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and preserve exception tracebacks for failures."""
    args = build_parser().parse_args(argv)
    result = execute(args)
    if result:
        print(json.dumps(result, sort_keys=True))
    return 0
