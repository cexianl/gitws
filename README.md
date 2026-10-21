# gitws

[中文文档](README.zh-CN.md)

`gitws` is a small, local-only CLI for creating isolated Git workspaces from
named repositories. It keeps its mirrors and workspaces outside the original
repository, pins each workspace to one commit, and never runs cleanup commands
against the original checkout.

`usage` reports logical bytes and physical bytes. Physical totals deduplicate
shared files, so shared files are not counted repeatedly.

## Commands

```text
gitws add <name> <path>
gitws sync <name>
gitws switch <name> <commit>
gitws new <name> <workspace> [--at COMMIT] [--cow]
gitws path <workspace>
gitws list
gitws status [name]
gitws remove <workspace>
gitws usage <name>
gitws doctor <name>
gitws clean <name>
```

The state directory defaults to `~/.gitws` and can be changed with
`GITWS_HOME`.

You can also choose it per command:

```bash
gitws --home /data/gitws new project task-a --cow
```

The `--home` option takes precedence over `GITWS_HOME`.

`--cow` is an optional Linux feature. Remove these workspaces with `gitws
remove` so their filesystem mounts can be unmounted cleanly.

## Safety model

- `add` only records the canonical path, remote URL, and branch.
- `sync` and `switch` are the only commands that modify the registered checkout.
- Both refuse dirty repositories and dirty submodules.
- `new` creates a detached worktree below the gitws state directory.
- Each initialized submodule uses its matching mirror as a Git reference store;
  submodule objects are shared while working files remain isolated per workspace.
- When Git LFS is installed, workspaces for the same repository use one shared
  LFS object store; each workspace still has its own working files.
- `new --cow` creates a full-tree copy-on-write workspace with OverlayFS. It
  uses `fuse-overlayfs` when a normal mount is not available.
- Removing a workspace only removes directories previously created by gitws.
- A workspace records its exact superproject commit and recursive submodule SHAs.

## gitws and native `git worktree`

`gitws` uses native Git worktrees internally. It adds repository aliases,
external mirrors, recursive submodule handling, status, disk usage, and Linux
file locks for concurrent CLI processes. `git worktree` provides the worktree
primitive but leaves those surrounding concerns to the user.

`gitws` does not replace Git. It calls native `git`, `git-lfs`, `worktree`, and
`submodule` commands. Workspace source files remain separate so tasks can edit
them independently; Git, submodule, and (when available) Git LFS objects are
shared.

## Measured example

An anonymized large repository with many recursive submodules was measured on
Linux using `new --cow`:

| Item | Measured value |
| --- | ---: |
| Initial setup, including mirrors and LFS | 11 min 23 s |
| Additional COW workspace | 2 min 08 s |
| Shared Git objects | 824.85 MiB physical |
| Shared Git LFS objects | 628.47 MiB physical |
| Shared full-tree base | 4.00 GiB physical |
| One workspace write layer | 304 KiB physical |

For five parallel workspaces, comparing task storage only and excluding the
already existing original checkout:

| Approach | Physical storage for five workspaces |
| --- | ---: |
| Five independent clones | about 87.1 GiB |
| `gitws --cow` | about 5.43 GiB |
| Saved by `gitws --cow` | about 81.7 GiB (93.8%) |

The independent-clone figure is `5 × (16.01 GiB source + 0.81 GiB Git
objects + 0.61 GiB LFS objects)`. The COW figure is one shared Git object
store, one shared LFS store, one shared 4.00 GiB base, and five 304 KiB write
layers. The write-layer value is a measured sample; actual tasks consume more
or less space according to their changes. Clone network time depends on the
remote and was not used as a fixed benchmark.

## Install

Install the current GitHub version with pip:

```bash
python3 -m pip install "git+https://github.com/cexianl/gitws.git"
gitws --help
```

On systems with an externally managed Python installation, use a virtual
environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
ln -sfn "$PWD/.venv/bin/gitws" "$HOME/.local/bin/gitws"
```

Make sure `$HOME/.local/bin` is in `PATH`, then run `gitws --help` anywhere.
