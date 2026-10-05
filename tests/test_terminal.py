"""The terminal tab: the call shape, and the script it runs, executed for real.

The script is run by a real bash, and `claude` is a fake executable that
records its argv and working directory: the prompt must arrive as ONE
argument, byte for byte, whatever it contains.
"""
from __future__ import annotations

import subprocess

import pytest

from annotate import config, terminal
from fakes import read_log, write_fake_claude, write_fake_wt

NASTY = ("Document: \"x\"\n--- Note 1 ---\nit's $HOME `date`; rm -rf / && echo; \\n"
         "accents: é à — and a trailing space ")


def test_the_prompt_reaches_claude_as_one_argument_in_the_right_folder(tmp_path):
    fake, log = write_fake_claude(tmp_path / "bin")
    cwd = tmp_path / "a repo with 'quotes'; and spaces"
    cwd.mkdir()
    script = terminal.write_launch("d1", str(cwd), str(fake), "sess-9", NASTY)
    subprocess.run(["bash", str(script)], check=True, timeout=30)
    [record] = read_log(log)
    assert record["argv"] == ["--resume", "sess-9", NASTY.rstrip("\n")]
    assert record["cwd"] == str(cwd)
    assert not script.exists(), "the script deletes itself and its prompt"
    assert list(script.parent.iterdir()) == []


def test_a_new_session_gets_no_resume_flag(tmp_path):
    fake, log = write_fake_claude(tmp_path / "bin")
    script = terminal.write_launch("d1", str(tmp_path), str(fake), None, "hello")
    subprocess.run(["bash", str(script)], check=True, timeout=30)
    assert read_log(log)[0]["argv"] == ["hello"]


def test_the_tab_command_never_carries_a_semicolon(tmp_path, monkeypatch):
    """`;` is wt's command separator: one in the arguments opens a second tab
    and cuts the command (measured 2026-10-05)."""
    write_fake_wt(tmp_path / "wt", monkeypatch)
    script = terminal.write_launch("d1", str(tmp_path), "claude", None, "a; b; c")
    argv = terminal.command("d1", script, "Some-Distro")
    assert argv[1:5] == ["-w", "0", "new-tab", "--title"]
    assert argv[argv.index("--") + 1:] == ["wsl.exe", "-d", "Some-Distro", "-e",
                                           "bash", "-l", str(script)]
    assert not any(";" in part for part in argv)


def test_a_data_dir_wt_would_split_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNOTATE_DATA_DIR", str(tmp_path / "with space"))
    with pytest.raises(terminal.TerminalError, match="must not contain"):
        terminal.write_launch("d1", str(tmp_path), "claude", None, "x")


def test_without_windows_terminal_a_console_opens_and_without_both_it_says_so(
        tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "WT", "no-such-wt.exe")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    script = tmp_path / "s.sh"
    with pytest.raises(terminal.TerminalError, match="neither wt.exe nor cmd.exe"):
        terminal.command("d1", script, "D")
    (bin_dir / "cmd.exe").write_text("#!/bin/sh\n")
    (bin_dir / "cmd.exe").chmod(0o755)
    argv = terminal.command("d1", script, "D")
    assert argv[1:4] == ["/c", "start", ""] and argv[4] == "wsl.exe"


def test_the_distribution_comes_from_the_env_then_from_wslpath(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Env-Distro")
    assert terminal.distro() == "Env-Distro"
    monkeypatch.delenv("WSL_DISTRO_NAME")

    def fake_run(argv, **kwargs):
        assert argv == ["wslpath", "-w", "/"]
        return subprocess.CompletedProcess(argv, 0, "\\\\wsl.localhost\\Path-Distro\\\n", "")

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    assert terminal.distro() == "Path-Distro"


def test_open_session_runs_the_tab_end_to_end(tmp_path, monkeypatch):
    """The real open_session: fake wt.exe -> the generated script -> fake claude."""
    wt_log = write_fake_wt(tmp_path / "wt", monkeypatch)
    fake, log = write_fake_claude(tmp_path / "bin")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Test-Distro")
    used = terminal.open_session("d1", str(tmp_path), str(fake), "sess-5", NASTY)
    assert used == "fake-wt.exe"
    assert read_log(wt_log)[0][read_log(wt_log)[0].index("-d") + 1] == "Test-Distro"
    assert read_log(log) == [{"argv": ["--resume", "sess-5", NASTY.rstrip("\n")],
                              "cwd": str(tmp_path)}]


def test_a_missing_session_folder_opens_nothing(tmp_path, monkeypatch):
    wt_log = write_fake_wt(tmp_path / "wt", monkeypatch)
    with pytest.raises(terminal.TerminalError, match="does not exist"):
        terminal.open_session("d1", str(tmp_path / "gone"), "claude", None, "x")
    assert read_log(wt_log) == []
    assert not (config.data_dir() / "launch").exists()
