"""What this machine knows about a Claude Code conversation.

`resumable(session)` decides whether the terminal tab resumes the producing
session (`claude --resume <id>`) or opens a new one with a "read the document
first" preamble. Asking `claude --resume` and reading its error is not an
option any more: the tab is interactive, and a session that is gone would
leave the user in front of "No conversation found" instead of their notes.

The criterion is MEASURED, not guessed (remarkable-sync, 2026-09-15, Claude
Code 2.1.272, `rmpapier/transfert.py`): a transcript
`~/.claude/projects/<folder>/<id>.jsonl` is resumed from any folder as soon as
it holds one `user` or `assistant` line, in the compact form Claude Code
writes (`"type":"user"`, no space after the colon); an empty transcript, or
one reduced to its title line, is refused. The folder of the transcript does
not matter, so every project folder is searched.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

MARKERS = ('"type":"user"', '"type":"assistant"')
SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{0,63}$")


class ClaudeError(Exception):
    """Reserved: nothing in this module raises today."""


def projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def resumable(session: str) -> bool:
    """True if `claude --resume <session>` would find the conversation here."""
    if not session or not SESSION_ID.match(session):
        return False
    for transcript in projects_dir().glob(f"*/{session}.jsonl"):
        try:
            text = transcript.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(marker in text for marker in MARKERS):
            return True
    return False


# -- who wrote a file, and what a session is called ---------------------------

TITLE_KINDS = (('"type":"custom-title"', "customTitle"), ('"type":"ai-title"', "aiTitle"))
# A tool call is recorded when the model emits it, the file is written when it
# runs: the call precedes the write, by the length of the command at most.
WRITE_SLACK = 5.0
WRITE_WINDOW = 3600.0


def transcript_of(session: str) -> Path | None:
    if not session or not SESSION_ID.match(session):
        return None
    found = sorted(projects_dir().glob(f"*/{session}.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


class _Titles:
    """Session titles, read INCREMENTALLY: a live transcript grows by megabytes
    and the tray asks every few seconds; only what was appended is read."""

    def __init__(self) -> None:
        self._seen: dict[Path, tuple[int, str, str]] = {}   # offset, custom, ai

    def get(self, session: str) -> str:
        path = transcript_of(session)
        if path is None:
            return ""
        offset, custom, ai = self._seen.get(path, (0, "", ""))
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            return custom or ai
        end = chunk.rfind(b"\n") + 1          # a half-written last line waits
        for raw in chunk[:end].splitlines():
            line = raw.decode("utf-8", "replace")
            for marker, key in TITLE_KINDS:
                if marker in line:
                    try:
                        value = json.loads(line).get(key)
                    except ValueError:
                        continue
                    if isinstance(value, str) and value.strip():
                        if key == "customTitle":
                            custom = value.strip()
                        else:
                            ai = value.strip()
        self._seen[path] = (offset + end, custom, ai)
        return custom or ai


_titles = _Titles()


def title(session: str) -> str:
    """The name the user sees for a session: its /rename title, else the
    title Claude Code generated, else ""."""
    return _titles.get(session)


def _stamp(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def writer_of(path: Path, mtime: float) -> tuple[str, str] | None:
    """(session id, working directory) of the session that wrote `path`.

    For documents the hook did not see: written by Bash (`cp` from a
    scratchpad, a script), often by a subagent (measured 2026-10-05: an agent
    composed the timeline in its scratchpad and copied it into `docs/`). The
    answer is the LATEST tool call naming the file by its basename, made at
    most `WRITE_SLACK` after the write and `WRITE_WINDOW` before it. Later
    calls only read it (`ls`, a review) and must not claim it. Subagent
    transcripts carry the parent's `sessionId`, which is the conversation
    the user can see.
    """
    name = path.name
    root = projects_dir()
    candidates = [*root.glob("*/*.jsonl"), *root.glob("*/*/subagents/*.jsonl")]
    best: tuple[float, str, str] | None = None
    for transcript in candidates:
        try:
            if transcript.stat().st_mtime < mtime - WRITE_SLACK:
                continue
            handle = transcript.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if name not in line or '"tool_use"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                when = _stamp(entry.get("timestamp"))
                if when is None or not mtime - WRITE_WINDOW <= when <= mtime + WRITE_SLACK:
                    continue
                content = (entry.get("message") or {}).get("content") or []
                if not any(isinstance(c, dict) and c.get("type") == "tool_use"
                           and name in json.dumps(c.get("input"), ensure_ascii=False)
                           for c in content):
                    continue
                session = str(entry.get("sessionId") or "")
                if SESSION_ID.match(session) and (best is None or when > best[0]):
                    best = (when, session, str(entry.get("cwd") or ""))
    return (best[1], best[2]) if best else None
