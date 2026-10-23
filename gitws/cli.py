"""Command-line argument parsing and presentation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import Store
from .errors import GitwsError
from .git import run_git
from .models import Repo


def human_size(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{value} B"
            return f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{value} B"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitws", description="Small Git workspace manager")
    parser.add_argument(
        "--home", type=Path, help="store mirrors and workspaces under this directory"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="register a local repository or remote URL")
    p.add_argument("name")
    p.add_argument("source")

    p = sub.add_parser(
        "sync",
        help="switch the original checkout to the remote default branch",
        description="Switch a clean original local checkout to the remote default branch. "
        "To create a task there without switching the original, use new --latest. "
        "URL sources fetch automatically during new.",
    )
    p.add_argument("name")
    p = sub.add_parser(
        "switch",
        help="switch the original checkout to a commit",
        description="Switch a clean original local checkout to a specific commit. "
        "Existing task workspaces keep their own commits.",
    )
    p.add_argument("name")
    p.add_argument("commit")

    p = sub.add_parser(
        "new",
        help="create an isolated workspace",
        description="Local sources default to local HEAD without remote access. "
        "URL sources fetch the remote default branch. --latest uses the upstream "
        "default branch while preserving the original checkout and existing tasks.",
    )
    p.add_argument("name")
    p.add_argument("workspace")
    revision = p.add_mutually_exclusive_group()
    revision.add_argument("--at", dest="commit", help="start at a specified commit")
    revision.add_argument(
        "--latest",
        action="store_true",
        help="fetch the upstream default branch into shared storage",
    )
    p.add_argument("--cow", action="store_true", help="use a copy-on-write OverlayFS workspace")

    sub.add_parser("list", help="list repositories and workspaces")
    p = sub.add_parser("status", help="show repository and workspace status")
    p.add_argument("name", nargs="?", help="repository alias")
    p = sub.add_parser(
        "path",
        help="print a usable workspace path",
        description="Print the path of an available workspace. Missing directories "
        "and unmounted COW workspaces fail; restore a COW workspace with mount first.",
    )
    p.add_argument("workspace")
    p = sub.add_parser(
        "remove",
        help="remove a workspace; refuse local or ignored files",
        description="Refuse modified, untracked or ignored files, including in submodules. "
        "Also protect hidden index entries and ordinary submodules' private Git objects. "
        "Mount COW workspaces before checking them. --force discards workspace files "
        "without these checks; preserve needed commits before removal.",
    )
    p.add_argument("workspace")
    p.add_argument("--force", action="store_true", help="discard all workspace files and edits")
    p = sub.add_parser("mount", help="restore a COW workspace mount")
    p.add_argument("workspace")
    p = sub.add_parser("usage", help="show repository disk usage")
    p.add_argument("name")
    p.add_argument("--bytes", action="store_true", help="show exact byte counts")
    p = sub.add_parser("doctor", help="check repository and workspace integrity")
    p.add_argument("name")
    p = sub.add_parser("clean", help="prune stale records and optionally unused bases")
    p.add_argument("name")
    p.add_argument("--unused", action="store_true", help="remove unused COW bases")
    p.add_argument("--dry-run", action="store_true", help="preview without removing data")
    return parser


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    # Accept --home before or after the subcommand, preserving the last value.
    raw = list(sys.argv[1:] if argv is None else argv)
    homes: list[Path] = []
    filtered: list[str] = []
    index = 0
    while index < len(raw):
        if raw[index] == "--home":
            if index + 1 >= len(raw):
                raise SystemExit("gitws: error: --home requires a directory")
            homes.append(Path(raw[index + 1]))
            index += 2
            continue
        filtered.append(raw[index])
        index += 1
    args = build_parser().parse_args(filtered)
    if homes:
        args.home = homes[-1]
    return args


def _checkout_status(path: Path) -> tuple[str, str]:
    commit = run_git("rev-parse", "--short", "HEAD", cwd=path)
    dirty = bool(run_git("status", "--porcelain", "--ignore-submodules=none", cwd=path))
    return commit, "modified" if dirty else "clean"


def _show_list(store: Store) -> None:
    for repo in store.all_repos().values():
        print(f"{repo.name}  {repo.path or repo.remote}")
        items = store.workspaces(repo.name)
        if not items:
            print("  (no workspaces)")
        for item in items:
            print(f"  {item['name']}  {item['commit'][:12]}  {item['path']}")


def _show_status(store: Store, name: str | None) -> None:
    repos = {name: store.get(name)} if name else store.all_repos()
    for repo in repos.values():
        print(f"{repo.name}  {repo.path or repo.remote}")
        if repo.path:
            try:
                commit, state = _checkout_status(repo.path)
                print(f"  original  {commit}  {state}")
            except GitwsError as exc:
                print(f"  original  unavailable ({exc})")
        items = store.workspaces(repo.name)
        if not items:
            print("  (no workspaces)")
        for item in items:
            path = Path(item["path"])
            current = item["commit"][:12]
            if item.get("mode") == "cow" and not store.is_mounted(path):
                state = "not mounted"
            elif not path.exists():
                state = "missing"
            else:
                try:
                    current, state = _checkout_status(path)
                except GitwsError:
                    state = "unavailable"
            print(f"  {item['name']}  {current}  {state}  {path}")


def _show_usage(store: Store, repo: Repo, raw_bytes: bool) -> None:
    for key, value in store.iter_usage(repo):
        format_size = str if raw_bytes else human_size
        logical = format_size(value["logical"])
        physical = format_size(value["physical"])
        label = key.removeprefix("workspace:")
        fields = [label, f"logical={logical}", f"physical={physical}"]
        for field in ("exclusive", "shared", "reclaimable"):
            if field in value:
                fields.append(f"{field}={format_size(value[field])}")
        if "workspaces" in value:
            fields.append(f"workspaces={value['workspaces']}")
        print("\t".join(fields), flush=True)


def _execute(store: Store, args: argparse.Namespace) -> None:
    if args.command == "add":
        repo = store.add(args.name, args.source)
        print(f"added {repo.name}: {repo.path or repo.remote}")
        return
    if args.command == "list":
        _show_list(store)
        return
    if args.command == "status":
        _show_status(store, args.name)
        return
    if args.command == "path":
        print(store.workspace_path(args.workspace))
        return
    if args.command == "remove":
        store.remove_workspace(args.workspace, force=args.force)
        print(f"removed {args.workspace}")
        return
    if args.command == "mount":
        print(store.mount_workspace(args.workspace))
        return

    repo = store.get(args.name)
    if args.command == "sync":
        print(store.switch_source(repo))
    elif args.command == "switch":
        print(store.switch_source(repo, args.commit))
    elif args.command == "new":
        item = store.create_workspace(
            repo, args.workspace, args.commit, args.cow, latest=args.latest
        )
        print(item["path"])
    elif args.command == "usage":
        _show_usage(store, repo, args.bytes)
    elif args.command == "doctor":
        problems = store.doctor(repo)
        if problems:
            for problem in problems:
                print(f"problem: {problem}")
            raise SystemExit(1)
        print(f"ok {repo.name}")
    elif args.command == "clean":
        for path in store.clean(repo, args.unused, args.dry_run):
            print(f"{'would remove' if args.dry_run else 'removed'} {path}")
        if not args.dry_run:
            print(f"cleaned {repo.name}")


def main(argv: list[str] | None = None) -> None:
    args = _parse_arguments(argv)
    try:
        _execute(Store(args.home), args)
    except (GitwsError, OSError) as exc:
        raise SystemExit(f"gitws: error: {exc}") from exc
