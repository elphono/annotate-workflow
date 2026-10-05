"""The sender, with a Popen double that behaves like the real one.

The double's `communicate(input=None, timeout=None)` has the real signature:
in remarkable-sync, a double that accepted one argument less turned a
correct call into a TypeError, whose cleanup path signalled pid 1 and took
the whole machine down. `test_the_double_matches_popen` keeps it honest.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import threading

import pytest

from annotate import claude, registry, sender
from annotate.config import Config
from fakes import FakePopen, FakeProc, ok

BODY = "<h1>Intro</h1><p>Alpha text.</p><h2>Part two</h2><p>Beta text.</p>"


def note(id_, text, selector="body > p:nth-of-type(1)", quote="Alpha text."):
    return {"id": id_, "selector": selector, "quote": quote, "note": text,
            "offset_x": 1, "offset_y": 2}


@pytest.fixture
def doc(make_doc):
    return registry.register(make_doc(BODY), session="sess-1")["id"]


def test_the_double_matches_popen():
    real = inspect.signature(subprocess.Popen.communicate)
    fake = inspect.signature(FakeProc.communicate)
    assert list(real.parameters) == list(fake.parameters)


def test_send_resumes_the_session_with_only_the_unsent_notes(doc):
    fake = FakePopen(ok("sess-1"), ok("sess-1"))
    s = sender.Sender(Config(), popen=fake)
    registry.replace_annotations(doc, [note("n1", "FIRST NOTE")])
    s.dispatch(doc, wait=True)
    registry.replace_annotations(doc, registry.list_annotations(doc)
                                 + [note("n2", "SECOND NOTE", quote="Beta text.",
                                         selector="body > p:nth-of-type(2)")])
    s.dispatch(doc, wait=True)

    first, second = fake.procs
    assert "FIRST NOTE" in first.stdin_text
    assert "SECOND NOTE" in second.stdin_text
    assert "FIRST NOTE" not in second.stdin_text      # already sent
    assert "Section: Part two" in second.stdin_text
    assert first.argv[-2:] == ["--resume", "sess-1"]
    assert "acceptEdits" in first.argv and "json" in first.argv
    assert first.kwargs["cwd"] == registry.get(doc)["cwd"]
    assert first.kwargs["start_new_session"] is True
    assert first.kwargs["env"][claude.SESSION_MARKER] == "1"
    assert all(a["sent_at"] for a in registry.list_annotations(doc))
    assert registry.get(doc)["status"] == "answered"


def test_three_sends_in_a_row_never_repeat_a_note(doc):
    fake = FakePopen(ok(), ok(), ok())
    s = sender.Sender(Config(), popen=fake)
    items = []
    for rank in range(3):
        items = registry.list_annotations(doc) + [note(f"n{rank}", f"NOTE-{rank}")]
        registry.replace_annotations(doc, items)
        s.dispatch(doc, wait=True)
    for rank, proc in enumerate(fake.procs):
        assert [f"NOTE-{i}" in proc.stdin_text for i in range(3)] == \
            [i == rank for i in range(3)]


def test_a_vanished_session_falls_back_to_a_new_one(doc):
    gone = lambda proc: (1, "", "No conversation found with session ID: sess-1")  # noqa: E731
    fake = FakePopen(gone, ok("sess-fresh"))
    s = sender.Sender(Config(), popen=fake)
    registry.replace_annotations(doc, [note("n1", "PLEASE FIX")])
    s.dispatch(doc, wait=True)
    resumed, fresh = fake.procs
    assert "--resume" in resumed.argv and "--resume" not in fresh.argv
    assert fresh.stdin_text.startswith("You did not produce this document")
    assert "PLEASE FIX" in fresh.stdin_text
    assert registry.get(doc)["session_id"] == "sess-fresh"
    assert registry.get(doc)["status"] == "answered"


def test_no_session_means_a_new_session_directly(make_doc):
    doc_id = registry.register(make_doc(BODY))["id"]
    fake = FakePopen(ok("sess-x"))
    registry.replace_annotations(doc_id, [note("n1", "X")])
    sender.Sender(Config(), popen=fake).dispatch(doc_id, wait=True)
    assert "--resume" not in fake.procs[0].argv
    assert registry.get(doc_id)["session_id"] == "sess-x"


def test_new_session_is_forced_even_with_a_session(doc):
    fake = FakePopen(ok("sess-y"))
    registry.replace_annotations(doc, [note("n1", "X")])
    sender.Sender(Config(), popen=fake).dispatch(doc, fresh=True, wait=True)
    assert "--resume" not in fake.procs[0].argv
    assert fake.procs[0].stdin_text.startswith("You did not produce")


def test_a_failed_send_makes_the_notes_sendable_again(doc):
    fake = FakePopen(lambda proc: (2, "", "boom: API overloaded"))
    registry.replace_annotations(doc, [note("n1", "X")])
    sender.Sender(Config(), popen=fake).dispatch(doc, wait=True)
    entry = registry.get(doc)
    assert entry["status"] == "annotated"
    assert "API overloaded" in entry["last_error"]
    assert [a["sent_at"] for a in registry.list_annotations(doc)] == [""]


def test_a_timeout_kills_the_group_and_restores(doc, monkeypatch):
    killed = []
    monkeypatch.setattr(claude, "kill_group", lambda proc: killed.append(proc.pid))
    calls = {"n": 0}

    def slow(proc):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.TimeoutExpired("claude", 5)
        return (-9, "", "")

    fake = FakePopen(slow)
    registry.replace_annotations(doc, [note("n1", "X")])
    sender.Sender(Config(send_timeout=5), popen=fake).dispatch(doc, wait=True)
    assert killed == [424242]
    assert registry.get(doc)["status"] == "annotated"
    assert "exceeded 5s" in registry.get(doc)["last_error"]


def test_dispatch_returns_before_the_session_ends_and_refuses_a_second(doc):
    release = threading.Event()

    def blocked(proc):
        assert release.wait(10)
        return (0, json.dumps({"session_id": "s"}), "")

    s = sender.Sender(Config(), popen=FakePopen(blocked))
    registry.replace_annotations(doc, [note("n1", "X")])
    result = s.dispatch(doc)
    assert result["count"] == 1
    assert registry.get(doc)["status"] == "sent"
    assert s.running() == {doc}
    registry.replace_annotations(doc, registry.list_annotations(doc) + [note("n2", "Y")])
    with pytest.raises(sender.Busy):
        s.dispatch(doc)
    release.set()
    for _ in range(100):
        if not s.running():
            break
        threading.Event().wait(0.05)
    assert not s.running()
    # n2 arrived while the session ran: still waiting, so not "answered".
    assert registry.get(doc)["status"] == "annotated"


def test_nothing_to_send_is_refused_without_launching(doc):
    fake = FakePopen()
    with pytest.raises(sender.NothingToSend):
        sender.Sender(Config(), popen=fake).dispatch(doc)
    assert fake.procs == []


def test_unreadable_output_and_is_error_are_failures(doc):
    for script in (lambda p: (0, "not json", ""),
                   lambda p: (0, json.dumps({"is_error": True, "result": "nope"}), "")):
        registry.replace_annotations(doc, [note("n1", "X")])
        sender.Sender(Config(), popen=FakePopen(script)).dispatch(doc, wait=True)
        assert registry.get(doc)["status"] == "annotated"
        assert registry.pending(registry.list_annotations(doc))


def test_recover_stale_restores_a_batch_left_sent(doc):
    registry.replace_annotations(doc, [note("n1", "X"), note("n2", "Y")])
    registry.mark_sent(doc, ["n1"], "OLD")
    registry.mark_sent(doc, ["n2"], "LIVE")
    registry.update(doc, status="sent", sent_at="LIVE")
    assert sender.recover_stale() == [doc]
    assert [a["sent_at"] for a in registry.list_annotations(doc)] == ["OLD", ""]
    assert registry.get(doc)["status"] == "annotated"


def test_kill_group_refuses_pids_zero_and_one(monkeypatch):
    calls = []
    monkeypatch.setattr(claude.os, "killpg", lambda pid, sig: calls.append(pid))

    class P:
        pid = 0
    for pid in (0, 1):
        P.pid = pid
        claude.kill_group(P())  # type: ignore[arg-type]
    assert calls == []
    P.pid = 4242
    claude.kill_group(P())  # type: ignore[arg-type]
    assert calls == [4242]
