"""The daemon's pass over the workspace: documents the hook did not see."""
from __future__ import annotations

import importlib.util
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from annotate import claude, registry, scanner
from fakes import tool_call, transcript

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "register-on-write.py"
HTML = "<!doctype html><title>{}</title><p>x</p>"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "workspace"
    (root / "repo" / ".git").mkdir(parents=True)
    return root


def write(root: Path, relative: str, mtime: float, title: str = "Doc") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HTML.format(title))
    os.utime(path, (mtime, mtime))
    return path


def test_nothing_older_than_the_first_pass_is_taken(ws):
    write(ws, "repo/docs/old.html", 1000.0)
    assert scanner.scan_once(ws, now=2000.0) == []          # first pass: a starting point
    assert scanner.scan_once(ws, now=3000.0) == []          # the old file stays out
    assert registry.all_docs() == {}


def test_a_document_written_after_the_first_pass_is_registered_with_its_writer(ws):
    scanner.scan_once(ws, now=2000.0)
    path = write(ws, "repo/docs/sub/new.html", 2500.0, title="Timeline")
    transcript(claude.projects_dir(), "parent-1", folder="-home-x-repo/parent-1/subagents",
               lines=(tool_call("parent-1", iso(2499.0), f"cp /tmp/s/new.html {path}",
                                cwd=str(ws / "repo")),))
    [doc_id] = scanner.scan_once(ws, now=3000.0)
    entry = registry.get(doc_id)
    assert entry["path"] == str(path) and entry["title"] == "Timeline"
    assert entry["session_id"] == "parent-1" and entry["cwd"] == str(ws / "repo")


def test_a_document_nobody_claims_is_registered_without_a_session(ws):
    scanner.scan_once(ws, now=2000.0)
    write(ws, "repo/docs/orphan.html", 2500.0)
    [doc_id] = scanner.scan_once(ws, now=3000.0)
    assert registry.get(doc_id)["session_id"] == ""


def test_the_daemon_being_down_loses_nothing(ws):
    scanner.scan_once(ws, now=2000.0)
    scanner.scan_once(ws, now=3000.0)
    write(ws, "repo/docs/while-down.html", 3500.0)     # after the last pass
    assert len(scanner.scan_once(ws, now=9000.0)) == 1  # the next start finds it


def test_three_passes_register_each_document_once(ws):
    scanner.scan_once(ws, now=2000.0)
    write(ws, "repo/docs/a.html", 2500.0)
    assert len(scanner.scan_once(ws, now=3000.0)) == 1
    write(ws, "repo/docs/b.html", 3500.0)
    assert len(scanner.scan_once(ws, now=4000.0)) == 1
    assert scanner.scan_once(ws, now=5000.0) == []
    assert len(registry.all_docs()) == 2


def test_a_document_the_hook_registered_keeps_its_session(ws):
    scanner.scan_once(ws, now=2000.0)
    path = write(ws, "repo/docs/hooked.html", 2500.0)
    doc_id = registry.register(path, session="from-the-hook")["id"]
    assert scanner.scan_once(ws, now=3000.0) == []
    assert registry.get(doc_id)["session_id"] == "from-the-hook"


def test_a_forgotten_document_comes_back_only_if_written_again(ws):
    scanner.scan_once(ws, now=2000.0)
    path = write(ws, "repo/docs/f.html", 2500.0)
    [doc_id] = scanner.scan_once(ws, now=3000.0)
    registry.forget(doc_id)
    assert scanner.scan_once(ws, now=4000.0) == []
    os.utime(path, (4500.0, 4500.0))
    assert len(scanner.scan_once(ws, now=5000.0)) == 1


def test_figures_and_tool_folders_are_never_documents(ws):
    scanner.scan_once(ws, now=2000.0)
    for relative in ("repo/docs/figures/x/tpl.html", "repo/docs/node_modules/p/i.html",
                     "repo/src/page.html", "repo/docs/notes.md"):
        write(ws, relative, 2500.0)
    assert scanner.scan_once(ws, now=3000.0) == []


@pytest.mark.parametrize("relative", [
    "repo/docs/a.html", "repo/docs/specs/b.html", "repo/docs/figures/x/tpl.html",
    "repo/docs/.git/c.html", "repo/src/d.html", "repo/docs.html", "repo/docs/e.md",
    "docs/f.html", "repo/doc/g.html", "repo/docs/node_modules/h.html",
    "repo/.claude/worktrees/agent-1/docs/i.html",
])
def test_the_hook_and_the_scan_agree_on_what_a_document_is(ws, relative):
    spec = importlib.util.spec_from_file_location("register_on_write", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)  # type: ignore[union-attr]
    path = write(ws, relative, 2500.0)
    assert hook.wanted(path, ws) == scanner.wanted(path, ws)


def test_a_failing_pass_does_not_stop_the_scan(ws, monkeypatch):
    calls = []
    stop = threading.Event()

    def flaky(root):
        calls.append(root)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        if len(calls) == 3:
            stop.set()

    monkeypatch.setattr(scanner, "scan_once", flaky)
    thread = threading.Thread(target=scanner.run, args=(ws, stop, 0.01), daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and len(calls) == 3


def test_an_unreadable_state_is_an_error_not_a_backfill(ws):
    scanner.state_path().parent.mkdir(parents=True, exist_ok=True)
    scanner.state_path().write_text("{broken")
    with pytest.raises(scanner.ScanError):
        scanner.scan_once(ws, now=2000.0)


def test_a_document_inside_an_agent_worktree_is_not_registered(ws):
    """A temporary copy, deleted with the agent (2026-10-06: it stayed listed as
    'file missing')."""
    scanner.scan_once(ws, now=2000.0)
    write(ws, "repo/.claude/worktrees/agent-a9/docs/install.html", 2500.0)
    assert scanner.scan_once(ws, now=3000.0) == []
