"""Open Claude Code sessions waiting for the notes of a document.

A session that wants the user's notes runs `annotate wait <id>` as a
BACKGROUND command. That command holds a long-poll request on the daemon
(`POST /api/docs/<id>/wait`); when the user clicks "Send", the daemon hands
the prompt to that request, the command prints it and exits, and Claude Code
wakes the session up with the output. The notes land in the conversation the
user is looking at, instead of a resumed copy of it running out of sight
(measured 2026-10-05: the first real send resumed the producing session with
`claude -p`, invisibly, for 1.71 USD, while the user waited in front of the
open one).

    session  ── annotate wait <id> (background) ──► POST …/wait ─┐ held
    browser  ── Send ──► Sender.dispatch ──► Board.deliver ──────┘ answered
                                              │ nobody listening
                                              └──► terminal tab (see terminal.py)

**A waiter whose client went away is never handed notes.** A session that
ends kills its background command, and the held request then belongs to
nobody; delivering to it would mark the notes sent and lose them in silence.
Each waiter carries an `alive()` probe (the socket is checked for EOF), run
by `deliver` before choosing it and by `wait` once per `STEP`.

**Taking a waiter is atomic.** `deliver` removes the waiter from the board
under the lock BEFORE waking it, and `wait` removes itself under the same
lock only if nothing was delivered: a note can be handed to one waiter at
most, and never to a waiter that already gave up.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

STEP = 1.0


class ListenerError(Exception):
    """A waiter could not be registered."""


@dataclass(eq=False)
class Waiter:
    doc_id: str
    session: str
    cwd: str
    alive: Callable[[], bool]
    event: threading.Event = field(default_factory=threading.Event)
    prompt: str | None = None


class Board:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[str, list[Waiter]] = {}

    def listening(self, doc_id: str) -> bool:
        """True if at least one live session waits for this document."""
        with self._lock:
            return any(w.alive() for w in self._waiters.get(doc_id, []))

    def wait(self, doc_id: str, session: str, cwd: str,
             alive: Callable[[], bool], timeout: float) -> Waiter | None:
        """Block until notes are delivered (the waiter, prompt set), the
        client goes away, or `timeout` elapses (None in both cases)."""
        if timeout <= 0:
            raise ListenerError("the wait timeout must be positive")
        waiter = Waiter(doc_id, session, cwd, alive)
        with self._lock:
            self._waiters.setdefault(doc_id, []).append(waiter)
        deadline = time.monotonic() + timeout
        while not waiter.event.wait(min(STEP, max(0.0, deadline - time.monotonic()))):
            if time.monotonic() >= deadline or not alive():
                break
        with self._lock:
            if waiter.event.is_set():
                return waiter
            self._drop(waiter)
            return None

    def deliver(self, doc_id: str, prompt: str) -> Waiter | None:
        """Hand `prompt` to the oldest live waiter of `doc_id`; None if none."""
        with self._lock:
            for waiter in list(self._waiters.get(doc_id, [])):
                self._drop(waiter)
                if waiter.alive():
                    waiter.prompt = prompt
                    waiter.event.set()
                    return waiter
            return None

    def _drop(self, waiter: Waiter) -> None:
        queue = self._waiters.get(waiter.doc_id, [])
        if waiter in queue:
            queue.remove(waiter)
        if not queue:
            self._waiters.pop(waiter.doc_id, None)
