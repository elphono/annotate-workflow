# Roadmap

What is shipped, what is next, and what is deliberately not on the list. Items marked **(design
done)** have a written design in [docs/etude-reutilisation.html](docs/etude-reutilisation.html);
items marked **(proposal)** are ideas, not commitments.

## Shipped — v0.1

- Hook on `Write|Edit` that registers a session's HTML documents with the session's id, and a daemon
  scan (every 30 s) for what Bash writes.
- Annotation layer injected by the daemon: Alt+click pins a numbered note, anchored by CSS selector +
  quote + offset, so it survives corrections. An “orphans” bar keeps notes whose anchor disappeared.
- Three delivery routes: the session that waits (`annotate wait`), the open session's inbox, a
  Windows Terminal tab that resumes the session. Never a background session.
- Home page and Windows tray icon, with per-document and per-daemon controls.
- Local-only HTTP API, hardened against pages that merely target `localhost`.
- Test suite with guards that keep it from touching the real system, mutation harness, `ruff` and
  `mypy` clean, no runtime dependency.

## Next

1. **First real run from Windows Chrome / Edge.** Alt+click has only been measured in headless
   Chromium on Linux, and Alt alone can give the focus to the browser menu on Windows. The tray icon
   and its menu have not been looked at on a screen either. Fix whatever this shows.
2. **More than pins: highlight, circle, strike-through** **(design done)**. Each annotation gets a
   `kind`; text kinds anchor on an exact quote with a 32-character prefix and suffix (the Web
   Annotation `TextQuoteSelector` idea), which survives block reordering where a selector does not.
   The tool is chosen in the bar, as in a screenshot tool — no gesture recognition. The prompt changes
   by one word per note.
3. **Watch the inbox route.** Posting into an open session's inbox relies on a line format that Claude
   Code does not document (captured on a real message, frozen by `tests/test_inbox.py`). Re-check it
   on every Claude Code release, and switch to a documented interface if one appears.
4. **Licence and packaging.** Pick a licence; publish so that `uvx` / `pipx` can install it without a
   clone.

## Later

- **Native Linux and macOS** **(proposal)**. The daemon, the hook and routes A and B are plain Python
  and HTTP. What ties the project to Windows is the terminal-tab fallback (`wt.exe`) and the tray icon;
  a fallback per terminal emulator and a menu-bar icon would remove that.
- **A browser extension** **(design done, deferred)**, only for pages the daemon does not serve (an
  external site, a pull request). It would reuse the overlay script and the daemon API as they are.
  Not worth it until that need is real.
- **Other document kinds** **(proposal)**: Markdown rendered by the daemon, so that a session that
  writes `.md` can be annotated the same way.

## Not planned, on purpose

- **A background session answering the notes.** The first real send did exactly that: 2 min 45 s,
  $1.71, a correct fix — and nothing visible to the person waiting at the open session. Notes go to the
  conversation you can see, or to a visible tab.
- **Gesture recognition** (a drawn symbol that means “done”). A button cannot be misread.
- **Notes stored next to the document.** The document lives in a repository; the notes do not.
