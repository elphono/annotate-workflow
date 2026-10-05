"""The Claude Code hook, run as Claude Code runs it: a process, JSON on stdin.

A fake `annotate` on the PATH records its arguments; nothing real is
registered.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "register-on-write.py"


@pytest.fixture
def run_hook(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    fake = bin_dir / "annotate"
    answer = tmp_path / "answer.txt"     # what the fake prints, if present
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    f"if os.path.exists({str(answer)!r}):\n"
                    f"    sys.stdout.write(open({str(answer)!r}).read())\n")
    fake.chmod(0o755)
    workspace = tmp_path / "workspace"
    outputs: list[str] = []

    def run(payload, extra_env=None, prints=None):
        if prints is not None:
            answer.write_text(prints)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
               "ANNOTATE_WORKSPACE": str(workspace), **(extra_env or {})}
        data = payload if isinstance(payload, str) else json.dumps(payload)
        done = subprocess.run([sys.executable, str(HOOK)], input=data, text=True,
                              capture_output=True, env=env, timeout=30)
        assert done.returncode == 0, done.stderr
        outputs.append(done.stdout)
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    run.outputs = outputs  # type: ignore[attr-defined]
    return run, workspace


def payload(path, session="sess-abc", cwd="/somewhere/repo"):
    return {"session_id": session, "cwd": cwd, "hook_event_name": "PostToolUse",
            "tool_name": "Write", "tool_input": {"file_path": str(path)}}


def test_an_html_doc_under_docs_is_registered_with_its_session(run_hook):
    run, ws = run_hook
    doc = ws / "misc" / "proj" / "docs" / "specs" / "design.html"
    assert run(payload(doc)) == [
        ["register", str(doc), "--session", "sess-abc", "--cwd", "/somewhere/repo",
         "--hook"]]


def test_other_files_are_ignored(run_hook):
    run, ws = run_hook
    assert run(payload(ws / "proj" / "docs" / "notes.md")) == []
    assert run(payload(ws / "proj" / "src" / "page.html")) == []      # no docs/
    assert run(payload(ws / "proj" / "docs.html")) == []             # docs is the file
    assert run(payload(Path("/tmp/elsewhere/docs/x.html"))) == []    # outside workspace


def test_the_context_annotate_prints_reaches_the_session(run_hook):
    run, ws = run_hook
    doc = ws / "proj" / "docs" / "x.html"
    context = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": "run annotate wait d1"}}
    run(payload(doc), prints=json.dumps(context))
    assert json.loads(run.outputs[-1]) == context


@pytest.mark.parametrize("printed", ["{}", "not json", "[1, 2]", ""])
def test_nothing_but_a_non_empty_object_is_forwarded(run_hook, printed):
    run, ws = run_hook
    run(payload(ws / "proj" / "docs" / "x.html"), prints=printed)
    assert run.outputs[-1] == ""


def test_garbage_on_stdin_never_fails_the_tool_call(run_hook):
    run, ws = run_hook
    assert run("not json at all") == []
    assert run({"tool_input": "nope"}) == []
    assert run({"session_id": "s"}) == []


def test_a_missing_session_does_not_clear_the_recorded_one(run_hook):
    run, ws = run_hook
    doc = ws / "proj" / "docs" / "x.html"
    assert run(payload(doc, session="")) == [
        ["register", str(doc), "--cwd", "/somewhere/repo", "--hook"]]
