from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from annotate import service, tray

TRAY_PS1 = Path(tray.SCRIPT)


def test_the_unit_runs_the_daemon_directly_never_under_uv_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = service.write_unit()
    text = target.read_text()
    assert target == tmp_path / ".config" / "systemd" / "user" / "annotate.service"
    exec_start = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
    assert exec_start == [f"ExecStart={Path(sys.executable).parent / 'annotate'} serve"]
    assert "uv run" not in "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text


def test_the_unit_path_reaches_claude(tmp_path, monkeypatch):
    fake_bin = tmp_path / "tools"
    fake_bin.mkdir()
    (fake_bin / "claude").write_text("#!/bin/sh\n")
    (fake_bin / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    assert service.search_path().split(":")[0] == str(fake_bin)


def test_service_refuses_an_interpreter_without_the_entry_point(monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "executable", str(tmp_path / "python3"))
    with pytest.raises(service.ServiceError):
        service.entry_point()


# -- tray.ps1: the lessons of plateau.ps1, as checks -------------------------

def test_tray_script_is_pure_ascii_and_short():
    data = TRAY_PS1.read_bytes()
    assert all(b < 128 for b in data), "non-ASCII byte in tray.ps1"
    assert len(data.decode().splitlines()) < 600


def _params(script: str) -> list[str]:
    block = script[script.index("param("):script.index("Set-StrictMode")]
    return re.findall(r"\$([A-Za-z]+)", block)


def test_the_parameters_python_passes_are_the_ones_the_script_declares():
    declared = {p.lower() for p in _params(TRAY_PS1.read_text())}
    passed = tray.script_arguments(8000, "SomeDistro", "/x/y")
    flags = {a[1:].lower() for a in passed if a.startswith("-")}
    assert flags <= declared
    assert "once" in declared


def test_no_script_variable_reuses_a_parameter_name():
    # PowerShell names are case-INSENSITIVE: `$unit = ...` would write the
    # [string]$Unit parameter. plateau.ps1 died on exactly this.
    script = TRAY_PS1.read_text()
    params = {p.lower() for p in _params(script)}
    body = script[script.index("Set-StrictMode"):]
    assigned = {m.lower() for m in re.findall(r"\$([A-Za-z]+)\s*=(?!=)", body)}
    loops = {m.lower() for m in re.findall(r"foreach\s*\(\s*\$([A-Za-z]+)", body)}
    assert not (assigned | loops) & params


def test_no_validateset_on_a_parameter_with_a_default():
    assert "ValidateSet" not in TRAY_PS1.read_text()


def test_the_startup_shortcut_asks_windows_for_the_folder(monkeypatch):
    monkeypatch.setattr(tray, "windows_path", lambda p: r"\\wsl.localhost\D\x\tray.ps1")
    monkeypatch.setenv("WSL_DISTRO_NAME", "SomeDistro")
    script = tray.shortcut_script(8123)
    assert "GetFolderPath('Startup')" in script
    assert "annotate-tray.lnk" in script
    assert '"-Port" ' not in script and "-Port 8123" in script
    assert "-Distro SomeDistro" in script and "-Unit annotate.service" in script
    removal = tray.shortcut_script(8123, uninstall=True)
    assert "Remove-Item" in removal and "CreateShortcut" not in removal


def test_tray_refuses_without_a_distribution(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    with pytest.raises(tray.TrayError):
        tray.environment_arguments(8765)
