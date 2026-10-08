"""Test doubles shared by several test files.

`RecordingOpener.__call__` has the REAL signature of
`terminal.open_session`; `test_sender.test_the_opener_double_matches_the_real_one`
compares them. A double that accepts what the original refuses lets a wrong
call pass (remarkable-sync paid for one with the whole machine, 2026-08-23).

The fake executables are real programs, launched by the real code, through
real pipes. Only their NAMES are fake: the suite guard refuses any program
named `claude` or `wt.exe`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from annotate import inbox, terminal


class RecordingOpener:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail

    def __call__(self, doc_id: str, cwd: str, claude_bin: str, resume: str | None,
                 prompt: str) -> str:
        self.calls.append({"doc_id": doc_id, "cwd": cwd, "claude_bin": claude_bin,
                           "resume": resume, "prompt": prompt})
        if self.fail is not None:
            raise self.fail
        return "fake-wt.exe"


FAKE_CLAUDE = '''#!{python}
"""A stand-in for an interactive `claude`: records how it was started."""
import json, os, sys
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
'''

FAKE_WT = '''#!{python}
"""A stand-in for wt.exe AND the wsl.exe it starts: checks the shape of the
call, then runs the script the way `wsl.exe -e bash -l <script>` would
(without -l: the test must not load the user's profile)."""
import json, subprocess, sys
argv = sys.argv[1:]
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")
tail = argv[argv.index("--") + 1:]
assert tail[0] == "wsl.exe" and tail[1] == "-d" and tail[3:6] == ["-e", "bash", "-l"], tail
sys.exit(subprocess.run(["bash", tail[6]]).returncode)
'''


def _write(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def write_fake_claude(folder) -> tuple[Path, Path]:
    """`fake-claude`, and the log it appends {argv, cwd} to."""
    folder = Path(folder)
    log = folder / "fake-claude.log"
    return _write(folder, "fake-claude",
                  FAKE_CLAUDE.format(python=sys.executable, log=str(log))), log


def write_fake_wt(folder, monkeypatch) -> Path:
    """`fake-wt.exe` on the PATH, and terminal.WT pointing at it; returns its log."""
    folder = Path(folder)
    log = folder / "fake-wt.log"
    _write(folder, "fake-wt.exe", FAKE_WT.format(python=sys.executable, log=str(log)))
    monkeypatch.setenv("PATH", f"{folder}:{__import__('os').environ['PATH']}")
    monkeypatch.setattr(terminal, "WT", "fake-wt.exe")
    return log


def read_log(log) -> list:
    path = Path(log)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def register_session(session_id: str, socket_path: Path, *, pid: int | None = None,
                     proc_start: str | None = None, name: str = "session-c1",
                     updated: int = 1) -> None:
    """An entry of the (isolated) `~/.claude/sessions`, as Claude Code writes
    it for an open session; by default a LIVE one: this test process."""
    pid = os.getpid() if pid is None else pid
    start = inbox._proc_start(os.getpid()) if proc_start is None else proc_start
    inbox.sessions_dir().mkdir(parents=True, exist_ok=True)
    (inbox.sessions_dir() / f"{pid}-{name}.json").write_text(json.dumps({
        "pid": pid, "sessionId": session_id, "procStart": start, "name": name,
        "messagingSocketPath": str(socket_path), "kind": "interactive",
        "updatedAt": updated}))


def transcript(projects: Path, session: str, *, folder: str = "-home-x-repo",
               lines: tuple[str, ...] = ('{"type":"user","message":"hi"}',)) -> Path:
    """A Claude Code transcript as `claude.resumable` reads it."""
    path = projects / folder / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def compact(entry: dict) -> str:
    """A transcript line as Claude Code writes it: no space after `:` or `,`."""
    return json.dumps(entry, separators=(",", ":"), ensure_ascii=False)


def conversation(projects: Path, session: str, *, cwd: str = "/repo", title: str = "",
                 when: float = 1000.0, folder: str = "-home-x-repo") -> Path:
    """A resumable transcript, shaped like a real one: a few bookkeeping lines
    without `cwd`, then a user line that carries it; an AI title if given;
    dated `when` (its mtime is what `claude.sessions` calls last activity)."""
    lines = [compact({"type": "mode", "mode": "normal", "sessionId": session}),
             compact({"type": "permission-mode", "permissionMode": "default",
                      "sessionId": session}),
             compact({"type": "user", "sessionId": session, "cwd": cwd,
                      "message": {"role": "user", "content": "hello"}})]
    if title:
        lines.append(compact({"type": "ai-title", "aiTitle": title, "sessionId": session}))
    path = transcript(projects, session, folder=folder, lines=tuple(lines))
    os.utime(path, (when, when))
    return path


def tool_call(session: str, when: str, command: str, cwd: str = "/repo") -> str:
    """An assistant line carrying one Bash tool call, at ISO time `when`."""
    return compact({"type": "assistant", "sessionId": session, "cwd": cwd,
                    "timestamp": when,
                    "message": {"content": [{"type": "tool_use", "name": "Bash",
                                             "input": {"command": command}}]}})
