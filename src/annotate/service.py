"""The systemd user unit of the daemon, generated from this machine's environment.

Pattern taken from remarkable-sync, `rmpapier/cli.py::_cmd_service`, with the
lesson of 2026-09-15 applied from the start: **the unit runs the entry point
of the project's virtualenv DIRECTLY, never `uv run`**. Under systemd,
`KillMode=control-group` sends SIGTERM to the whole group, and `uv run`
forwards its own to the child: Python received two (10 times out of 15), and
the duplicate ended in exit 143 or in a deadlock then SIGKILL.

Every path comes from the environment at generation time: the interpreter
running this command (hence the venv), `claude`, `cmd.exe`, `explorer.exe` and
`wt.exe` on the PATH. Nothing is written in the code.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

UNIT_NAME = "annotate.service"


class ServiceError(Exception):
    """The unit cannot be generated on this machine."""


def entry_point() -> Path:
    entry = Path(sys.executable).parent / "annotate"
    if not entry.is_file():
        raise ServiceError(
            f"{entry} not found: `service` must run with the project's "
            f"interpreter (`uv run annotate service`), otherwise the unit "
            f"would start a missing file")
    return entry


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def search_path() -> str:
    """PATH of the unit: where `claude`, the Windows openers and Windows
    Terminal live, then the system directories. `wt.exe` sits in
    `WindowsApps`, which no other tool shares: without it the daemon falls
    back to a plain console window (terminal.py)."""
    dirs: list[str] = []
    for tool in ("claude", "wslview", "cmd.exe", "explorer.exe", "wt.exe"):
        found = shutil.which(tool)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    for system in ("/usr/local/bin", "/usr/bin", "/bin"):
        if system not in dirs:
            dirs.append(system)
    return ":".join(dirs)


def unit_text(entry: Path, project: Path, path_env: str) -> str:
    return f"""[Unit]
Description=annotate-workflow daemon (annotate HTML documents, send notes to Claude Code)
Documentation=file://{project}/CLAUDE.md
After=default.target

[Service]
Type=simple
WorkingDirectory={project}
Environment=PATH={path_env}
# The daemon DIRECTLY, never under `uv run`: uv forwards the SIGTERM systemd
# already sends to the whole group (remarkable-sync, measured 2026-09-15).
ExecStart={entry} serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def write_unit() -> Path:
    entry = entry_point()
    project = Path(__file__).resolve().parent.parent.parent
    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(unit_text(entry, project, search_path()), encoding="utf-8")
    return target
