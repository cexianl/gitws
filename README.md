# gitws

[中文](README.zh-CN.md)

Create isolated workspaces for parallel development. Share Git and LFS objects,
including submodules, and optionally source files to reduce disk usage.

## Install

Requires Linux, Python 3.10+, and Git. Install Git LFS for repositories that use it.

```bash
python3 -m pip install "git+https://github.com/cexianl/gitws.git"
gitws --help
```

If your system restricts pip installation, use `pipx install "git+https://github.com/cexianl/gitws.git"`.

## Usage

```bash
gitws add project /path/to/project
gitws new project task-a
cd "$(gitws path task-a)"
git switch -c task-a
```

Register a remote URL with `gitws add upstream https://github.com/example/project.git`.
HTTPS, SSH, and `file://` URLs are supported.

| Source | Default commit | Data source |
| --- | --- | --- |
| Local path | Local `HEAD` | Local objects only; required submodules and LFS objects must be present |
| Remote URL | Latest commit on the remote default branch | Fetches remote updates |

`new --at <commit>` selects a specific commit. Workspaces start with a detached
HEAD and pin recursive submodule commits. Later source updates do not change
existing workspaces. Uncommitted edits, untracked files, and build outputs are
not copied from the source.

## Commands

| Command | Purpose |
| --- | --- |
| `add <name> <path-or-url>` | Register a repository alias |
| `new <name> <workspace> [--at COMMIT] [--cow]` | Create a workspace |
| `path <workspace>` | Print its path |
| `list` | List repositories and workspaces |
| `status [name]` | Show commits and local changes |
| `usage <name> [--bytes]` | Show disk usage |
| `remove <workspace>` | Remove a workspace, including its local changes |
| `doctor <name>` | Check repository and workspace integrity |
| `clean <name>` | Prune stale workspace records |
| `sync <name>` | Switch the original checkout to the latest remote branch commit |
| `switch <name> <commit>` | Switch the original checkout to a specific commit |

Only `sync` and `switch` modify the original checkout. They require a local
path source and refuse repositories with local changes.

## Storage

The default directory is `~/.gitws`. Change it with:

```bash
export GITWS_HOME=/data/gitws
```

`gitws --home /data/gitws <command>` overrides `GITWS_HOME`.

Ordinary workspaces have separate source files. `new --cow` also shares a full
source tree and stores each workspace's changes separately. It requires
OverlayFS or `fuse-overlayfs`; use `gitws remove` to unmount and delete it.

`usage` reports file lengths (`logical`) and allocated file blocks (`physical`),
including build outputs. Hard links count once toward physical usage.
Results appear row by row; `--bytes` displays exact byte counts.

## Compared with `git worktree`

| Capability | `git worktree` | `gitws` |
| --- | --- | --- |
| Share main repository Git objects | Yes | Yes |
| Repository aliases | No | Yes |
| Initialize recursive submodules | Manual | Automatic |
| Share submodule and LFS objects | Manual configuration | Automatic |
| Share source files | No | `--cow` |
| Disk usage report | Separate tool | `usage` |

## Disk savings example

Measurements from a large repository with recursive submodules on Linux:
Git objects **0.92 GiB**, LFS objects **0.63 GiB**, and checked-out source
**6.40 GiB**, all measured as physical usage. One COW write layer used
**0.0006 GiB**; creating another workspace with an existing base took about
**2 min 08 s**.

Estimated storage for **five workspaces at the same commit**:

| Approach | Physical usage |
| --- | ---: |
| Five independent clones | 39.75 GiB |
| Five `gitws --cow` workspaces | 7.95 GiB |
| Savings | **31.80 GiB (80%)** |

Calculation: independent clones use `5 × (0.92 + 0.63 + 6.40)` GiB; COW uses
`0.92 + 0.63 + 6.40 + 5 × 0.0006` GiB. This excludes the original checkout
and build outputs. Task edits and builds add to each workspace's usage.
