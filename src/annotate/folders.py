"""The folders the daemon tracks: where an .html file becomes a document.

Before 2026-10-07 there was one rule, written twice (here and in the hook):
an .html file with a `docs/` folder between the workspace and itself. It
missed what sessions produce elsewhere, measured that day: a deliverable
under `~/.claude/sync/docs/` (outside the workspace) and a report under a
`rapports/` folder of a repository (no `docs/` on the way). The user now picks
the folders from the index page (decision of 2026-10-07: "on peut choisir
n'importe quel dossier de ~/ [...] tout ce qui est en dessous du dossier
choisi est traqué").

    folders.json   {"version": 1, "folders": [{"path": "/abs", "docs_only": false}]}

| Folder                          | What is tracked under it                    |
|---------------------------------|---------------------------------------------|
| the default one, the workspace  | every .html with a `docs/` folder on the way (`docs_only`) |
| a folder the user added         | every .html, at any depth                   |

In both cases nothing under a `SKIPPED` folder is a document. The default
entry exists only while the file does not: once the user adds or removes a
folder, the list on disk is the whole truth, the workspace included.

**One rule, in one place**: the hook does not decide any more, it hands every
.html it sees to `annotate register --hook`, which asks `wanted()`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config, registry

# Folders never entered: tool caches, dependencies, build output (coverage and
# lint reports are HTML too, by the hundred), `figures/`, where the HTML
# sources of a document's images live (docs/figures/<x>/tpl.html: inputs of a
# picture, not documents to read), and `.claude` (agent worktrees: temporary
# copies, deleted with the agent).
SKIPPED = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache",
                     ".pytest_cache", ".ruff_cache", "figures", ".claude",
                     "build", "_build", "dist", "target", "htmlcov", "site-packages"})


class FoldersError(Exception):
    """The list of folders cannot be read, written, or does not accept this one."""


def path() -> Path:
    return config.data_dir() / "folders.json"


def home() -> Path:
    return Path.home().resolve()


def _default() -> list[dict[str, Any]]:
    return [{"path": str(config.workspace().expanduser().resolve()), "docs_only": True}]


def load() -> list[dict[str, Any]]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default()
    except (OSError, ValueError) as exc:
        raise FoldersError(f"cannot read {path()}: {exc}") from exc
    items = data.get("folders") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise FoldersError(f"{path()}: no 'folders' list")
    return [{"path": str(item["path"]), "docs_only": bool(item.get("docs_only"))}
            for item in items if isinstance(item, dict) and isinstance(item.get("path"), str)]


def _save(items: list[dict[str, Any]]) -> None:
    try:
        registry.atomic_write(path(), json.dumps({"version": 1, "folders": items},
                                                 ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise FoldersError(f"cannot write {path()}: {exc}") from exc


def normalise(raw: str) -> Path:
    """`raw` as an absolute, resolved folder under the home directory, or
    FoldersError. `~` is expanded; a relative path is refused rather than
    resolved against the daemon's own working directory."""
    text = (raw or "").strip()
    if not text:
        raise FoldersError("no folder given")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise FoldersError(f"{text!r} is not an absolute path (start it with / or ~)")
    folder = candidate.resolve()
    if not folder.is_relative_to(home()):
        raise FoldersError(f"{folder} is not under {home()}")
    if not folder.is_dir():
        raise FoldersError(f"{folder} is not a folder")
    return folder


def add(raw: str) -> dict[str, Any]:
    """Track every .html under `raw`. Adding a folder already tracked makes it
    track everything (a `docs_only` entry becomes a full one)."""
    folder = normalise(raw)
    with registry.locked():
        items = [i for i in load() if i["path"] != str(folder)]
        entry = {"path": str(folder), "docs_only": False}
        _save([*items, entry])
    return entry


def remove(raw: str) -> dict[str, Any]:
    """Stop tracking a folder. Its documents stay registered: unmanaging them
    is a decision per document."""
    text = (raw or "").strip()
    with registry.locked():
        items = load()
        kept = [i for i in items if i["path"] != text]
        if len(kept) == len(items):
            raise FoldersError(f"{text!r} is not a tracked folder")
        _save(kept)
    return next(i for i in items if i["path"] == text)


def _matches(path: Path, folder: Path, docs_only: bool) -> bool:
    try:
        parts = path.relative_to(folder).parts[:-1]
    except ValueError:
        return False
    if SKIPPED.intersection(parts):
        return False
    return "docs" in parts if docs_only else True


def wanted(path: Path, items: list[dict[str, Any]] | None = None) -> bool:
    """True if `path` is a document: an .html file that one tracked folder
    claims. The hook (through `annotate register --hook`) and the daemon's
    pass both ask this, and nothing else."""
    if path.suffix.lower() != ".html":
        return False
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    return any(_matches(resolved, Path(i["path"]), i["docs_only"])
               for i in (load() if items is None else items))


def roots(items: list[dict[str, Any]]) -> list[Path]:
    """The folders to walk: a folder inside another one is already visited by
    that one's walk, unless a SKIPPED folder lies between them (`~` prunes
    `.claude`, so `~/.claude/sync/docs` must be walked on its own)."""
    paths = sorted({Path(i["path"]) for i in items}, key=lambda p: len(p.parts))
    kept: list[Path] = []
    for folder in paths:
        covered = any(folder != outer and folder.is_relative_to(outer)
                      and not SKIPPED.intersection(folder.relative_to(outer).parts)
                      for outer in kept)
        if not covered:
            kept.append(folder)
    return kept


def subfolders(raw: str, limit: int = 50) -> dict[str, Any]:
    """What the index page proposes while the user types a folder: the
    subfolders of the deepest existing folder of `raw`, under the home
    directory only. Hidden folders included: `~/.claude/sync/docs` is one of
    the folders this exists for."""
    text = (raw or "~/").strip() or "~/"
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = home() / candidate
    base = candidate if text.endswith("/") else candidate.parent
    prefix = "" if text.endswith("/") else candidate.name
    try:
        base = base.resolve()
    except OSError:
        base = home()
    if not base.is_relative_to(home()) or not base.is_dir():
        base, prefix = home(), ""
    try:
        names = sorted(e.name for e in base.iterdir()
                       if e.is_dir() and e.name.startswith(prefix))
    except OSError:
        names = []
    return {"base": str(base), "folders": [str(base / n) for n in names[:limit]]}
