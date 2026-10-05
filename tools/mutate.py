"""Directed mutation campaign on the central properties of annotate-workflow.

    uv run python tools/mutate.py            # every mutation, full default suite
    uv run python tools/mutate.py orphan-js  # one of them

Each mutation breaks ONE property on purpose; the suite must go red. The
campaign never touches the working tree: it copies the repository into a
temporary folder, mutates the copy, runs the suite there with this
interpreter (the copy's `src` comes first on sys.path through the pytest
`pythonpath` setting), and bounds every run. A run that exceeds its bound
counts as a detection: a suite that does not return does not pass.

Before mutating, the UNMUTATED copy must be green, or every "kill" would be
a crash rather than a detection.

Mutations marked `browser` run the browser suite (`-m browser`), which needs
ANNOTATE_PLAYWRIGHT_DIR (see tests/test_browser.py).

Adapted from remarkable-sync's tools/mutate.py, reduced to directed
mutations: here the list is short and each line names what it breaks.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 600


class MutationError(Exception):
    """A mutation could not be applied (its anchor text is gone)."""


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    breaks: str
    browser: bool = False


MUTATIONS = (
    Mutation("path-escape", "src/annotate/server.py",
             "    if not target.is_relative_to(root):\n",
             "    if False:\n",
             "files outside the document folder are refused"),
    Mutation("send-all", "src/annotate/registry.py",
             '    return [a for a in items if not a.get("sent_at")]\n',
             "    return list(items)\n",
             "send only sends the unsent annotations"),
    Mutation("orphan-prompt", "src/annotate/prompt.py",
             "        node = htmldoc.resolve(doc, item.get(\"selector\", \"\"))\n",
             "        node = htmldoc.resolve(doc, item.get(\"selector\", \"\"))\n"
             "        if node is None:\n            continue\n",
             "an annotation whose selector no longer resolves is still sent"),
    Mutation("orphan-js", "src/annotate/static/annotate.js",
             "      if (!target) { orphans.push(item); continue; }\n",
             "      if (!target) { annotations = annotations.filter((a) => a !== item);"
             " continue; }\n",
             "an orphan stays in the model, so the next save keeps it",
             browser=True),
    Mutation("guard-claude", "tests/conftest.py",
             "            if name in FORBIDDEN_PROGRAMS:\n",
             "            if False:\n",
             "no test can launch a real claude"),
    Mutation("guard-kill", "src/annotate/claude.py",
             "    if proc.pid in (0, 1):\n",
             "    if False:\n",
             "kill_group never signals pid 0 or 1"),
)


def copy_repo(target: Path) -> None:
    ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache",
                                    ".mypy_cache", ".ruff_cache")
    shutil.copytree(ROOT, target, ignore=ignore)


def run_suite(copy: Path, browser: bool) -> tuple[str, str]:
    """Run the suite in `copy`; return (verdict, last line)."""
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if browser:
        argv += ["-m", "browser"]
    try:
        done = subprocess.run(argv, cwd=copy, capture_output=True, text=True,
                              timeout=TIMEOUT, env={**os.environ})
    except subprocess.TimeoutExpired:
        return "timeout", f"no answer within {TIMEOUT}s"
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    last = lines[-1] if lines else done.stderr.strip()[-200:]
    failed = [ln.split(" - ")[0].replace("FAILED ", "") for ln in lines
              if ln.startswith("FAILED ")]
    if failed:
        last = last + "\n      red: " + "\n      red: ".join(failed)
    return ("green" if done.returncode == 0 else "red"), last


def apply(copy: Path, mutation: Mutation) -> None:
    path = copy / mutation.path
    text = path.read_text(encoding="utf-8")
    if text.count(mutation.old) != 1:
        raise MutationError(f"{mutation.name}: anchor found {text.count(mutation.old)} "
                            f"times in {mutation.path}, expected exactly once")
    path.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="mutations to run (default: all)")
    args = parser.parse_args(argv)
    chosen = [m for m in MUTATIONS if not args.names or m.name in args.names]
    if not chosen:
        print(f"no mutation named {args.names}; known: {[m.name for m in MUTATIONS]}")
        return 2
    survivors = 0
    for browser in sorted({m.browser for m in chosen}):
        with tempfile.TemporaryDirectory(prefix="annotate-mutate-") as tmp:
            base = Path(tmp) / "baseline"
            copy_repo(base)
            verdict, last = run_suite(base, browser)
            label = "browser" if browser else "default"
            print(f"baseline ({label} suite, unmutated copy): {verdict} | {last}")
            if verdict != "green":
                print("the unmutated copy is not green: no mutation result would mean anything")
                return 2
        for mutation in (m for m in chosen if m.browser == browser):
            with tempfile.TemporaryDirectory(prefix="annotate-mutate-") as tmp:
                copy = Path(tmp) / "copy"
                copy_repo(copy)
                apply(copy, mutation)
                verdict, last = run_suite(copy, browser)
            killed = verdict in ("red", "timeout")
            survivors += 0 if killed else 1
            print(f"{'KILLED ' if killed else 'SURVIVED'} {mutation.name:<14} "
                  f"({mutation.breaks}) | {last}")
    print(f"{len(chosen)} mutation(s), {survivors} survivor(s)")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
