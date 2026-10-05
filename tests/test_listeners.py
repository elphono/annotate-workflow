"""The board of open sessions waiting for notes (long-poll, see listeners.py)."""
from __future__ import annotations

import threading
import time

import pytest

from annotate import listeners


def _wait_in_thread(board, doc_id, alive=lambda: True, timeout=5.0, session="s1"):
    box: dict[str, object] = {}

    def run():
        box["waiter"] = board.wait(doc_id, session, "/repo", alive, timeout)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(200):
        if board.listening(doc_id):
            break
        time.sleep(0.01)
    return thread, box


def test_a_delivery_wakes_the_waiter_with_the_prompt():
    board = listeners.Board()
    thread, box = _wait_in_thread(board, "d1")
    taken = board.deliver("d1", "the notes")
    thread.join(5)
    assert taken is not None and taken.session == "s1"
    assert box["waiter"] is taken and taken.prompt == "the notes"
    assert not board.listening("d1")


def test_nobody_listening_means_no_delivery():
    board = listeners.Board()
    assert board.deliver("d1", "the notes") is None
    assert not board.listening("d1")


def test_a_waiter_of_another_document_is_not_taken():
    board = listeners.Board()
    thread, box = _wait_in_thread(board, "other", timeout=1.5)
    assert board.deliver("d1", "the notes") is None
    thread.join(5)
    assert box["waiter"] is None


def test_a_dead_client_is_never_handed_the_notes():
    board = listeners.Board()
    state = {"alive": True}
    thread, box = _wait_in_thread(board, "d1", alive=lambda: state["alive"])
    state["alive"] = False
    assert not board.listening("d1")
    assert board.deliver("d1", "the notes") is None
    thread.join(5)
    assert box["waiter"] is None


def test_the_wait_gives_up_when_its_client_goes_away(monkeypatch):
    monkeypatch.setattr(listeners, "STEP", 0.05)
    board = listeners.Board()
    state = {"alive": True}
    thread, box = _wait_in_thread(board, "d1", alive=lambda: state["alive"], timeout=30)
    state["alive"] = False
    thread.join(2)
    assert not thread.is_alive(), "a waiter whose client left must stop waiting"
    assert box["waiter"] is None


def test_a_timeout_returns_none_and_leaves_the_board_empty():
    board = listeners.Board()
    started = time.monotonic()
    assert board.wait("d1", "s", "/r", lambda: True, 0.3) is None
    assert time.monotonic() - started >= 0.25
    assert not board.listening("d1")


def test_three_rounds_each_reach_exactly_one_waiter():
    """Re-arming after each delivery: the third round must work like the first."""
    board = listeners.Board()
    for rank in range(3):
        thread, box = _wait_in_thread(board, "d1")
        assert board.deliver("d1", f"round {rank}") is not None
        thread.join(5)
        assert box["waiter"].prompt == f"round {rank}"  # type: ignore[union-attr]
        assert board.deliver("d1", "nobody left") is None


def test_two_waiters_get_one_delivery_each_oldest_first():
    board = listeners.Board()
    first, box1 = _wait_in_thread(board, "d1", session="first")
    second, box2 = _wait_in_thread(board, "d1", session="second")
    assert board.deliver("d1", "a").session == "first"  # type: ignore[union-attr]
    assert board.deliver("d1", "b").session == "second"  # type: ignore[union-attr]
    first.join(5)
    second.join(5)
    assert box1["waiter"].prompt == "a" and box2["waiter"].prompt == "b"  # type: ignore[union-attr]


def test_a_non_positive_timeout_is_refused():
    with pytest.raises(listeners.ListenerError):
        listeners.Board().wait("d1", "s", "/r", lambda: True, 0)
