# gitws

[中文文档](README.zh-CN.md)

`gitws` is a small CLI for creating isolated Git workspaces from named local
repositories or remote URLs. It keeps its mirrors and workspaces outside the
original repository, pins each workspace to one commit, and never runs cleanup
commands against the original checkout.

`usage` reports logical bytes and physical bytes. Physical totals deduplicate
shared files, so shared files are not counted repeatedly.

## Commands

```text
gitws add <name> <path-or-url>
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

For a local path, `new` defaults to the source repository's current `HEAD` and
uses only local Git objects, initialized submodules, and available LFS objects.
It never fetches from the remote. The first workspace seeds an independent
Git object store from the local repository; untracked, ignored, and build files
in the source checkout are excluded. Use `--at` for another locally available
commit. Cached commits remain usable after the original directory is removed.

For a URL, `new` fetches remote updates and defaults to the remote's default
branch. HTTPS, SSH, and `file://` URLs are accepted. `sync` and `switch` apply
only to local path sources and explicitly change the registered checkout.
Offline local work requires the needed submodule and LFS objects to already be
present locally; missing objects are not downloaded.

The state directory defaults to `~/.gitws` and can be changed with
`GITWS_HOME`.

You can also choose it per command:

```bash
gitws --home /data/gitws new project task-a --cow
```

The `--home` option takes precedence over `GITWS_HOME`.

`--cow` is an optional Linux feature. Remove these workspaces with `gitws
remove` so their filesystem mounts can be unmounted cleanly.

## Disk usage

`gitws usage <name>` scans current files and prints each row as soon as it is
ready. It includes untracked files and build outputs in checkouts. For COW
workspaces, each shared base is scanned once per invocation, followed by each
workspace's write layer; the mounted full trees are not scanned again.

`logical` reports file lengths; `physical` reports allocated file blocks, with
hard links counted once across rows. Symbolic links are measured without
following their targets, and sparse files use their allocated blocks. Results
are not cached, so large trees still take time to scan; concurrent builds can
change files while a scan is running. Use `--bytes` for exact byte counts.

## Safety model

- `add` only records the local path or remote URL and does not modify the source.
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
| Shared Git objects | 0.92 GiB physical |
| Shared Git LFS objects | 0.63 GiB physical |
| Shared full tracked-tree base | 6.40 GiB physical |
| One workspace write layer | 0.0006 GiB physical |
| Additional COW workspace creation | about 2 min 08 s |

For five parallel workspaces, comparing task storage only and excluding the
already existing original checkout:

| Approach | Physical storage for five workspaces |
| --- | ---: |
| Five independent clones | about 39.75 GiB |
| `gitws --cow` | about 7.95 GiB |
| Saved by `gitws --cow` | about 31.80 GiB (80.0%) |

The independent-clone figure is `5 × (6.40 GiB tracked tree + 0.92 GiB Git
objects + 0.63 GiB LFS objects)`. The COW figure is one shared Git object
store, one shared LFS store, one shared 6.40 GiB base, and five small write
layers. The write-layer value is a measured sample; actual tasks consume more
or less space according to their changes. The original checkout also contained
ignored local worktrees and build files; those are not part of the tracked-tree
comparison. Clone network time depends on the remote and was not presented as
a fixed benchmark.

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
