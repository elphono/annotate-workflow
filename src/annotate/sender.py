"""Send the unsent annotations of a document to a Claude Code session.

    dispatch(id)                      (HTTP thread, returns at once)
      ├─ prompt.build(unsent notes)
      ├─ mark them sent (stamp)      → the browser greys them
      ├─ status = sent
      └─ thread ─► claude -p --resume <session> … (stdin = prompt)
                     ├─ "No conversation found" → same prompt, NEW session,
                     │                            with a "read it first" preamble
                     ├─ exit 0  → status answered (annotated if new notes
                     │            arrived meanwhile), session_id updated
                     └─ failure → batch un-marked, status annotated, logged

**The daemon never blocks on a session**: `dispatch` returns as soon as the
thread is started. **One session per document at a time**: a second dispatch
while one runs raises `Busy` (HTTP 409), instead of paying twice for the same
notes.

**A failed send is undone, not retried.** The batch is identified by its
stamp (`sent_at`); clearing it makes the notes sendable again, and the user
decides whether to click again. Retrying automatically would re-run a session
that may already have edited the file (remarkable-sync, 2026-08-31: a retry
loop placed outside the unit that knows the difference re-deposited the same
document three times).
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import claude, prompt, registry
from .config import Config

log = logging.getLogger("annotate")


class SendError(Exception):
    """A send could not be started."""


class Busy(SendError):
    """A session is already running for this document."""


class NothingToSend(SendError):
    """The document has no unsent annotation."""


PopenFactory = Callable[..., Any]


def _stamp() -> str:
    # Microseconds: the stamp identifies a batch, two must never collide.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Sender:
    def __init__(self, cfg: Config, popen: PopenFactory = subprocess.Popen) -> None:
        self._cfg = cfg
        self._popen = popen
        self._lock = threading.Lock()
        self._running: dict[str, threading.Thread] = {}

    def running(self) -> set[str]:
        with self._lock:
            return set(self._running)

    def dispatch(self, doc_id: str, *, fresh: bool = False,
                 wait: bool = False) -> dict[str, Any]:
        entry = registry.get(doc_id)
        with self._lock:
            if doc_id in self._running:
                raise Busy(f"a session is already running for {doc_id}")
            items = registry.pending(registry.list_annotations(doc_id))
            if not items:
                raise NothingToSend(
                    f"{doc_id} has no unsent annotation: Alt+click in the "
                    f"document to add one")
            use_fresh = fresh or not entry.get("session_id")
            try:
                prompt.build(entry, items, fresh=use_fresh)  # fail now, not in the thread
            except prompt.PromptError as exc:
                raise SendError(str(exc)) from exc
            stamp = _stamp()
            registry.mark_sent(doc_id, [a["id"] for a in items], stamp)
            registry.update(doc_id, status="sent", sent_at=stamp, last_error="")
            thread = threading.Thread(
                target=self._run, args=(doc_id, entry, items, stamp, use_fresh),
                name=f"send-{doc_id}", daemon=True)
            self._running[doc_id] = thread
            thread.start()
        log.info("sending %d note(s) of %s to %s", len(items), doc_id,
                 "a new session" if use_fresh else f"session {entry['session_id']}")
        if wait:
            thread.join()
        return {"doc_id": doc_id, "count": len(items), "fresh": use_fresh,
                "stamp": stamp}

    # -- background -------------------------------------------------------
    def _run(self, doc_id: str, entry: dict[str, Any],
             items: list[dict[str, Any]], stamp: str, fresh: bool) -> None:
        try:
            try:
                session, cost = self._launch(entry, items,
                                             None if fresh else entry["session_id"], fresh)
            except claude.SessionGone:
                log.warning("session %s of %s is gone on this machine: opening a "
                            "new session with the same notes",
                            entry.get("session_id"), doc_id)
                session, cost = self._launch(entry, items, None, True)
            still = registry.pending(registry.list_annotations(doc_id))
            registry.update(doc_id,
                            status="annotated" if still else "answered",
                            session_id=session or entry.get("session_id", ""),
                            answered_at=registry.now_iso(), last_error="",
                            last_cost_usd=cost)
            log.info("%s answered by session %s (cost reported: %s USD)",
                     doc_id, session, "unknown" if cost is None else f"{cost:.4f}")
        except Exception as exc:  # the thread must always settle the status
            log.error("send of %s failed: %s", doc_id, exc)
            try:
                count = registry.unmark_sent(doc_id, stamp)
                registry.update(doc_id, status="annotated", last_error=str(exc)[:500])
                log.info("%d note(s) of %s are sendable again", count, doc_id)
            except registry.RegistryError as inner:
                log.error("could not restore %s after the failure: %s", doc_id, inner)
        finally:
            with self._lock:
                self._running.pop(doc_id, None)

    def _launch(self, entry: dict[str, Any], items: list[dict[str, Any]],
                resume: str | None, fresh: bool) -> tuple[str, float | None]:
        """Run one session; return (session id, cost in USD as claude reports it)."""
        text = prompt.build(entry, items, fresh=fresh)
        command = claude.argv(self._cfg.claude_bin, resume)
        try:
            proc = self._popen(command, cwd=entry["cwd"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True,
                               env=claude.session_env())
        except OSError as exc:
            raise claude.LaunchError(f"cannot start {command[0]}: {exc}") from exc
        # Same except order as remarkable-sync: `Exception` before
        # `BaseException`, so that Ctrl-C propagates untouched.
        try:
            stdout, stderr = proc.communicate(text, timeout=self._cfg.send_timeout)
        except subprocess.TimeoutExpired as exc:
            claude.kill_group(proc)
            proc.communicate()
            raise claude.LaunchError(
                f"the session exceeded {self._cfg.send_timeout}s; its process "
                f"group was killed") from exc
        except Exception as exc:
            claude.kill_group(proc)
            proc.communicate()
            raise claude.LaunchError(f"the session failed while running: {exc}") from exc
        except BaseException:
            claude.kill_group(proc)
            proc.communicate()
            raise
        if proc.returncode != 0:
            if resume and claude.SESSION_GONE_PATTERN in (stderr or "") + (stdout or ""):
                raise claude.SessionGone(resume)
            raise claude.LaunchError(
                f"claude exited with {proc.returncode}: "
                f"{(stderr or stdout or '').strip()[:400]}")
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise claude.LaunchError(f"unreadable claude output: {stdout[:200]!r}") from exc
        if not isinstance(data, dict):
            raise claude.LaunchError(f"unexpected claude output: {stdout[:200]!r}")
        if data.get("is_error"):
            raise claude.LaunchError(f"claude reported an error: "
                                     f"{str(data.get('result', ''))[:400]}")
        cost = data.get("total_cost_usd")
        return (str(data.get("session_id", "")),
                float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                else None)


def recover_stale() -> list[str]:
    """At daemon start, no session can be running: undo every `sent` batch.

    A `sent` status that survives a daemon restart belongs to a session the
    restart killed (systemd stops the whole cgroup). Its notes become
    sendable again rather than staying greyed forever.
    """
    recovered = []
    for doc_id, entry in registry.all_docs().items():
        if entry.get("status") == "sent":
            stamp = entry.get("sent_at", "")
            if stamp:
                registry.unmark_sent(doc_id, stamp)
            registry.update(doc_id, status="annotated",
                            last_error="interrupted: the daemon restarted during the session")
            recovered.append(doc_id)
            log.warning("%s was 'sent' when the daemon started: its notes are "
                        "sendable again", doc_id)
    return recovered
