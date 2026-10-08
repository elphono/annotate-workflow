"""The registry of documents, and their annotations, on disk.

Layout under `config.data_dir()` (default `~/.local/share/annotate`):

    registry.json            {"version": 1, "docs": {id: entry}}
    registry.lock            flock target, never read
    annotations/<id>.json    {"doc_id": id, "annotations": [...]}
    forgotten.json           {"version": 1, "forgotten": {path: mtime}}

**A document the user unmanaged stays out until it is written again**:
`forget` records the file's date, and the catch-up pass of a rescan
(scanner.catch_up) skips a file whose date has not moved since. Without it,
"Rescan" would bring back every document the user had just dismissed.

**Annotations never live next to the document**: the document sits in a git
repository, and a sidecar file would show up in `git status` and end up
committed.

**Two writers share these files**: the daemon (browser PUTs, sends) and the
CLI (`annotate register`, called by the Claude Code hook from any session). Every read-modify-write therefore runs under an
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

from . import claude, config, htmldoc

STATUSES = ("new", "annotated", "delivered", "answered")
MAX_TEXT = 20_000


class RegistryError(Exception):
    """The registry or an annotation file could not be read or written."""


class UnknownDocument(RegistryError):
    """No document with this id is registered."""


class InvalidAnnotations(RegistryError):
    """A PUT carried a list that is not a valid set of annotations."""


class InvalidSession(RegistryError):
    """An attach named something that is not a conversation of this machine."""


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


def forgotten_path() -> Path:
    return _root() / "forgotten.json"


def _read_forgotten() -> dict[str, float]:
    path = forgotten_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc
    table = data.get("forgotten") if isinstance(data, dict) else None
    return {str(k): float(v) for k, v in (table or {}).items()
            if isinstance(v, (int, float))}


def _write_forgotten(table: dict[str, float]) -> None:
    try:
        atomic_write(forgotten_path(), json.dumps({"version": 1, "forgotten": table},
                                                  indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise RegistryError(f"cannot write {forgotten_path()}: {exc}") from exc


def dismissed(path: Path, mtime: float) -> bool:
    """True if the user unmanaged `path` and it has not been written since."""
    with locked():
        recorded = _read_forgotten().get(str(path))
    return recorded is not None and mtime <= recorded


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


WORKTREES = "/.claude/worktrees/"


def usable_cwd(cwd: str | Path | None, path: Path) -> Path:
    """The folder a session for `path` should work in, and one that EXISTS.

    A subagent started with `isolation: worktree` runs in
    `<repo>/.claude/worktrees/agent-<id>/`, deleted when it ends: measured
    2026-10-06, a document copied by such an agent kept that folder as its
    session folder, and "Send to session" failed on it the next day. So: the
    given folder if it exists and is not an agent worktree; else the
    repository that holds the worktree; else the repository of the document;
    else its folder."""
    candidates: list[Path] = []
    if cwd:
        text = str(cwd)
        if WORKTREES in text + "/":
            candidates.append(Path(text.split(WORKTREES.rstrip("/"))[0]))
        else:
            candidates.append(Path(text))
    candidates += [_default_cwd(path), path.parent]
    for folder in candidates:
        if folder.is_dir():
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

    The status follows what re-registering MEANS. The hook re-registers a
    document every time a session writes it, so a write after a delivery is
    the session's answer: `delivered` becomes `answered`, and stays so on
    later writes. Unsent annotations win over both (`annotated`): they are
    still waiting, and any other status would hide them.
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
            "cwd": str(usable_cwd(cwd.expanduser().resolve() if cwd else
                                  entry.get("cwd"), path)),
            "registered_at": now_iso(),
            "sent_at": entry.get("sent_at", ""),
        })
        if pending:
            entry["status"] = "annotated"
        elif entry.get("status") in ("delivered", "answered"):
            entry["status"] = "answered"
        else:
            entry["status"] = "new"
        docs[doc_id] = entry
        _write_docs(docs)
        table = _read_forgotten()
        if table.pop(str(path), None) is not None:
            _write_forgotten(table)
        return dict(entry)


def attach(doc_id: str, session: object, cwd: object = None) -> dict[str, Any]:
    """Tie a document to the conversation `session`, or untie it (`""`).

    The user's correction (index page, `annotate attach`) of what the hook or
    the scan recorded, or could not find: "Send to session" then reaches that
    conversation. Only one `claude --resume` would find is accepted
    (claude.resumable): anything else would make the next send open a NEW
    session under a heading that names another.

    Status and annotations stay as they are: `register` would turn
    `delivered` into `answered`, and an attach is not the session's answer.
    The folder becomes `cwd` if given, else the one the session started in,
    through `usable_cwd` (never a deleted agent worktree). Untying keeps it.
    """
    if not isinstance(session, str):
        raise InvalidSession(f"session must be a string ('' detaches), got {session!r}")
    if cwd is not None and not (isinstance(cwd, str) and (not cwd or os.path.isabs(cwd))):
        raise InvalidSession(f"cwd must be an absolute folder, got {cwd!r}")
    hint = ""
    if session:
        if not claude.SESSION_ID.match(session):
            raise InvalidSession(f"{session!r} is not a Claude Code session id")
        if not claude.resumable(session):
            raise InvalidSession(f"no conversation {session} on this machine: "
                                 f"`claude --resume` would not find it")
        hint = str(cwd or "") or claude.session_cwd(session)
    with locked():
        docs = _read_docs()
        if doc_id not in docs:
            raise UnknownDocument(f"no document {doc_id!r} in the registry")
        entry = docs[doc_id]
        entry["session_id"] = session
        if session:
            entry["cwd"] = str(usable_cwd(hint or entry.get("cwd"), Path(entry["path"])))
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
    """Remove the entry and its annotations; with `delete_file`, the file too.
    A file that stays is remembered with its date (see `dismissed`)."""
    with locked():
        docs = _read_docs()
        if doc_id not in docs:
            raise UnknownDocument(f"no document {doc_id!r} in the registry")
        entry = docs.pop(doc_id)
        _write_docs(docs)
        annotations_path(doc_id).unlink(missing_ok=True)
        if not delete_file:
            try:
                mtime = Path(entry["path"]).stat().st_mtime
            except OSError:
                mtime = None
            if mtime is not None:
                table = _read_forgotten()
                table[entry["path"]] = mtime
                _write_forgotten(table)
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
    for key in ("id", "selector", "quote", "note", "at"):
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
        if pending(cleaned):
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


def group_by_session(docs: list[dict[str, Any]], title_of: Any,
                     root: Path) -> list[dict[str, Any]]:
    """The documents of `summary()` grouped by the session that produced them,
    most recent group first (decision of 2026-10-05: "classer les documents par
    session"). Documents without a known session form one last group.

    `label` is what the user reads: the session's title (`title_of(id)`, its
    /rename title or the one Claude Code generated), then the folder it works
    in, relative to the workspace."""
    groups: dict[str, dict[str, Any]] = {}
    for doc in docs:                       # already newest first
        key = doc.get("session_id") or ""
        group = groups.get(key)
        if group is None:
            cwd = Path(doc.get("cwd") or "")
            try:
                folder = str(cwd.relative_to(root)) if cwd.is_absolute() else ""
            except ValueError:
                folder = cwd.name
            name = (title_of(key) if key else "") or (
                f"session {key[:8]}" if key else "documents without a known session")
            group = groups[key] = {
                "session_id": key, "title": name, "folder": folder if key else "",
                "label": f"{name} · {folder}" if key and folder else name,
                "docs": []}
        group["docs"].append(doc["id"])
    known = [g for k, g in groups.items() if k]
    return known + ([groups[""]] if "" in groups else [])


def summary() -> list[dict[str, Any]]:
    """The registry as `/api/docs` serves it, with annotation counts."""
    with locked():
        docs = _read_docs()
        out = []
        for doc_id, entry in sorted(docs.items(),
                                    key=lambda kv: kv[1].get("registered_at", ""),
                                    reverse=True):
            items = _read_annotations(doc_id)
            try:
                mtime = Path(entry["path"]).stat().st_mtime
            except OSError:
                mtime = None
            out.append({**entry,
                        "annotations": len(items),
                        "pending": len(pending(items)),
                        "exists": mtime is not None,
                        "mtime": mtime})
        return out
