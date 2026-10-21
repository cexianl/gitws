from __future__ import annotations

import argparse
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

    sub.add_parser("list", help="list repositories and workspaces")
    p = sub.add_parser("path", help="print a workspace path")
    p.add_argument("workspace")
    p = sub.add_parser("remove", help="remove a workspace")
    p.add_argument("workspace")
    p = sub.add_parser("size", help="show repository disk usage")
    p.add_argument("name")
    p.add_argument("--bytes", action="store_true", help="show exact byte counts")
    p = sub.add_parser("clean", help="prune stale records and mirror data")
    p.add_argument("name")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    store = Store()
    try:
        if args.command == "add":
            repo = store.add(args.name, args.path)
            print(f"added {repo.name}: {repo.path}")
            return
        if args.command == "list":
            for repo in store.all_repos().values():
                print(f"repo {repo.name} {repo.path}")
                for item in store.workspaces(repo.name):
                    print(f"  ws {item['name']} {item['commit'][:12]} {item['path']}")
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
            ensure_clean(repo.path)
            store.sync_mirror(repo)
            commit = store.latest(repo)
            run_git("switch", "--detach", commit, cwd=repo.path)
            store.update_submodules(repo.path)
            print(commit)
        elif args.command == "switch":
            ensure_clean(repo.path)
            store.sync_mirror(repo)
            run_git("cat-file", "-e", f"{args.commit}^{{commit}}", cwd=repo.mirror)
            run_git("switch", "--detach", args.commit, cwd=repo.path)
            store.update_submodules(repo.path)
            print(args.commit)
        elif args.command == "new":
            item = store.create_workspace(repo, args.workspace, args.commit)
            print(item["path"])
        elif args.command == "size":
            for key, value in store.size(repo).items():
                if args.bytes:
                    logical = str(value["logical"])
                    physical = str(value["physical"])
                else:
                    logical = human_size(value["logical"])
                    physical = human_size(value["physical"])
                print(f"{key}\tlogical={logical}\tphysical={physical}")
        elif args.command == "clean":
            store.clean(repo)
            print(f"cleaned {repo.name}")
    except GitwsError as exc:
        raise SystemExit(f"gitws: error: {exc}") from exc

