"""The prompt sent to the session: one block per UNSENT annotation.

Each block carries what lets the session find the passage again without a
screen: the section heading (nearest preceding h1..h6, resolved here on the
current HTML), the quote captured in the browser, and the note.

**An annotation whose selector no longer resolves is still sent**, with its
quote and an explicit "anchor lost" line. Dropping it would lose the user's
note in silence, which is the failure mode this repository refuses first.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import htmldoc


class PromptError(Exception):
    """There is nothing to send, or the document cannot be read."""


INSTRUCTION = (
    "The user annotated the document you produced; address each note, edit "
    "the file in place at `{path}`, keep it a self-contained HTML, and reply "
    "briefly.")

FRESH_CONTEXT = (
    "You did not produce this document in this conversation; read it first "
    "(`{path}`), then act on the notes below.\n\n")


def build(entry: dict[str, Any], items: list[dict[str, Any]], *,
          fresh: bool = False) -> str:
    """Prompt for the `items` (already filtered to the unsent ones)."""
    if not items:
        raise PromptError("no unsent annotation: nothing to send")
    path = Path(entry["path"])
    try:
        doc = htmldoc.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, htmldoc.HtmlDocError) as exc:
        raise PromptError(f"cannot read {path}: {exc}") from exc

    lines = []
    if fresh:
        lines.append(FRESH_CONTEXT.format(path=path))
    lines.append(f"Document: \"{entry.get('title') or path.name}\"")
    lines.append(f"Path: {path}")
    lines.append("")
    lines.append(f"{len(items)} note(s) from the user:")
    for rank, item in enumerate(items, start=1):
        node = htmldoc.resolve(doc, item.get("selector", ""))
        lines.append("")
        lines.append(f"--- Note {rank} (pin #{item.get('number', rank)}) ---")
        if node is None:
            lines.append("Anchor: lost (the element it was pinned to no longer "
                         "exists; rely on the quote)")
        else:
            section = htmldoc.heading_before(doc, node)
            lines.append(f"Section: {section or '(before any heading)'}")
        quote = " ".join(str(item.get("quote", "")).split())
        lines.append(f"Quoted passage: \"{quote}\"" if quote
                     else "Quoted passage: (none captured)")
        lines.append("Note:")
        lines.append(str(item.get("note", "")).strip())
    lines.append("")
    lines.append(INSTRUCTION.format(path=path))
    return "\n".join(lines)
