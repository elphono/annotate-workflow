"""Guards every test runs under, whether it asks for them or not.

Three autouse fixtures, each closing a failure that cost something real in
remarkable-sync, from which they are adapted:

- `_isolated_data`: no test reads or writes the user's registry, annotations,
  config or Claude Code transcripts. A test writing `~/.local/share/annotate/registry.json` would make
  the tray icon and the browser show a fake document.
- `_no_outside_process`: no test launches `claude` (billed, can edit files,
  an interactive one blocks the suite: measured 9 s of billed session the day
  the guard was introduced over there), nor anything that reaches the
  Windows desktop or systemd (`cmd.exe`, `explorer.exe`, `wslview`,
  `powershell.exe`, `wsl.exe`, `wt.exe` (it opens real terminal tabs),
  `reg.exe`, `systemctl`), nor `ssh`/`scp`. The end-to-end tests use fakes
  that are NOT named `claude` or `wt.exe`.
- `_no_real_signal`: no test sends a signal to a process it did not start.
  `os.killpg` is refused outright; `os.kill` only reaches pids of processes
  the test itself spawned. In remarkable-sync, a double carrying `pid = 1`
  made `os.killpg(1, SIGKILL)` broadcast to every process of the user during
  a plain `pytest`, and WSL shut the VM down (2026-08-23).

`tests/test_suite_guards.py` checks that each guard is armed: an autouse
guard is invisible, and nothing tells "it protects" from "it stopped
protecting" until someone trips it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from annotate import claude, inbox

FORBIDDEN_PROGRAMS = frozenset({
    "claude", "ssh", "scp", "cmd.exe", "explorer.exe", "wslview",
    "powershell.exe", "wsl.exe", "wt.exe", "reg.exe", "systemctl",
})


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNOTATE_DATA_DIR", str(tmp_path / "annotate-data"))
    monkeypatch.setenv("ANNOTATE_CONFIG", str(tmp_path / "annotate-config.toml"))
    for name in ("ANNOTATE_PORT", "ANNOTATE_CLAUDE", "WSL_DISTRO_NAME",
                 # set when the suite runs inside a Claude Code session:
                 # `annotate wait` would report that session's id
                 "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    # `claude.resumable` reads ~/.claude/projects: never the user's own.
    projects = tmp_path / "claude-projects"
    monkeypatch.setattr(claude, "projects_dir", lambda: projects)
    # `inbox.open_sessions` reads ~/.claude/sessions: a test must never find,
    # let alone post into, one of the user's real sessions.
    sessions = tmp_path / "claude-sessions"
    monkeypatch.setattr(inbox, "sessions_dir", lambda: sessions)


def _program(argv: object) -> str:
    if isinstance(argv, (list, tuple)) and argv:
        return Path(str(argv[0])).name
    if isinstance(argv, (str, bytes)):
        text = argv.decode() if isinstance(argv, bytes) else argv
        return Path(text.split()[0]).name if text.split() else ""
    return ""


@pytest.fixture(autouse=True)
def _no_outside_process(monkeypatch, request):
    real_popen = subprocess.Popen
    spawned: set[int] = set()
    request.node.spawned_pids = spawned

    class GuardedPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            name = _program(argv)
            if name in FORBIDDEN_PROGRAMS:
                raise AssertionError(
                    f"this test launches a real {name!r}: it would bill a "
                    f"session, touch the Windows desktop or systemd. Point "
                    f"ANNOTATE_CLAUDE at a fake executable not named "
                    f"'claude', or monkeypatch the call. argv: {argv}")
            super().__init__(*args, **kwargs)
            spawned.add(self.pid)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)


@pytest.fixture(autouse=True)
def _no_real_signal(monkeypatch, request, _no_outside_process):
    real_kill = os.kill

    def refuse_killpg(pgid, sig):
        raise AssertionError(
            f"os.killpg({pgid}, {sig}): a test tried to signal a real process "
            f"group. Replace os.killpg with a recorder in this test, and check "
            f"the pid your Popen double carries.")

    def guarded_kill(pid, sig):
        spawned = getattr(request.node, "spawned_pids", set())
        if not (isinstance(pid, int) and pid > 1 and pid in spawned):
            raise AssertionError(
                f"os.kill({pid}, {sig}): a test tried to signal a process it "
                f"did not start. Replace os.kill with a recorder in this test.")
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "killpg", refuse_killpg)
    monkeypatch.setattr(os, "kill", guarded_kill)


@pytest.fixture
def make_doc(tmp_path):
    """Write an HTML document inside a throwaway git-like repository."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)

    def make(body: str, name: str = "doc.html", title: str = "Test doc",
             folder: str = "docs") -> Path:
        path = repo / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title></head><body>{body}</body></html>",
            encoding="utf-8")
        return path

    return make
