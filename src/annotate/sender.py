"""Hand the unsent annotations of a document to a Claude Code session.

    dispatch(id)
      ├─ prompt.build(unsent notes)
      ├─ an open session listens (annotate wait)?
      │     yes ─► Board.deliver: the notes land in THAT conversation
      │     no  ─► terminal.open_session: a Windows Terminal tab resumes the
      │            producing session (or opens a new one), with the notes
      ├─ mark them sent (stamp)      → the browser greys them
      └─ status = delivered          → `answered` when the session rewrites the
                                       document (registry.register, via the hook)

**Nothing runs in the background any more.** Until 2026-10-05 a send resumed
the session with `claude -p`, out of sight: the user clicked "Send", saw
"2 notes sent to the producing session", and nothing else, while a copy of
the conversation edited the document and committed for 1.71 USD. Both paths
now end in a conversation the user can see.

**The notes are marked sent only once a session has them.** A tab that
cannot be opened leaves them sendable, with the reason in `last_error`; the
user decides whether to click again. Nothing retries on its own (the
remarkable-sync lesson of 2026-08-31: a retry loop outside the unit that
knows the difference re-deposited the same document three times).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import claude, inbox, listeners, prompt, registry, terminal
from .config import Config

log = logging.getLogger("annotate")


class SendError(Exception):
    """The notes could not be handed to any session."""


class NothingToSend(SendError):
    """The document has no unsent annotation."""


def _stamp() -> str:
    # Microseconds: the stamp identifies a batch, two must never collide.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def describe(result: dict[str, Any]) -> str:
    """Where the notes went, in one line: what the CLI prints, and what the
    overlay and the index page show (one wording, written once)."""
    count = result.get("count")
    if result.get("target") == "inbox":
        return (f"{count} note(s) posted to the open session {result.get('name')}: "
                f"approve the message in that terminal.")
    if result.get("target") == "session":
        session = result.get("session") or ""
        return f"{count} note(s) delivered to the open session {session}".rstrip() + \
            ": it answers there."
    if result.get("fresh"):
        return f"{count} note(s) opened in a terminal tab, in a NEW session."
    return (f"{count} note(s) opened in a terminal tab: no open session listened, "
            f"session {result.get('session')} resumed there.")


class Sender:
    def __init__(self, cfg: Config, board: listeners.Board | None = None,
                 opener: Any = terminal.open_session) -> None:
        self._cfg = cfg
        self.board = board or listeners.Board()
        self._open = opener
        # One dispatch at a time: a double click would otherwise read the same
        # unsent notes twice, deliver them once, and open a tab with them too.
        self._lock = threading.Lock()

    def dispatch(self, doc_id: str, *, fresh: bool = False) -> dict[str, Any]:
        with self._lock:
            return self._dispatch(doc_id, fresh)

    def _dispatch(self, doc_id: str, fresh: bool) -> dict[str, Any]:
        entry = registry.get(doc_id)
        items = registry.pending(registry.list_annotations(doc_id))
        if not items:
            raise NothingToSend(f"{doc_id} has no unsent annotation: Alt+click in "
                                f"the document to add one")
        if not fresh:
            text = self._build(entry, items, fresh=False)
            waiter = self.board.deliver(doc_id, text)
            if waiter is not None:
                self._settle(doc_id, items, target="session",
                             session_id=waiter.session or entry.get("session_id", ""),
                             cwd=waiter.cwd or entry["cwd"])
                log.info("%d note(s) of %s delivered to the open session %s",
                         len(items), doc_id, waiter.session or "(unknown id)")
                return {"doc_id": doc_id, "count": len(items), "target": "session",
                        "session": waiter.session}
            session_id = entry.get("session_id", "")
            opened = inbox.open_sessions(session_id) if session_id else []
            if opened:
                return self._post(doc_id, items, text, opened)
        folder = str(registry.usable_cwd(entry.get("cwd"), Path(entry["path"])))
        if folder != entry.get("cwd"):
            log.warning("%s: session folder %s is gone, using %s", doc_id,
                        entry.get("cwd"), folder)
            registry.update(doc_id, cwd=folder)
            entry = {**entry, "cwd": folder}
        session = "" if fresh else entry.get("session_id", "")
        resume = session if session and claude.resumable(session) else None
        text = self._build(entry, items, fresh=resume is None)
        try:
            program = self._open(doc_id, entry["cwd"], self._cfg.claude_bin, resume, text)
        except terminal.TerminalError as exc:
            registry.update(doc_id, last_error=str(exc)[:500])
            log.error("no session for %s: %s", doc_id, exc)
            raise SendError(f"no open session listens to this document, and no "
                            f"terminal could be opened: {exc}") from exc
        self._settle(doc_id, items, target="terminal")
        log.info("%d note(s) of %s opened in a %s (%s)", len(items), doc_id, program,
                 f"session {resume} resumed" if resume else "new session")
        return {"doc_id": doc_id, "count": len(items), "target": "terminal",
                "session": resume or "", "fresh": resume is None}

    def _post(self, doc_id: str, items: list[dict[str, Any]], text: str,
              opened: list[dict[str, Any]]) -> dict[str, Any]:
        """The session is open but does not listen: post into its inbox.

        Never a terminal tab from here, even when every post fails: the
        session IS open, and a tab would resume it a second time (the duplicate
        of 2026-10-06). The notes then stay sendable, with the reason."""
        errors = []
        for target in opened:                 # most recently active first
            try:
                inbox.post(target["messagingSocketPath"], text)
            except inbox.InboxError as exc:
                errors.append(str(exc))
                continue
            name = str(target.get("name") or target.get("pid"))
            self._settle(doc_id, items, target="inbox")
            log.info("%d note(s) of %s posted to the inbox of the open session %s",
                     len(items), doc_id, name)
            return {"doc_id": doc_id, "count": len(items), "target": "inbox",
                    "session": str(target.get("sessionId", "")), "name": name}
        reason = "; ".join(errors)
        registry.update(doc_id, last_error=reason[:500])
        raise SendError(f"the session is open but its inbox refused the notes "
                        f"(they stay to send): {reason}")

    def _build(self, entry: dict[str, Any], items: list[dict[str, Any]],
               fresh: bool) -> str:
        try:
            return prompt.build(entry, items, fresh=fresh)
        except prompt.PromptError as exc:
            raise SendError(str(exc)) from exc

    def _settle(self, doc_id: str, items: list[dict[str, Any]], target: str,
                **fields: Any) -> None:
        stamp = _stamp()
        registry.mark_sent(doc_id, [a["id"] for a in items], stamp)
        registry.update(doc_id, status="delivered", sent_at=stamp, delivered_to=target,
                        last_error="", **fields)
