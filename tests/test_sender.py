"""Where the notes go: to the open session that listens, else to a terminal tab.

The tab opener is a double with the real signature
(`test_the_opener_double_matches_the_real_one`); the board of listeners and
`claude.resumable` are the real ones, the latter on transcripts written in
the test's own folder (conftest isolates `~/.claude/projects`).
"""
from __future__ import annotations

import inspect
import threading
import time

import pytest

from annotate import claude, listeners, registry, sender, terminal
from annotate.config import Config
from fakes import RecordingOpener, transcript

BODY = "<h1>Intro</h1><p>Alpha text.</p><h2>Part two</h2><p>Beta text.</p>"


def note(id_, text, selector="body > p:nth-of-type(1)", quote="Alpha text."):
    return {"id": id_, "selector": selector, "quote": quote, "note": text,
            "offset_x": 1, "offset_y": 2}


@pytest.fixture
def doc(make_doc):
    return registry.register(make_doc(BODY), session="sess-1")["id"]


@pytest.fixture
def opener():
    return RecordingOpener()


@pytest.fixture
def send(opener):
    return sender.Sender(Config(), opener=opener)


def listen(board, doc_id, session="sess-live", cwd="/"):
    """A session running `annotate wait`: returns (thread, box)."""
    box: dict[str, object] = {}

    def run():
        box["waiter"] = board.wait(doc_id, session, cwd, lambda: True, 10)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(200):
        if board.listening(doc_id):
            break
        time.sleep(0.01)
    return thread, box


def test_the_opener_double_matches_the_real_one():
    real = inspect.signature(terminal.open_session)
    fake = inspect.signature(RecordingOpener.__call__)
    assert list(real.parameters) == [p for p in fake.parameters if p != "self"]


def test_an_open_session_receives_only_the_unsent_notes(doc, send, opener):
    registry.replace_annotations(doc, [note("n1", "already handled")])
    registry.mark_sent(doc, ["n1"], "T0")
    registry.replace_annotations(doc, [note("n1", "already handled"),
                                       note("n2", "shorten this")])
    thread, box = listen(send.board, doc, session="sess-live", cwd="/tmp")
    result = send.dispatch(doc)
    thread.join(5)
    assert result["target"] == "session" and result["count"] == 1
    prompt = box["waiter"].prompt  # type: ignore[union-attr]
    assert "shorten this" in prompt and "already handled" not in prompt
    assert opener.calls == []                       # no tab when a session listens
    entry = registry.get(doc)
    assert (entry["status"], entry["delivered_to"]) == ("delivered", "session")
    assert entry["session_id"] == "sess-live" and entry["cwd"] == "/tmp"
    assert registry.pending(registry.list_annotations(doc)) == []


def test_without_a_listener_the_producing_session_is_resumed_in_a_tab(doc, send, opener):
    transcript(claude.projects_dir(), "sess-1")
    registry.replace_annotations(doc, [note("n1", "shorten this")])
    result = send.dispatch(doc)
    assert result == {"doc_id": doc, "count": 1, "target": "terminal",
                      "session": "sess-1", "fresh": False}
    [call] = opener.calls
    assert call["resume"] == "sess-1" and call["cwd"] == registry.get(doc)["cwd"]
    assert "shorten this" in call["prompt"] and "read it first" not in call["prompt"]
    assert registry.get(doc)["status"] == "delivered"
    assert registry.get(doc)["delivered_to"] == "terminal"


def test_a_session_gone_from_this_machine_opens_a_new_one_with_the_preamble(doc, send, opener):
    registry.replace_annotations(doc, [note("n1", "shorten this")])
    send.dispatch(doc)                              # no transcript for sess-1
    [call] = opener.calls
    assert call["resume"] is None
    assert call["prompt"].startswith("You did not produce this document")


def test_new_session_never_reaches_the_listening_one(doc, send, opener):
    transcript(claude.projects_dir(), "sess-1")
    registry.replace_annotations(doc, [note("n1", "fresh eyes please")])
    thread, box = listen(send.board, doc)
    result = send.dispatch(doc, fresh=True)
    assert result["target"] == "terminal" and result["fresh"] is True
    assert opener.calls[0]["resume"] is None
    assert send.board.listening(doc), "the open session must not be handed fresh notes"
    assert send.board.deliver(doc, "release") is not None
    thread.join(5)


def test_a_tab_that_cannot_open_leaves_the_notes_sendable(doc, opener):
    failing = sender.Sender(Config(), opener=RecordingOpener(
        fail=terminal.TerminalError("wt.exe exited 1")))
    registry.replace_annotations(doc, [note("n1", "x")])
    with pytest.raises(sender.SendError, match="wt.exe exited 1"):
        failing.dispatch(doc)
    assert len(registry.pending(registry.list_annotations(doc))) == 1
    entry = registry.get(doc)
    assert entry["status"] == "annotated" and "wt.exe exited 1" in entry["last_error"]


def test_three_rounds_never_repeat_a_note(doc, send):
    seen = []
    for rank in range(3):
        current = [note(f"n{i}", f"note {i}") for i in range(rank + 1)]
        registry.replace_annotations(doc, current)
        thread, box = listen(send.board, doc)
        send.dispatch(doc)
        thread.join(5)
        seen.append(box["waiter"].prompt)  # type: ignore[union-attr]
    for rank, prompt in enumerate(seen):
        assert f"note {rank}" in prompt
        assert all(f"note {i}" not in prompt for i in range(rank))


def test_nothing_to_send_is_refused_without_opening_anything(doc, send, opener):
    with pytest.raises(sender.NothingToSend):
        send.dispatch(doc)
    assert opener.calls == []


def test_the_notes_tell_the_session_how_to_get_the_next_ones(doc, send, opener):
    registry.replace_annotations(doc, [note("n1", "x")])
    send.dispatch(doc)
    assert f"annotate wait {doc}`" in opener.calls[0]["prompt"]


def test_a_double_click_hands_the_notes_over_once(doc, opener):
    """Two sends racing on one listener: the second must find nothing left,
    not open a tab with the very notes the open session just received."""
    class SlowBoard(listeners.Board):
        def deliver(self, doc_id, prompt):
            time.sleep(0.3)
            return super().deliver(doc_id, prompt)

    send = sender.Sender(Config(), board=SlowBoard(), opener=opener)
    registry.replace_annotations(doc, [note("n1", "once only")])
    thread, _ = listen(send.board, doc)
    outcomes: list[object] = []

    def click():
        try:
            outcomes.append(send.dispatch(doc)["target"])
        except sender.NothingToSend:
            outcomes.append("nothing")

    clicks = [threading.Thread(target=click) for _ in range(2)]
    for c in clicks:
        c.start()
        time.sleep(0.05)
    for c in clicks:
        c.join(5)
    thread.join(5)
    assert sorted(outcomes) == ["nothing", "session"]  # type: ignore[type-var]
    assert opener.calls == []
