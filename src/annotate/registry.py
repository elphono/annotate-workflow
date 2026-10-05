"""The registry of documents, and their annotations, on disk.

Layout under `config.data_dir()` (default `~/.local/share/annotate`):

    registry.json            {"version": 1, "docs": {id: entry}}
    registry.lock            flock target, never read
    annotations/<id>.json    {"doc_id": id, "annotations": [...]}

**Annotations never live next to the document**: the document sits in a git
repository, and a sidecar file would show up in `git status` and end up
committed.

**Two writers share these files**: the daemon (browser PUTs, sends finishing
in background threads) and the CLI (`annotate register`, called by the Claude
Code hook from any session). Every read-modify-write therefore runs under an
exclusive `flock` on `registry.lock`. `flock` locks belong to an open file
description, so two threads of the daemon opening the lock file separately
exclude each other as two processes would.

Every write is atomic (temporary file, then `os.replace`): a reader never sees
a truncated JSON. Pattern copied from remarkable-sync,
`rmpapier/etat.py::ecrire_atomiquement`, including the pid in the temporary
name so that two writers never rename each other's half-written file.

**The server is the authority on `sent_at`.** The browser replaces the whole
list on every edit (PUT); if it carried a stale copy (`sent_at` empty while a
send just marked the note), the note would be sent twice. `replace_annotations`
therefore keeps the stored `sent_at` of every annotation it already knows.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, htmldoc

STATUSES = ("new", "annotated", "sent", "answered")
MAX_TEXT = 20_000


class RegistryError(Exception):
    """The registry or an annotation file could not be read or written."""


class UnknownDocument(RegistryError):
    """No document with this id is registered."""


class InvalidAnnotations(RegistryError):
    """A PUT carried a list that is not a valid set of annotations."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` so that no reader ever sees it truncated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _root() -> Path:
    return config.data_dir()


def registry_path() -> Path:
    return _root() / "registry.json"


def annotations_path(doc_id: str) -> Path:
    if not doc_id.isalnum():
        raise UnknownDocument(f"invalid document id {doc_id!r}")
    return _root() / "annotations" / f"{doc_id}.json"


@contextmanager
def locked() -> Iterator[None]:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "registry.lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# -- registry ----------------------------------------------------------------

def _read_docs() -> dict[str, dict[str, Any]]:
    path = registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc
    docs = data.get("docs") if isinstance(data, dict) else None
    if not isinstance(docs, dict):
        raise RegistryError(f"{path} has no 'docs' table")
    return docs


def _write_docs(docs: dict[str, dict[str, Any]]) -> None:
    try:
        atomic_write(registry_path(),
                     json.dumps({"version": 1, "docs": docs}, indent=2,
                                ensure_ascii=False) + "\n")
    except OSError as exc:
        raise RegistryError(f"cannot write {registry_path()}: {exc}") from exc


def doc_id_for(path: Path, taken: dict[str, dict[str, Any]] | None = None) -> str:
    """Short and stable: derived from the absolute path, so re-registering
    the same file finds the same id. Lengthened on the (unlikely) collision."""
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    for size in range(8, len(digest) + 1):
        candidate = digest[:size]
        if not taken or candidate not in taken or taken[candidate]["path"] == str(path):
            return candidate
    raise RegistryError(f"no free id for {path}")  # pragma: no cover


def _default_cwd(path: Path) -> Path:
    for folder in [path.parent, *path.parent.parents]:
        if (folder / ".git").exists():
            return folder
    return path.parent


def all_docs() -> dict[str, dict[str, Any]]:
    with locked():
        return _read_docs()


def get(doc_id: str) -> dict[str, Any]:
    docs = all_docs()
    if doc_id not in docs:
        raise UnknownDocument(f"no document {doc_id!r} in the registry")
    return docs[doc_id]


def register(path: Path, session: str | None = None,
             cwd: Path | None = None) -> dict[str, Any]:
    """Add or refresh a document. Idempotent on the absolute path.

    `session=None` keeps the session already recorded; `session=""` clears it.
    Re-registering resets the status to `new`, unless unsent annotations
    exist: they are still waiting, and `new` would hide them.
    """
    path = path.expanduser().resolve()
    if not path.is_file():
        raise RegistryError(f"{path} is not a file")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc
    title = htmldoc.title_of(text, path.name)
    with locked():
        docs = _read_docs()
        doc_id = doc_id_for(path, docs)
        entry = docs.get(doc_id, {})
        pending = [a for a in _read_annotations(doc_id) if not a.get("sent_at")]
        entry.update({
            "id": doc_id,
            "path": str(path),
            "title": title,
            "session_id": entry.get("session_id", "") if session is None else session,
            "cwd": str((cwd.expanduser().resolve() if cwd else None)
                       or entry.get("cwd") or _default_cwd(path)),
            "registered_at": now_iso(),
            "sent_at": entry.get("sent_at", ""),
        })
        if entry.get("status") != "sent":
            entry["status"] = "annotated" if pending else "new"
        docs[doc_id] = entry
        _write_docs(docs)
        return dict(entry)


