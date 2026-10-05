"""Launching `claude -p`: the vanished-session signal and the process-group guard.

Copied from remarkable-sync, `src/rmcore/claude.py` (2026-10-05), translated
to English, and reduced to what this repository needs. Three pieces matter
and must not be "simplified" away:

- `SESSION_GONE_PATTERN` / `SessionGone`: what `claude --resume` prints when
  the conversation no longer exists on this machine (purged after thirty
  days, or opened on another machine). Measured on a real run by
  remarkable-sync: "No conversation found with session ID: <uuid>".
- `kill_group`: the most critical guard of the original repository. See its
  docstring before touching it.
- `session_env`: the marker that tells the Claude Code hook "this session was
  started BY annotate", so that its edits do not re-register the document
  (same loop as remarkable-sync's `RMSYNC_TOUR`).

The prompt always goes through STDIN, never argv: a prompt starting with a
dash would be read by the CLI option parser (remarkable-sync, 2026-08-23).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess

log = logging.getLogger("annotate")

SESSION_GONE_PATTERN = "No conversation found with session ID"
SESSION_MARKER = "ANNOTATE_SESSION"


class SessionGone(Exception):
    """The session to resume no longer exists on THIS machine."""


class LaunchError(Exception):
    """`claude` produced no usable result (timeout, crash, unreadable output)."""


def session_env() -> dict[str, str]:
    """The environment of a session started by annotate: the user's, plus a marker."""
    return {**os.environ, SESSION_MARKER: "1"}


def argv(claude_bin: str, resume: str | None) -> list[str]:
    command = [claude_bin, "-p", "--output-format", "json",
               "--permission-mode", "acceptEdits"]
    if resume:
        command += ["--resume", resume]
    return command


def kill_group(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    """Kill the process group of `proc`, without raising if it is already gone.

    **The guard on pids 0 and 1 is the reason this function exists.**
    `killpg(pgid, sig)` is `kill(-pgid, sig)` in POSIX: `-1` does not mean
    "group 1" but EVERY process of the user, and `0` means the caller's own
    group, i.e. the daemon and the shell that started it. Measured in
    remarkable-sync on 2026-08-23: a test double carrying `pid = 1` sent this
    SIGKILL in broadcast during a plain `uv run pytest`; every terminal and
    every Claude Code session of the machine died, and WSL, with no session
    left, shut the VM down fifteen seconds later.

    The second line of defence is the test-suite fixture that refuses any
    real signal (tests/conftest.py). This first line covers production.
    """
    if proc.pid in (0, 1):
        log.error("refusing to kill process group %r: this pid has a special "
                  "meaning for killpg (0 = my own group, 1 = every process of "
                  "the user). A claude session may be left orphaned, but "
                  "nothing was killed.", proc.pid)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
