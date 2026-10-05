"""`annotate`: register documents, run the daemon, talk to it.

    annotate register <path> [--session ID] [--cwd DIR] [--hook]
    annotate wait <id>
    annotate list
    annotate open <id>
    annotate send <id>
    annotate new-session <id>
    annotate forget <id> [--delete]
    annotate serve
    annotate service
    annotate tray [--uninstall] [--start] [--once]
    annotate status
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from . import browser, client, config, prompt, registry, server, service, tray

log = logging.getLogger("annotate")


def _url(cfg: config.Config, doc_id: str) -> str:
    # 127.0.0.1, not localhost: see server.AnnotateServer.base_url.
    return f"http://127.0.0.1:{cfg.port}/docs/{doc_id}"


def cmd_register(args: argparse.Namespace, cfg: config.Config) -> int:
    entry = registry.register(Path(args.path), session=args.session,
                              cwd=Path(args.cwd) if args.cwd else None)
    if args.hook:
        print(json.dumps(hook_output(cfg, entry)))
        return 0
    print(f"{entry['id']}  {entry['status']}  {entry['title']}")
    print(_url(cfg, entry["id"]))
    return 0


def _listening(cfg: config.Config, doc_id: str) -> bool:
    try:
        data = client.call(cfg.port, "GET", f"/api/docs/{doc_id}", timeout=3)
    except client.ClientError:
        return False
    return bool(data.get("listening"))


def hook_output(cfg: config.Config, entry: dict[str, object]) -> dict[str, object]:
    """What the PostToolUse hook prints: tell the writing session how to get
    the user's notes back, unless a session already listens for them.

    Telling it on every write would repeat the same paragraph each time the
    session edits its document, answering notes included."""
    doc_id = str(entry["id"])
    if _listening(cfg, doc_id):
        return {}
    context = (
        f"annotate: {entry['path']} is registered as document {doc_id}; the user "
        f"reads and annotates it at {_url(cfg, doc_id)} . So that the user's notes "
        f"come back to THIS conversation, start this command once, as a background "
        f"command (run_in_background), and do not wait for it: "
        f"`{prompt.wait_command(doc_id)}`. It ends when the user sends notes, and "
        f"its output is the notes. Skip it if it is already running for {doc_id}.")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": context}}


def cmd_wait(args: argparse.Namespace, cfg: config.Config) -> int:
    """Wait for the user's notes on a document; print them and exit.

    Meant to run as a BACKGROUND command of a Claude Code session: when it
    exits, Claude Code hands its output to that session. It never exits on
    its own otherwise: a daemon restart or an unreachable daemon only makes
    it ask again a few seconds later, since waking the session up to say
    "nothing happened" would be noise in the conversation.
    """
    registry.get(args.id)  # an unknown id is an error now, not a silent wait
    body = {"session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
            "cwd": os.getcwd()}
    while True:
        try:
            result = client.call(cfg.port, "POST", f"/api/docs/{args.id}/wait", body,
                                 timeout=server.WAIT_HOLD + 20)
        except client.DaemonUnreachable:
            time.sleep(args.retry)
            continue
        text = result.get("prompt")
        if isinstance(text, str) and text:
            print(text)
            return 0


def cmd_list(args: argparse.Namespace, cfg: config.Config) -> int:
    docs = registry.summary()
    if not docs:
        print("no document registered")
        return 0
    for doc in docs:
        missing = "" if doc["exists"] else "  (file missing)"
        print(f"{doc['id']}  {doc['status']:<9}  {doc['pending']}/{doc['annotations']} "
              f"to send  {doc['path']}{missing}")
    return 0


def cmd_open(args: argparse.Namespace, cfg: config.Config) -> int:
    registry.get(args.id)
    url = _url(cfg, args.id)
    opener = browser.open_url(url)
    print(f"opened {url} with {opener}")
    return 0


def _post(cfg: config.Config, doc_id: str, action: str) -> int:
    result = client.call(cfg.port, "POST", f"/api/docs/{doc_id}/{action}", timeout=60)
    print(describe(result))
    return 0


def describe(result: dict[str, object]) -> str:
    """Where the notes went, in one line (also what the browser says)."""
    count = result.get("count")
    if result.get("target") == "session":
        return f"{count} note(s) delivered to the open session {result.get('session') or ''}".rstrip()
    if result.get("fresh"):
        return f"{count} note(s) opened in a terminal tab, in a NEW session"
    return (f"{count} note(s) opened in a terminal tab: no open session listened, "
            f"session {result.get('session')} resumed there")


def cmd_send(args: argparse.Namespace, cfg: config.Config) -> int:
    return _post(cfg, args.id, "send")


def cmd_new_session(args: argparse.Namespace, cfg: config.Config) -> int:
    return _post(cfg, args.id, "new-session")


def cmd_forget(args: argparse.Namespace, cfg: config.Config) -> int:
    try:
        client.call(cfg.port, "DELETE",
                    f"/api/docs/{args.id}" + ("?delete=1" if args.delete else ""))
    except client.DaemonUnreachable:
        # Without a daemon nobody can be running a session: forget directly.
        registry.forget(args.id, delete_file=args.delete)
    print(f"forgot {args.id}" + (" and deleted its file" if args.delete else ""))
    return 0


def cmd_serve(args: argparse.Namespace, cfg: config.Config) -> int:
    return server.serve(cfg)


def cmd_service(args: argparse.Namespace, cfg: config.Config) -> int:
    target = service.write_unit()
    print(f"unit written: {target}\n")
    print("to enable it:")
    print("  loginctl enable-linger $USER     # otherwise it dies with the last session")
    print("  systemctl --user daemon-reload")
    print(f"  systemctl --user enable --now {service.UNIT_NAME}")
    print("to follow it:")
    print(f"  journalctl --user -u {service.UNIT_NAME} -f")
    return 0


def cmd_tray(args: argparse.Namespace, cfg: config.Config) -> int:
    if args.once:
        code, text = tray.once(cfg.port)
        print(text)
        return code
    print(tray.run_powershell(tray.shortcut_script(cfg.port, uninstall=args.uninstall)))
    if args.uninstall:
        return 0
    if args.start:
        print(f"icon started (launcher pid {tray.launch(cfg.port)})")
    else:
        print("the icon starts at the next Windows logon; to start it now:")
        print("  annotate tray --start")
    print("to check it without a screen:")
    print("  annotate tray --once")
    return 0


def cmd_status(args: argparse.Namespace, cfg: config.Config) -> int:
    try:
        data = client.call(cfg.port, "GET", "/api/docs", timeout=3)
    except client.DaemonUnreachable:
        print(f"daemon unreachable on 127.0.0.1:{cfg.port} "
              f"(systemctl --user status {service.UNIT_NAME})")
        return 1
    docs = data.get("docs", [])
    counts = Counter(d.get("status") for d in docs)
    listening = sum(1 for d in docs if d.get("listening"))
    parts = [f"{counts[s]} {s}" for s in registry.STATUSES if counts[s]]
    print(f"daemon up on :{cfg.port}, {len(docs)} document(s)"
          + (": " + ", ".join(parts) if parts else "")
          + (f"; {listening} with a session listening" if listening else ""))
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="annotate", description=__doc__.splitlines()[0])
    top.add_argument("-v", "--verbose", action="store_true")
    sub = top.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="register an HTML document")
    p.add_argument("path")
    p.add_argument("--session", default=None,
                   help="id of the producing Claude Code session ('' clears it)")
    p.add_argument("--cwd", default=None, help="working directory of that session")
    p.add_argument("--hook", action="store_true",
                   help="print the JSON the Claude Code hook forwards (internal)")
    p.set_defaults(run=cmd_register)

    p = sub.add_parser("wait", help="wait for the user's notes on a document, print "
                                    "them (run it in the background of a session)")
    p.add_argument("id")
    p.add_argument("--retry", type=float, default=5.0, help=argparse.SUPPRESS)
    p.set_defaults(run=cmd_wait)

    sub.add_parser("list", help="one line per document").set_defaults(run=cmd_list)

    for name, run, text in (("open", cmd_open, "open the document in the browser"),
                            ("send", cmd_send, "hand the unsent notes to a session"),
                            ("new-session", cmd_new_session,
                             "open the unsent notes in a NEW session (terminal tab)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("id")
        p.set_defaults(run=run)

    p = sub.add_parser("forget", help="stop tracking a document")
    p.add_argument("id")
    p.add_argument("--delete", action="store_true", help="also delete the file")
    p.set_defaults(run=cmd_forget)

    sub.add_parser("serve", help="run the daemon").set_defaults(run=cmd_serve)
    sub.add_parser("service", help="write the systemd user unit").set_defaults(
        run=cmd_service)

    p = sub.add_parser("tray", help="install the Windows tray icon at logon")
    p.add_argument("--uninstall", action="store_true", help="remove the shortcut")
    p.add_argument("--start", action="store_true", help="also start the icon now")
    p.add_argument("--once", action="store_true",
                   help="print what the icon would show, then exit")
    p.set_defaults(run=cmd_tray)

    sub.add_parser("status", help="what the daemon does, in one line").set_defaults(
        run=cmd_status)
    return top


ERRORS = (config.ConfigError, registry.RegistryError, client.ClientError,
          browser.OpenError, server.ServerError, service.ServiceError,
          tray.TrayError, subprocess.SubprocessError)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        cfg = config.load_config()
        return int(args.run(args, cfg))
    except ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
