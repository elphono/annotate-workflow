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
    Mutation("listener-dead", "src/annotate/listeners.py",
             "                if waiter.alive():\n",
             "                if True:\n",
             "a waiter whose client went away is never handed notes"),
    Mutation("listener-gives-up", "src/annotate/listeners.py",
             "            if time.monotonic() >= deadline or not alive():\n",
             "            if time.monotonic() >= deadline:\n",
             "a wait stops as soon as its client goes away"),
    Mutation("send-lock", "src/annotate/sender.py",
             "        with self._lock:\n            return self._dispatch(doc_id, fresh)\n",
             "        return self._dispatch(doc_id, fresh)\n",
             "a double click hands the notes over once"),
    Mutation("listener-first", "src/annotate/sender.py",
             "        if not fresh:\n",
             "        if False:\n",
             "an open session that listens gets the notes, no tab opens"),
    Mutation("fresh-to-listener", "src/annotate/sender.py",
             "        if not fresh:\n",
             "        if True:\n",
             "'New session' never reaches the listening session"),
    Mutation("resume-unchecked", "src/annotate/sender.py",
             "        resume = session if session and claude.resumable(session) else None\n",
             "        resume = session or None\n",
             "a session gone from this machine is not resumed"),
    Mutation("prompt-split", "src/annotate/terminal.py",
             "            f\"exec {shlex.join(command)} \\\"$prompt\\\"\\n\")\n",
             "            f\"exec {shlex.join(command)} $prompt\\n\")\n",
             "the prompt reaches claude as ONE argument"),
    Mutation("hook-repeats", "src/annotate/cli.py",
             "    if _listening(cfg, doc_id):\n        return {}\n",
             "",
             "the hook stays silent when a session already listens"),
    Mutation("answered", "src/annotate/registry.py",
             "        elif entry.get(\"status\") in (\"delivered\", \"answered\"):\n",
             "        elif False:\n",
             "a write after a delivery marks the document answered"),
    Mutation("wait-gives-up", "src/annotate/cli.py",
             "            time.sleep(args.retry)\n            continue\n",
             "            raise\n",
             "`annotate wait` survives a daemon that is away"),
    Mutation("scan-backfill", "src/annotate/scanner.py",
             "    if since is None:\n        write_since(start)\n",
             "    if since is None:\n        since = 0.0\n    if False:\n        write_since(start)\n",
             "nothing older than the first pass is taken"),
    Mutation("scan-forgets-progress", "src/annotate/scanner.py",
             "    write_since(start)\n    return added\n",
             "    return added\n",
             "each pass starts where the previous one did"),
    Mutation("writer-earliest", "src/annotate/claude.py",
             "(best is None or when > best[0])",
             "(best is None or when < best[0])",
             "the writer is the LATEST call naming the file"),
    Mutation("writer-reader", "src/annotate/claude.py",
             "not mtime - WRITE_WINDOW <= when <= mtime + WRITE_SLACK",
             "not mtime - WRITE_WINDOW <= when",
             "a call made after the write only read the file"),
    Mutation("scan-figures", "src/annotate/scanner.py",
             "                     \".pytest_cache\", \".ruff_cache\", \"figures\"})\n",
             "                     \".pytest_cache\", \".ruff_cache\"})\n",
             "figures/ sources are not documents"),
    Mutation("title-ai-first", "src/annotate/claude.py",
             "        self._seen[path] = (offset + end, custom, ai)\n        return custom or ai\n",
             "        self._seen[path] = (offset + end, custom, ai)\n        return ai or custom\n",
             "a /rename title wins over the generated one"),
    Mutation("unknown-first", "src/annotate/registry.py",
             "    return known + ([groups[\"\"]] if \"\" in groups else [])\n",
             "    return ([groups[\"\"]] if \"\" in groups else []) + known\n",
             "documents without a known session come last"),
    Mutation("at-dropped", "src/annotate/static/annotate.js",
             "      at: selection ? '' : textAt(e),\n",
             "      at: '',\n",
             "the word under the cursor is captured",
             browser=True),
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
