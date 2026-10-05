"""Open a URL in the Windows browser, from WSL.

Three openers, tried in order: `wslview` (wslu), `cmd.exe /c start`,
`explorer.exe`. Each is looked up on the PATH; none is written as an absolute
path. Under systemd the PATH is the one the unit declares, and
`annotate service` puts the directories of `cmd.exe` and `explorer.exe` in it,
as discovered in the user's shell when the unit is written.

`cmd.exe` runs with `/mnt/...`-independent cwd: it refuses a UNC working
directory ("UNC paths are not supported") and falls back to C:\\Windows, which
is harmless but noisy; the cwd passed is the Windows system root when known.
"""
from __future__ import annotations

import os
import shutil
import subprocess


class OpenError(Exception):
    """No opener could open the URL."""


def openers(url: str) -> list[list[str]]:
    found = []
    if path := shutil.which("wslview"):
        found.append([path, url])
    if path := shutil.which("cmd.exe"):
        # The empty "" is the window title `start` expects before the target.
        found.append([path, "/c", "start", "", url])
    if path := shutil.which("explorer.exe"):
        found.append([path, url])
    return found


def open_url(url: str) -> str:
    """Open `url`; return the opener used. Raise OpenError if none worked."""
    candidates = openers(url)
    if not candidates:
        raise OpenError("no opener found on the PATH (wslview, cmd.exe, "
                        "explorer.exe): open the URL by hand: " + url)
    errors = []
    for command in candidates:
        cwd = os.path.dirname(command[0]) if command[0].endswith(".exe") else None
        try:
            done = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                  timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{command[0]}: {exc}")
            continue
        # explorer.exe returns 1 even when it opened the URL: only a launch
        # failure counts for it.
        if done.returncode == 0 or os.path.basename(command[0]) == "explorer.exe":
            return os.path.basename(command[0])
        errors.append(f"{command[0]} exited {done.returncode}: {done.stderr.strip()[:200]}")
    raise OpenError("could not open " + url + ": " + "; ".join(errors))
