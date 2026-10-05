"""The Windows tray icon: build its command line, install its Startup shortcut.

The icon itself is `windows/tray.ps1` (PowerShell 5.1 + WinForms, nothing to
install on Windows). It talks to the daemon over HTTP only; the only thing it
does through `wsl.exe` is start/stop/restart the systemd unit.

Everything the script needs arrives as parameters computed here from the
environment (port from the config, distribution from `WSL_DISTRO_NAME`, home
from `Path.home()`, script path from `wslpath -w`): the script carries no
path, no distribution name, no port. Same discipline as remarkable-sync's
`rmpapier/plateau.py`.

The Startup shortcut is the one thing written on the Windows side, and only
on request (`annotate tray`, removed by `annotate tray --uninstall`). The
Startup folder is asked to Windows (`[Environment]::GetFolderPath('Startup')`),
never composed from a user name.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

from .service import UNIT_NAME

SCRIPT = Path(__file__).resolve().parent / "windows" / "tray.ps1"
SHORTCUT_NAME = "annotate-tray.lnk"


class TrayError(Exception):
    """The Windows side could not be reached (not under WSL?)."""


def powershell() -> str:
    found = shutil.which("powershell.exe")
    if not found:
        raise TrayError("powershell.exe is not on the PATH: this command only "
                        "makes sense under WSL, from an interactive shell")
    return found


def windows_path(path: Path) -> str:
    try:
        done = subprocess.run(["wslpath", "-w", str(path)], capture_output=True,
                              text=True, check=True)
    except FileNotFoundError as exc:
        raise TrayError("wslpath not found: not running under WSL") from exc
    except subprocess.CalledProcessError as exc:
        raise TrayError(f"wslpath -w {path} failed: {exc.stderr.strip()}") from exc
    return done.stdout.strip()


def script_arguments(port: int, distro: str, home: str, unit: str = UNIT_NAME) -> list[str]:
    """Parameters of tray.ps1, in its own vocabulary."""
    if not distro:
        raise TrayError("WSL_DISTRO_NAME is empty: the icon would not know which "
                        "distribution runs the daemon")
    return ["-Port", str(port), "-Distro", distro, "-LinuxHome", home, "-Unit", unit]


def environment_arguments(port: int) -> list[str]:
    return script_arguments(port, os.environ.get("WSL_DISTRO_NAME", ""),
                            str(Path.home()))


def command(port: int, *, once: bool = False) -> list[str]:
    argv = [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass"]
    if not once:
        argv += ["-WindowStyle", "Hidden"]
    argv += ["-File", windows_path(SCRIPT), *environment_arguments(port)]
    if once:
        argv.append("-Once")
    return argv


def _quote_ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def shortcut_script(port: int, *, uninstall: bool = False) -> str:
    """PowerShell that creates (or removes) the Startup shortcut, and says so."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$dir = [Environment]::GetFolderPath('Startup')",
        f"$lnk = Join-Path $dir {_quote_ps(SHORTCUT_NAME)}",
    ]
    if uninstall:
        lines += [
            "if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk; "
            "\"removed $lnk\" } else { \"absent $lnk\" }",
        ]
        return "\n".join(lines)
    args = ["-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
            "-File", f'"{windows_path(SCRIPT)}"']
    for value in environment_arguments(port):
        args.append(f'"{value}"' if " " in value or value.startswith("/") else value)
    lines += [
        "$shell = New-Object -ComObject WScript.Shell",
        "$link = $shell.CreateShortcut($lnk)",
        "$link.TargetPath = 'powershell.exe'",
        f"$link.Arguments = {_quote_ps(' '.join(args))}",
        "$link.WindowStyle = 7",
        "$link.Save()",
        "\"installed $lnk\"",
    ]
    return "\n".join(lines)


def run_powershell(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        done = subprocess.run([powershell(), "-NoProfile", "-NonInteractive",
                               "-EncodedCommand", encoded],
                              capture_output=True, text=True, timeout=60,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrayError(f"powershell.exe failed: {exc}") from exc
    if done.returncode != 0:
        raise TrayError(f"powershell.exe exited {done.returncode}: "
                        f"{(done.stderr or done.stdout).strip()[:400]}")
    return done.stdout.strip()


def launch(port: int) -> int:
    """Start the icon now, detached; return the pid of the WSL-side launcher."""
    try:
        proc = subprocess.Popen(command(port), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as exc:
        raise TrayError(f"powershell.exe could not be started: {exc}") from exc
    return proc.pid


def once(port: int) -> tuple[int, str]:
    """Run the script in -Once mode: it queries the daemon, prints, exits."""
    try:
        done = subprocess.run(command(port, once=True), capture_output=True,
                              text=True, timeout=60, encoding="utf-8",
                              errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrayError(f"powershell.exe failed: {exc}") from exc
    return done.returncode, (done.stdout + done.stderr).strip()
