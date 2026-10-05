"""Test doubles shared by several test files.

`FakeProc.communicate(input=None, timeout=None)` has the REAL signature of
`subprocess.Popen.communicate`; `test_sender.test_the_double_matches_popen`
compares them.
"""
from __future__ import annotations

import json


class FakeProc:
    def __init__(self, script, argv, kwargs):
        self.script, self.argv, self.kwargs = script, argv, kwargs
        self.pid = 424242
        self.returncode = None
        self.stdin_text = None

    def communicate(self, input=None, timeout=None):
        if self.stdin_text is None:
            self.stdin_text = input
        outcome = self.script(self)
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode, out, err = outcome
        return out, err


class FakePopen:
    """Records every launch; `script(proc)` decides each outcome."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.procs: list[FakeProc] = []

    def __call__(self, argv, **kwargs):
        proc = FakeProc(self.scripts.pop(0), argv, kwargs)
        self.procs.append(proc)
        return proc


def ok(session="sess-new"):
    return lambda proc: (0, json.dumps({"session_id": session, "result": "done"}), "")


FAKE_CLAUDE = '''#!{python}
"""A stand-in for `claude -p`: records what it received, answers like it."""
import json, os, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
record = {{"argv": argv, "cwd": os.getcwd(), "stdin": prompt,
          "marker": os.environ.get("ANNOTATE_SESSION", "")}}
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
if {gone!r} and "--resume" in argv:
    sys.stderr.write("No conversation found with session ID: " + argv[-1] + "\\n")
    sys.exit(1)
print(json.dumps({{"type": "result", "is_error": False, "result": "done",
                  "session_id": "fake-session-42", "total_cost_usd": 0.0123}}))
'''


def write_fake_claude(folder, *, gone: bool = False):
    """Write an executable named `fake-claude` (NOT `claude`: the suite guard
    refuses that name) and return (path, log path)."""
    import sys
    from pathlib import Path

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "fake-claude.log"
    script = folder / "fake-claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(log),
                                         gone=gone), encoding="utf-8")
    script.chmod(0o755)
    return script, log


def read_log(log):
    from pathlib import Path

    path = Path(log)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]
