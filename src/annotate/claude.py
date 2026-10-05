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

import re
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
