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

For a local source, use `new --latest` to fetch the upstream default branch into
the existing cache without switching the original checkout or existing tasks:

```bash
gitws new project task-b --latest
```

This also permits fetching required submodule and LFS data. `--latest` and
`--at` are mutually exclusive; either can be combined with `--cow`.

Use a different branch name for each task. Workspace `origin` points to the
registered upstream; local sources without an upstream have no `origin`.
Registering an existing alias with a different source is rejected.

## Commands

| Command | Purpose |
| --- | --- |
| `add <name> <path-or-url>` | Register a repository alias |
| `new <name> <workspace> [--at COMMIT \| --latest] [--cow]` | Create a workspace |
| `path <workspace>` | Print its path; fail if missing or not mounted |
| `list` | List repositories and workspaces |
| `status [name]` | Show commits and local changes |
| `usage <name> [--bytes]` | Show disk usage |
| `remove <workspace> [--force]` | Remove a workspace; `--force` discards local files |
| `mount <workspace>` | Restore a COW mount, for example after a reboot |
| `doctor <name>` | Check repository and workspace integrity |
| `clean <name> [--unused] [--dry-run]` | Prune stale records; optionally remove unused COW bases |
| `sync <name>` | Switch the original checkout to the current remote default branch |
| `switch <name> <commit>` | Switch the original checkout to a specific commit |

Only `sync` and `switch` modify the original checkout. They require a local
path source and refuse repositories with local changes.

`remove` refuses modified, untracked, or ignored files, including in submodules.
It also refuses hidden index entries (`assume-unchanged`/`skip-worktree`) and
ordinary submodules with private Git objects that would be deleted.
Use `--force` only after preserving needed work. For an unmounted COW workspace, mount it to
inspect its contents before removal, or explicitly discard it with `--force`.
Preserve needed commits before removing a workspace; removal is not a backup.

## Storage

The default directory is `~/.gitws`. Change it with:

```bash
export GITWS_HOME=/data/gitws
```

`gitws --home /data/gitws <command>` overrides `GITWS_HOME`.

Ordinary workspaces have separate source files. `new --cow` also shares a full
source tree and stores each workspace's changes separately. It requires
OverlayFS or `fuse-overlayfs`; use `gitws remove` to unmount and delete it.
After a reboot, run `gitws mount <workspace>` to restore the tree and its edits.

New COW bases also share regular files with identical content and compatible permissions
across commits, including recursive submodules and hydrated LFS files. Each version keeps
a complete directory tree; OverlayFS copies edited files into the task's own write layer.
Different paths within one tree never become hardlinks merely because their contents match.

Creation checks out and deduplicates bounded batches instead of allocating a full duplicate
tree first. A single large file or filter output still needs temporary space. Sharing is
limited to new-format bases in the same repository cache; files remain independent if
hardlinks are unavailable. Existing bases and workspaces retain their layout without
automatic migration or source hardlinks to new bases. The first use of the new layout
requires a new base. Develop in workspaces; do not edit internal base directories directly.

Unused bases remain cached for reuse and are included in `usage`, along with
submodule mirrors. Preview and reclaim unused bases with:

```bash
gitws clean project --unused --dry-run
gitws clean project --unused
```

Bases referenced by existing workspaces are retained, including unmounted ones.
Bases with locally stored submodule Git objects are also retained to protect task commits.
Git and LFS object caches are retained. Different projects can create workspaces
concurrently; operations on the same repository are serialized.

`usage` reports file lengths (`logical`) and allocated file blocks (`physical`),
including build outputs. Hard links count once toward physical usage.
Results appear row by row; `--bytes` displays exact byte counts.

Base rows also show:

| Field | Meaning |
| --- | --- |
| `exclusive` | Allocated file blocks referenced only within this base |
| `shared` | Allocated file blocks shared with paths outside this base; not additive across rows |
| `reclaimable` | Estimated exclusive file space released if this base can currently be cleaned; 0 while used or protected |
| `workspaces` | Workspaces referencing the base, including unmounted ones |

Each row's `physical` is its contribution to the deduplicated total, not the space freed
by deleting it. `reclaimable` excludes external Git metadata. Cleaning several bases may
also release shared files, so the per-base estimates are not additive.

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
