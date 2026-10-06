"""The daemon's own pass over the workspace: what the hook cannot see.

The hook (hooks/register-on-write.py) fires on `Write` and `Edit` only. A
document a session produces with Bash (a script, a `cp` from its scratchpad,
the usual way of subagents) never reaches it: measured 2026-10-05,
`<repo>/docs/<name>.html` was composed by a subagent
in its scratchpad and copied into `docs/` at 16:08, an hour after the hook
existed, and never appeared in the tray.

Every `SCAN_EVERY` seconds the daemon walks the workspace (0.3 s for 10 577
folders, measured) and registers the documents written SINCE ITS PREVIOUS
PASS; the session that wrote each one is looked up in the transcripts
(claude.writer_of). The hook stays: it knows the session for certain, and it
is what tells that session to run `annotate wait`.

**Nothing older than the first pass is ever taken** (decision of 2026-10-05:
"on rattrape pas l'existant"). The start of each pass is kept on disk
(`scan.json`): a file written while the daemon was down is found at the next
start, and a document the user forgot comes back only if it is written again,
exactly as the hook would bring it back.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from . import claude, config, registry

log = logging.getLogger("annotate")

SCAN_EVERY = 30.0
# Folders never entered: tool caches, dependencies, and `figures/`, where the
# HTML sources of a document's images live (docs/figures/<x>/tpl.html): they
# are inputs of a picture, not documents to read.
SKIPPED = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache",
                     ".pytest_cache", ".ruff_cache", "figures",
                     # agent worktrees: temporary copies, deleted with the agent
                     ".claude"})


class ScanError(Exception):
    """The scan state cannot be read or written."""


def wanted(path: Path, root: Path) -> bool:
    """An .html file with a `docs` folder between the workspace and itself,
    and no skipped folder on the way. The hook applies the SAME rule
    (tests/test_scanner.py compares them on a table of paths)."""
    if path.suffix.lower() != ".html":
        return False
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return False
    return "docs" in parts and not SKIPPED.intersection(parts)


def state_path() -> Path:
    return config.data_dir() / "scan.json"


def read_since() -> float | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ScanError(f"cannot read {state_path()}: {exc}") from exc
    since = data.get("since") if isinstance(data, dict) else None
    return float(since) if isinstance(since, (int, float)) else None


def write_since(since: float) -> None:
    try:
        registry.atomic_write(state_path(), json.dumps({"since": since}) + "\n")
    except OSError as exc:
        raise ScanError(f"cannot write {state_path()}: {exc}") from exc


def candidates(root: Path, since: float) -> list[tuple[Path, float]]:
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIPPED]
        for name in files:
            if not name.lower().endswith(".html"):
                continue
            path = Path(folder) / name
            if not wanted(path, root):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > since:
                found.append((path, mtime))
    return found


def scan_once(root: Path, now: float | None = None) -> list[str]:
    """One pass; return the ids it registered. The first pass registers
    nothing: it only sets the starting point."""
    start = time.time() if now is None else now
    since = read_since()
    if since is None:
        write_since(start)
        log.info("scan: first pass, documents written from now on will be listed")
        return []
    known = {entry["path"] for entry in registry.all_docs().values()}
    added = []
    for path, mtime in sorted(candidates(root, since), key=lambda c: c[1]):
        if str(path.resolve()) in known:
            continue
        writer = claude.writer_of(path, mtime)
        session, cwd = writer if writer else ("", "")
        try:
            entry = registry.register(path, session=session,
                                      cwd=Path(cwd) if cwd and Path(cwd).is_dir() else None)
        except registry.RegistryError as exc:
            log.warning("scan: %s not registered: %s", path, exc)
            continue
        added.append(entry["id"])
        log.info("scan: %s registered as %s (session %s)", path, entry["id"],
                 session or "unknown")
    write_since(start)
    return added


def run(root: Path, stop: threading.Event, every: float = SCAN_EVERY) -> None:
    """The daemon's thread: a pass, then a wait that `stop` interrupts. A
    failing pass is logged and the next one is tried: the scan is a
    complement, it must never take the daemon down."""
    while not stop.is_set():
        try:
            scan_once(root)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.error("scan failed: %s", exc)
        stop.wait(every)
