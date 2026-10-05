"""What the server needs to know about an HTML document, standard library only.

Four jobs, all on the document as it is ON DISK (the producing session edits
it in place, so nothing here is cached):

- `title_of`: the `<title>`, for the registry and the prompt;
- `inject_overlay`: the page served to the browser, with the overlay script
  and a `<base>` so that relative resources (`figures/x.png`) keep loading
  from `/docs/<id>/files/`;
- `resolve`: find the element an annotation's `selector` points to;
- `heading_before`: the nearest preceding h1..h6 of that element, which is
  what lets the receiving session find the passage again.

**The selector grammar is the one `static/annotate.js` writes, and only that
one**: `#id` or `body`, then ` > tag:nth-of-type(n)` segments. Resolving it
server-side means rebuilding the tree the browser built. `html.parser` does
not do HTML5 tree construction, so the few implied-tag rules that change
`nth-of-type` counts in real documents are applied here: void elements, an
open `<p>` closed by a block, `<li>`/`<dt>`/`<dd>`/`<tr>`/`<td>` closing
their open sibling, and the `<tbody>` browsers insert around bare `<tr>`.
When the trees still disagree, `resolve` returns None and the annotation is
sent with its quote only: never dropped (see `prompt.py`).
"""
from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser


class HtmlDocError(Exception):
    """A document could not be read or parsed."""


VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "param", "source", "track", "wbr"})
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Opening one of these closes an open <p> (HTML5 "close a p element").
CLOSES_P = frozenset({
    "address", "article", "aside", "blockquote", "details", "dialog", "div",
    "dl", "fieldset", "figcaption", "figure", "footer", "form", "header",
    "hgroup", "hr", "main", "menu", "nav", "ol", "p", "pre", "section",
    "table", "ul"} | HEADINGS)
