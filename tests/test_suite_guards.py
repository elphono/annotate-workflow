"""The autouse guards of conftest.py are armed.

Each test trips one guard with an action that is HARMLESS if the guard were
missing (signal 0 only checks permissions; a pid above pid_max does not
exist; `claude --version` would merely print a version), so that a disarmed
guard shows as a red test, never as a real side effect.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from annotate import config


def test_a_real_claude_cannot_be_launched():
    with pytest.raises(AssertionError, match="launches a real 'claude'"):
        subprocess.run(["claude", "--version"], capture_output=True)


def test_windows_programs_cannot_be_launched():
    for program in ("cmd.exe", "powershell.exe", "wsl.exe", "explorer.exe"):
        with pytest.raises(AssertionError, match="launches a real"):
            subprocess.Popen([program, "/c", "echo"])


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
