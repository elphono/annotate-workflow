#!/usr/bin/env python3
"""Claude Code PostToolUse hook (matcher `Write|Edit`): register the HTML
documents a session writes under a `docs/` folder of the workspace.

This is what makes the loop automatic: the session that writes
`~/workspace/<repo>/docs/x.html` gets it registered with ITS session id and
working directory, so that the notes the user pins on it later come back to
that very session.

Reads the hook JSON on stdin (`session_id`, `cwd`, `tool_input.file_path`).
Calls `annotate register <path> --session <id> --cwd <cwd>`, found on the
PATH first, then in the virtualenv of this repository.

**It never fails the session's tool call**: every error is swallowed, the
exit code is always 0, the registration is bounded to 10 s.

**It ignores the sessions annotate itself starts** (`ANNOTATE_SESSION=1`,
set by `annotate.claude.session_env`): a session answering notes edits the
document, and re-registering it from there would reset its status while the
daemon is settling it. Same loop as remarkable-sync's `RMSYNC_TOUR`.

The workspace is `$ANNOTATE_WORKSPACE`, or `~/workspace`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SESSION_MARKER = "ANNOTATE_SESSION"


def workspace() -> Path:
    return Path(os.environ.get("ANNOTATE_WORKSPACE") or Path.home() / "workspace")


def annotate_command() -> list[str] | None:
    found = shutil.which("annotate")
    if found:
        return [found]
    local = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "annotate"
    return [str(local)] if local.is_file() else None


def wanted(path: Path, root: Path) -> bool:
    """An .html file with a `docs` folder between the workspace and itself."""
    if path.suffix.lower() != ".html":
        return False
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return "docs" in relative.parts[:-1]


def main() -> int:
    if os.environ.get(SESSION_MARKER):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    tool_input = payload.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path:
        return 0
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(cwd or ".") / path
    if not wanted(path, workspace()):
        return 0
    command = annotate_command()
    if command is None:
        return 0
    argv = [*command, "register", str(path)]
    session = payload.get("session_id")
    if isinstance(session, str) and session:
        argv += ["--session", session]
    if cwd:
        argv += ["--cwd", cwd]
    try:
        subprocess.run(argv, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
