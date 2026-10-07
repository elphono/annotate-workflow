#!/usr/bin/env python3
"""Claude Code PostToolUse hook (matcher `Write|Edit`): register the HTML
documents a session writes in a folder the daemon tracks.

This is what makes the loop automatic: the session that writes
`~/workspace/<repo>/docs/x.html` gets it registered with ITS session id and
working directory, so that the notes the user pins on it later come back to
that very session.

Reads the hook JSON on stdin (`session_id`, `cwd`, `tool_input.file_path`).
For every `.html` file, calls `annotate register <path> --session <id> --cwd
<cwd> --hook`, found on the PATH first, then in the virtualenv of this
repository. **What is a document is decided there**, by the tracked folders
the user chose (src/annotate/folders.py), not here: since 2026-10-07 the
folders change from the index page, and a copy of the rule in this file
would go stale. The command prints an `additionalContext` that tells the
session to start `annotate wait <id>` in the background, unless a session
already listens for that document; this hook forwards it. That background
command is what brings the user's notes back into the open conversation
(see src/annotate/listeners.py).

**It never fails the session's tool call**: every error is swallowed, the
exit code is always 0, the registration is bounded to 10 s, and anything
that is not a JSON object is dropped rather than forwarded.

A session answering notes edits the document, and is registered again:
that is how `delivered` becomes `answered` (registry.register).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def annotate_command() -> list[str] | None:
    found = shutil.which("annotate")
    if found:
        return [found]
    local = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "annotate"
    return [str(local)] if local.is_file() else None


def main() -> int:
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
    if path.suffix.lower() != ".html":
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
    argv.append("--hook")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return 0
    try:
        answer = json.loads(done.stdout) if done.returncode == 0 else None
    except ValueError:
        answer = None
    if isinstance(answer, dict) and answer:
        print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
