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
import sys
from pathlib import Path

from annotate import terminal


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


def transcript(projects: Path, session: str, *, folder: str = "-home-x-repo",
               lines: tuple[str, ...] = ('{"type":"user","message":"hi"}',)) -> Path:
    """A Claude Code transcript as `claude.resumable` reads it."""
    path = projects / folder / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