def update(doc_id: str, **fields: Any) -> dict[str, Any]:
    with locked():
        docs = _read_docs()
        if doc_id not in docs:
            raise UnknownDocument(f"no document {doc_id!r} in the registry")
        docs[doc_id].update(fields)
        _write_docs(docs)
        return dict(docs[doc_id])


def forget(doc_id: str, delete_file: bool = False) -> dict[str, Any]:
    """Remove the entry and its annotations; with `delete_file`, the file too."""
    with locked():
        docs = _read_docs()
        if doc_id not in docs:
            raise UnknownDocument(f"no document {doc_id!r} in the registry")
        entry = docs.pop(doc_id)
        _write_docs(docs)
        annotations_path(doc_id).unlink(missing_ok=True)
    if delete_file:
        try:
            Path(entry["path"]).unlink(missing_ok=True)
        except OSError as exc:
            raise RegistryError(f"cannot delete {entry['path']}: {exc}") from exc
    return entry


# -- annotations -------------------------------------------------------------

def _read_annotations(doc_id: str) -> list[dict[str, Any]]:
    path = annotations_path(doc_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc
    items = data.get("annotations") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _write_annotations(doc_id: str, items: list[dict[str, Any]]) -> None:
    try:
        atomic_write(annotations_path(doc_id),
                     json.dumps({"doc_id": doc_id, "annotations": items},
                                indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise RegistryError(f"cannot write annotations of {doc_id}: {exc}") from exc


def list_annotations(doc_id: str) -> list[dict[str, Any]]:
    with locked():
        return _read_annotations(doc_id)


def pending(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The annotations that were never sent to a session."""
    return [a for a in items if not a.get("sent_at")]


def _clean(raw: object, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InvalidAnnotations(f"annotation #{index} is not an object")
    out: dict[str, Any] = {}
    for key in ("id", "selector", "quote", "note"):
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise InvalidAnnotations(f"annotation #{index}: {key} must be a string")
        if len(value) > MAX_TEXT:
            raise InvalidAnnotations(f"annotation #{index}: {key} is too long")
        out[key] = value
    if not out["id"] or not out["id"].replace("-", "").isalnum():
        raise InvalidAnnotations(f"annotation #{index}: invalid id {out['id']!r}")
    for key in ("offset_x", "offset_y"):
        value = raw.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidAnnotations(f"annotation #{index}: {key} must be a number")
        out[key] = float(value)
    number = raw.get("number", index + 1)
    out["number"] = number if isinstance(number, int) and not isinstance(number, bool) \
        else index + 1
    created = raw.get("created_at", "")
    out["created_at"] = created if isinstance(created, str) and created else now_iso()
    return out


def replace_annotations(doc_id: str, incoming: object) -> list[dict[str, Any]]:
    """Replace the list (browser PUT), keeping the server's `sent_at`."""
    if not isinstance(incoming, list):
        raise InvalidAnnotations("the body must be a JSON list of annotations")
    cleaned = [_clean(raw, i) for i, raw in enumerate(incoming)]
    ids = [a["id"] for a in cleaned]
    if len(set(ids)) != len(ids):
        raise InvalidAnnotations("two annotations share the same id")
    with locked():
        docs = _read_docs()
        if doc_id not in docs:
            raise UnknownDocument(f"no document {doc_id!r} in the registry")
        known = {a.get("id"): a for a in _read_annotations(doc_id)}
        for item in cleaned:
            item["sent_at"] = (known.get(item["id"]) or {}).get("sent_at", "") or ""
        _write_annotations(doc_id, cleaned)
        entry = docs[doc_id]
        if pending(cleaned) and entry.get("status") != "sent":
            entry["status"] = "annotated"
        _write_docs(docs)
        return cleaned


def mark_sent(doc_id: str, ids: list[str], stamp: str) -> None:
    with locked():
        items = _read_annotations(doc_id)
        for item in items:
            if item.get("id") in ids and not item.get("sent_at"):
                item["sent_at"] = stamp
        _write_annotations(doc_id, items)


def unmark_sent(doc_id: str, stamp: str) -> int:
    """Undo a failed send: the batch stamped `stamp` becomes sendable again."""
    with locked():
        items = _read_annotations(doc_id)
        count = 0
        for item in items:
            if item.get("sent_at") == stamp:
                item["sent_at"] = ""
                count += 1
        _write_annotations(doc_id, items)
        return count


def summary() -> list[dict[str, Any]]:
    """The registry as `/api/docs` serves it, with annotation counts."""
    with locked():
        docs = _read_docs()
        out = []
        for doc_id, entry in sorted(docs.items(),
                                    key=lambda kv: kv[1].get("registered_at", ""),
                                    reverse=True):
            items = _read_annotations(doc_id)
            out.append({**entry,
                        "annotations": len(items),
                        "pending": len(pending(items)),
                        "exists": Path(entry["path"]).is_file()})
        return out
