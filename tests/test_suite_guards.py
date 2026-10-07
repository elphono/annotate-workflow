"""The autouse guards of conftest.py are armed.

Each test trips one guard with an action that is HARMLESS if the guard were
missing (signal 0 only checks permissions; a pid above pid_max does not
exist; the forbidden programs are inert decoys carrying the forbidden name),
so that a disarmed guard shows as a red test, never as a real side effect.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from annotate import claude, config, folders


def _decoy(folder: Path, name: str) -> str:
    """An inert executable carrying a forbidden NAME. If the guard were
    disarmed, this is what would run: nothing real, no window, no bill.
    (A first version called the real `explorer.exe /c echo`; the mutation
    campaign that disarmed the guard ran it for real on 2026-10-05.)"""
    path = folder / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def test_a_real_claude_cannot_be_launched(tmp_path):
    with pytest.raises(AssertionError, match="launches a real 'claude'"):
        subprocess.run([_decoy(tmp_path, "claude"), "--version"], capture_output=True)


def test_windows_programs_cannot_be_launched(tmp_path):
    for program in ("cmd.exe", "powershell.exe", "wsl.exe", "wt.exe", "explorer.exe",
                    "reg.exe", "systemctl", "wslview"):
        with pytest.raises(AssertionError, match="launches a real"):
            subprocess.Popen([_decoy(tmp_path, program)])


def test_killpg_is_refused():
    with pytest.raises(AssertionError, match="os.killpg"):
        os.killpg(1, 0)


def test_kill_of_a_process_the_test_did_not_start_is_refused():
    with pytest.raises(AssertionError, match="did not start"):
        os.kill(-1, 0)
    pid_max = int(Path("/proc/sys/kernel/pid_max").read_text())
    with pytest.raises(AssertionError, match="did not start"):
        os.kill(pid_max + 1, 0)


def test_kill_of_a_process_the_test_started_is_allowed():
    proc = subprocess.Popen(["sleep", "30"])
    proc.terminate()
    assert proc.wait(timeout=10) != 0


def test_the_data_directory_is_not_the_real_one(tmp_path):
    assert config.data_dir().is_relative_to(tmp_path)
    assert not config.data_dir().is_relative_to(Path.home() / ".local")


def test_the_claude_transcripts_are_not_the_real_ones(tmp_path):
    assert claude.projects_dir().is_relative_to(tmp_path)


def test_the_tracked_folders_are_not_the_real_ones(tmp_path):
    """A scan in a test would otherwise walk the user's workspace, and a
    folder added in a test would be checked against the real home."""
    assert config.workspace().is_relative_to(tmp_path)
    assert all(Path(f["path"]).is_relative_to(tmp_path.resolve()) for f in folders.load())
    assert folders.home() == tmp_path.resolve()
