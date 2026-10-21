# gitws

`gitws` is a small, local-only CLI for creating isolated Git workspaces from
named repositories. It keeps its mirrors and workspaces outside the original
repository, pins each workspace to one commit, and never runs cleanup commands
against the original checkout.

`size` reports logical bytes and physical bytes. Physical totals deduplicate
hard-linked files, so shared files are not counted repeatedly.

## Commands

```text
gitws add <name> <path>
gitws sync <name>
gitws switch <name> <commit>
gitws new <name> <workspace> [--at COMMIT]
gitws path <workspace>
gitws list
gitws remove <workspace>
gitws size <name>
gitws clean <name>
```

The state directory defaults to `~/.gitws` and can be changed with
`GITWS_HOME`.

## Safety model

- `add` only records the canonical path, remote URL, and branch.
- `sync` and `switch` are the only commands that modify the registered checkout.
- Both refuse dirty repositories and dirty submodules.
- `new` creates a detached worktree below the gitws state directory.
- Each initialized submodule uses its matching mirror as a Git reference store;
  submodule objects are shared while working files remain isolated per workspace.
- Removing a workspace only removes directories previously created by gitws.
- A workspace records its exact superproject commit and recursive submodule SHAs.

## Development

```bash
python -m gitws --help
python -m unittest discover -s tests -v
```

For local use without packaging, run `python -m gitws ...` from this directory.

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
