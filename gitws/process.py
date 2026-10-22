"""Run external commands without a shell and normalize execution errors."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import GitwsError


def run_command(
    *args: str, cwd: Path | None = None, check: bool = True, env: dict[str, str] | None = None
) -> str:
    try:
        process = subprocess.run(
            list(args), cwd=cwd, env=env, text=True, capture_output=True, check=False
        )
    except OSError as exc:
        raise GitwsError(f"cannot run {args[0]} in {cwd or Path.cwd()}: {exc}") from exc
    if check and process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise GitwsError(f"{' '.join(args)} failed: {detail}")
    return process.stdout.strip()