SCOPE_BOUNDARY = frozenset({"html", "table", "td", "th", "caption",
                            "template", "button", "object", "applet",
                            "marquee", "svg"})


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str]
    parent: "Node | None"
    order: int
    children: list["Node"] = field(default_factory=list)
    text: list[str] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None, 0)
        self.stack: list[Node] = [self.root]
        self.count = 0
        self.title_parts: list[str] = []
        self.in_title = False

    # -- helpers ---------------------------------------------------------
    def _open(self, tag: str, attrs: dict[str, str]) -> Node:
        self.count += 1
        node = Node(tag, attrs, self.stack[-1], self.count)
        self.stack[-1].children.append(node)
        return node

    def _close_in_scope(self, tags: frozenset[str],
                        boundary: frozenset[str]) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            current = self.stack[i].tag
            if current in tags:
                del self.stack[i:]
                return
            if current in boundary:
                return

    # -- HTMLParser hooks -------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: (v if v is not None else "") for k, v in attrs}
        if tag in CLOSES_P:
            self._close_in_scope(frozenset({"p"}), SCOPE_BOUNDARY)
        if tag == "li":
            self._close_in_scope(frozenset({"li"}), SCOPE_BOUNDARY | {"ol", "ul"})
        elif tag in ("dt", "dd"):
            self._close_in_scope(frozenset({"dt", "dd"}), SCOPE_BOUNDARY | {"dl"})
        elif tag == "tr":
            self._close_in_scope(frozenset({"tr"}), frozenset({"table"}))
            if self.stack[-1].tag == "table":
                self.stack.append(self._open("tbody", {}))
        elif tag in ("td", "th"):
            self._close_in_scope(frozenset({"td", "th"}),
                                 frozenset({"tr", "table"}))
        node = self._open(tag, values)
        if tag == "title":
            self.in_title = True
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<div/>` is NOT self-closing in HTML; only void and foreign
        # elements are. Inside an SVG, `<rect/>` is closed.
        in_svg = any(n.tag == "svg" for n in self.stack)
        self.handle_starttag(tag, attrs)
        if tag not in VOID and (in_svg or tag == "svg") and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # A stray end tag is ignored, as browsers mostly do.

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        for node in self.stack:
            if node.tag in HEADINGS:
                node.text.append(data)


@dataclass
class Document:
    root: Node
    title: str
    by_order: list[Node]

    def find(self, predicate: Callable[[Node], bool]) -> "Node | None":
        for node in self.by_order:
            if predicate(node):
                return node
        return None

    @property
    def body(self) -> Node | None:
        return self.find(lambda n: n.tag == "body")


def parse(text: str) -> Document:
    builder = _TreeBuilder()
    try:
        builder.feed(text)
        builder.close()
    except Exception as exc:  # html.parser raises little, but never crash a request
        raise HtmlDocError(f"cannot parse HTML: {exc}") from exc
    nodes: list[Node] = []

    def walk(node: Node) -> None:
        for child in node.children:
            nodes.append(child)
            walk(child)

    walk(builder.root)
    nodes.sort(key=lambda n: n.order)
    title = " ".join("".join(builder.title_parts).split())
    return Document(builder.root, title, nodes)


def title_of(text: str, fallback: str) -> str:
    try:
        title = parse(text).title
    except HtmlDocError:
        title = ""
    return title or fallback


# -- selectors -------------------------------------------------------------

_SEGMENT = re.compile(r"^([a-zA-Z][a-zA-Z0-9-]*):nth-of-type\((\d+)\)$")
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6}\s?|.)")


def _css_unescape(ident: str) -> str:
    def one(match: re.Match[str]) -> str:
        token = match.group(1)
        stripped = token.strip()
        if re.fullmatch(r"[0-9a-fA-F]{1,6}", stripped):
            return chr(int(stripped, 16))
        return token
    return _CSS_ESCAPE.sub(one, ident)


def resolve(doc: Document, selector: str) -> Node | None:
    """The element `selector` designates, or None if it no longer exists."""
    parts = [p.strip() for p in selector.split(">")] if selector else []
    # A `>` inside an escaped id would break the split; ids with `>` are
    # pathological enough to accept the None.
    if not parts or not all(parts):
        return None
    head, rest = parts[0], parts[1:]
    current: Node | None
    if head.startswith("#"):
        wanted = _css_unescape(head[1:])
        current = doc.find(lambda n: n.attrs.get("id") == wanted)
    elif head in ("body", "html"):
        current = doc.find(lambda n: n.tag == head)
    else:
        return None
    for part in rest:
        if current is None:
            return None
        match = _SEGMENT.match(part)
        if not match:
            return None
        tag, index = match.group(1).lower(), int(match.group(2))
        same = [c for c in current.children if c.tag == tag]
        current = same[index - 1] if 1 <= index <= len(same) else None
    return current


def heading_before(doc: Document, node: Node) -> str:
    """Text of the nearest h1..h6 that starts at or before `node`."""
    best: Node | None = None
    for candidate in doc.by_order:
        if candidate.order > node.order:
            break
        if candidate.tag in HEADINGS:
            best = candidate
    if best is None:
        return ""
    return " ".join("".join(best.text).split())


# -- serving -----------------------------------------------------------------

_HEAD_OPEN = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html(?:\s[^>]*)?>", re.IGNORECASE)
_HAS_BASE = re.compile(r"<base\s", re.IGNORECASE)
_BODY_CLOSE = re.compile(r"</body\s*>", re.IGNORECASE)


def inject_overlay(text: str, doc_id: str) -> str:
    """The document as served: `<base>` for relative files, overlay at the end.

    The `<base>` makes `figures/x.png` load from `/docs/<id>/files/figures/x.png`.
    Its side effect on in-page links (`href="#section"` would resolve against
    the base, i.e. leave the page) is undone by the overlay script, which
    handles fragment-only links itself. A document that already declares its
    own `<base>` is left alone.
    """
    quoted = html.escape(doc_id, quote=True)
    if not _HAS_BASE.search(text):
        base = f'<base href="/docs/{quoted}/files/">'
        head = _HEAD_OPEN.search(text)
        anchor = head or _HTML_OPEN.search(text)
        if anchor:
            text = text[:anchor.end()] + base + text[anchor.end():]
        else:
            text = base + text
    overlay = (
        '\n<link rel="stylesheet" href="/static/annotate.css">'
        f'\n<script src="/static/annotate.js" data-annotate-doc="{quoted}" defer>'
        "</script>\n")
    closings = list(_BODY_CLOSE.finditer(text))
    if closings:
        at = closings[-1].start()
        return text[:at] + overlay + text[at:]
    return text + overlay
