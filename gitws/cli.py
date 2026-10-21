from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import GitwsError, Store, ensure_clean, ensure_repo, run_git


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
    parser.add_argument("--home", type=Path,
                        help="store mirrors and workspaces under this directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="register a local repository")
    p.add_argument("name")
    p.add_argument("path", type=Path)

    p = sub.add_parser("sync", help="switch the registered checkout to remote latest")
    p.add_argument("name")
    p = sub.add_parser("switch", help="switch the registered checkout to a commit")
    p.add_argument("name")
    p.add_argument("commit")

    p = sub.add_parser("new", help="create an isolated workspace")
    p.add_argument("name")
    p.add_argument("workspace")
    p.add_argument("--at", dest="commit")
    p.add_argument("--cow", action="store_true",
                   help="use a copy-on-write OverlayFS workspace")

    sub.add_parser("list", help="list repositories and workspaces")
    p = sub.add_parser("status", help="show repository and workspace status")
    p.add_argument("name", nargs="?", help="repository alias")
    p = sub.add_parser("path", help="print a workspace path")
    p.add_argument("workspace")
    p = sub.add_parser("remove", help="remove a workspace")
    p.add_argument("workspace")
    p = sub.add_parser("usage", help="show repository disk usage")
    p.add_argument("name")
    p.add_argument("--bytes", action="store_true", help="show exact byte counts")
    p = sub.add_parser("doctor", help="check repository and workspace integrity")
    p.add_argument("name")
    p = sub.add_parser("clean", help="prune stale records and mirror data")
    p.add_argument("name")
    return parser


def main(argv: list[str] | None = None) -> None:
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
    store = Store(args.home)
    try:
        if args.command == "add":
            repo = store.add(args.name, args.path)
            print(f"added {repo.name}: {repo.path}")
            return
        if args.command == "list":
            for repo in store.all_repos().values():
                print(f"{repo.name}  {repo.path}")
                items = store.workspaces(repo.name)
                if not items:
                    print("  (no workspaces)")
                for item in items:
                    print(f"  {item['name']}  {item['commit'][:12]}  {item['path']}")
            return
        if args.command == "status":
            repos = store.all_repos()
            if args.name:
                repos = {args.name: store.get(args.name)}
            for repo in repos.values():
                print(f"{repo.name}  {repo.path}")
                try:
                    commit = run_git("rev-parse", "--short", "HEAD", cwd=repo.path)
                    dirty = bool(run_git("status", "--porcelain", "--ignore-submodules=none", cwd=repo.path))
                    print(f"  original  {commit}  {'modified' if dirty else 'clean'}")
                except GitwsError as exc:
                    print(f"  original  unavailable ({exc})")
                items = store.workspaces(repo.name)
                if not items:
                    print("  (no workspaces)")
                for item in items:
                    path = Path(item["path"])
                    if item.get("mode") == "cow" and not store.is_mounted(path):
                        current = item["commit"][:12]
                        state = "not mounted"
                    elif path.exists():
                        try:
                            current = run_git("rev-parse", "--short", "HEAD", cwd=path)
                            dirty = bool(run_git("status", "--porcelain", "--ignore-submodules=none", cwd=path))
                            state = "modified" if dirty else "clean"
                        except GitwsError:
                            current = item["commit"][:12]
                            state = "unavailable"
                    else:
                        current = item["commit"][:12]
                        state = "missing"
                    print(f"  {item['name']}  {current}  {state}  {path}")
            return
        if args.command == "path":
            items = {item["name"]: item for item in store.workspaces()}
            item = items.get(args.workspace)
            if not item:
                raise GitwsError(f"unknown workspace: {args.workspace}")
            print(item["path"])
            return
        if args.command == "remove":
            store.remove_workspace(args.workspace)
            print(f"removed {args.workspace}")
            return

        repo = store.get(args.name)
        if args.command == "sync":
            with store.locked("state"):
                ensure_clean(repo.path)
                store.sync_mirror(repo)
                commit = store.latest(repo)
                run_git("switch", "--detach", commit, cwd=repo.path)
                store.update_submodules(repo.path)
            print(commit)
        elif args.command == "switch":
            with store.locked("state"):
                ensure_clean(repo.path)
                store.sync_mirror(repo)
                run_git("cat-file", "-e", f"{args.commit}^{{commit}}", cwd=repo.mirror)
                run_git("switch", "--detach", args.commit, cwd=repo.path)
                store.update_submodules(repo.path)
            print(args.commit)
        elif args.command == "new":
            item = store.create_workspace(repo, args.workspace, args.commit, args.cow)
            print(item["path"])
        elif args.command == "usage":
            for key, value in store.usage(repo).items():
                if args.bytes:
                    logical = str(value["logical"])
                    physical = str(value["physical"])
                else:
                    logical = human_size(value["logical"])
                    physical = human_size(value["physical"])
                label = key.removeprefix("workspace:")
                print(f"{label}\tlogical={logical}\tphysical={physical}")
        elif args.command == "doctor":
            problems = store.doctor(repo)
            if problems:
                for problem in problems:
                    print(f"problem: {problem}")
                raise SystemExit(1)
            print(f"ok {repo.name}")
        elif args.command == "clean":
            store.clean(repo)
            print(f"cleaned {repo.name}")
    except GitwsError as exc:
        raise SystemExit(f"gitws: error: {exc}") from exc

