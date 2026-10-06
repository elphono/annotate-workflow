"""Posting into the inbox of an open session (see inbox.py).

The receiving end is a real Unix socket in the test's folder, registered in
an isolated `~/.claude/sessions` (conftest): never one of the user's sessions.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from annotate import inbox, registry, sender
from annotate.config import Config
from fakes import RecordingOpener


class Inbox:
    """A listening socket that keeps every line it receives."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[dict] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(5)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            data = b""
            with conn:
                while chunk := conn.recv(65536):
                    data += chunk
            self.lines += [json.loads(x) for x in data.decode().splitlines() if x]

    def wait(self, count: int = 1) -> None:
        for _ in range(300):
            if len(self.lines) >= count:
                return
            threading.Event().wait(0.01)

    def close(self) -> None:
        self._srv.close()


def register_session(session_id: str, socket_path: Path, *, pid: int | None = None,
                     proc_start: str | None = None, name: str = "session-c1",
                     updated: int = 1) -> None:
    pid = os.getpid() if pid is None else pid
    start = inbox._proc_start(os.getpid()) if proc_start is None else proc_start
    inbox.sessions_dir().mkdir(parents=True, exist_ok=True)
    (inbox.sessions_dir() / f"{pid}-{name}.json").write_text(json.dumps({
        "pid": pid, "sessionId": session_id, "procStart": start, "name": name,
        "messagingSocketPath": str(socket_path), "kind": "interactive",
        "updatedAt": updated}))


@pytest.fixture
def box(tmp_path):
    receiver = Inbox(tmp_path / "s.sock")
    yield receiver
    receiver.close()


def test_the_line_is_the_one_claude_code_writes_and_claims_no_permission_class():
    message = json.loads(inbox.line('notes with "quotes" and é'))
    assert (message["msgV"], message["type"], message["priority"]) == (1, "user", "next")
    content = message["message"]["content"]
    assert message["message"]["role"] == "user"
    assert content.startswith('<cross-session-message from="annotate" from-name="annotate">\n')
    assert content.endswith('\n</cross-session-message>')
    assert 'notes with "quotes" and é' in content
    assert "from-mode" not in content, "claiming a class would skip the approval dialog"


def test_only_a_live_process_of_that_session_counts(box, tmp_path):
    register_session("s-1", box.path, name="live")
    register_session("s-1", box.path, pid=2 ** 22 + 7, name="dead")          # no such pid
    register_session("s-1", box.path, proc_start="1", name="reused")         # pid reused
    register_session("s-1", tmp_path / "no.sock", name="no-socket")
    register_session("s-2", box.path, name="other-session")
    assert [e["name"] for e in inbox.open_sessions("s-1")] == ["live"]


def test_an_open_session_that_does_not_listen_gets_the_notes_in_its_inbox(box, make_doc):
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "x",
                                        "note": "fix the title"}])
    register_session("s-1", box.path)
    opener = RecordingOpener()
    result = sender.Sender(Config(), opener=opener).dispatch(doc)
    box.wait()
    assert result["target"] == "inbox" and result["name"] == "session-c1"
    assert opener.calls == [], "the session is open: never a second tab"
    assert "fix the title" in box.lines[0]["message"]["content"]
    assert registry.pending(registry.list_annotations(doc)) == []
    assert "approve the message" in sender.describe(result)


def test_the_most_recently_active_copy_of_a_twice_open_session_is_chosen(tmp_path, make_doc):
    old, recent = Inbox(tmp_path / "old.sock"), Inbox(tmp_path / "recent.sock")
    try:
        register_session("s-1", old.path, name="old", updated=100)
        register_session("s-1", recent.path, name="recent", updated=200)
        doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
        registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                            "note": "n"}])
        assert sender.Sender(Config(), opener=RecordingOpener()).dispatch(doc)["name"] == "recent"
        recent.wait()
        assert len(recent.lines) == 1 and old.lines == []
    finally:
        old.close()
        recent.close()


def test_a_refusing_inbox_keeps_the_notes_and_opens_no_tab(tmp_path, make_doc):
    dead = tmp_path / "dead.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(dead))
    srv.close()                              # the file exists, nobody listens
    register_session("s-1", dead)
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                        "note": "n"}])
    opener = RecordingOpener()
    with pytest.raises(sender.SendError, match="inbox refused"):
        sender.Sender(Config(), opener=opener).dispatch(doc)
    assert opener.calls == []
    assert len(registry.pending(registry.list_annotations(doc))) == 1


def test_a_listening_session_still_comes_first(box, make_doc):
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                        "note": "n"}])
    register_session("s-1", box.path)
    send = sender.Sender(Config(), opener=RecordingOpener())
    got: dict = {}
    waiter = threading.Thread(target=lambda: got.update(
        w=send.board.wait(doc, "s-1", "/", lambda: True, 10)), daemon=True)
    waiter.start()
    for _ in range(200):
        if send.board.listening(doc):
            break
        threading.Event().wait(0.01)
    assert send.dispatch(doc)["target"] == "session"
    waiter.join(5)
    assert box.lines == []


def test_new_session_never_posts_into_the_open_one(box, make_doc):
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                        "note": "n"}])
    register_session("s-1", box.path)
    opener = RecordingOpener()
    assert sender.Sender(Config(), opener=opener).dispatch(doc, fresh=True)["target"] == "terminal"
    assert box.lines == [] and len(opener.calls) == 1
