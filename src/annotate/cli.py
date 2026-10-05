"""`annotate`: register documents, run the daemon, talk to it.

    annotate register <path> [--session ID] [--cwd DIR]
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
import logging
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import browser, client, config, registry, server, service, tray

log = logging.getLogger("annotate")


def _url(cfg: config.Config, doc_id: str) -> str:
    return f"http://localhost:{cfg.port}/docs/{doc_id}"


def cmd_register(args: argparse.Namespace, cfg: config.Config) -> int:
    entry = registry.register(Path(args.path), session=args.session,
                              cwd=Path(args.cwd) if args.cwd else None)
    print(f"{entry['id']}  {entry['status']}  {entry['title']}")
    print(_url(cfg, entry["id"]))
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
    result = client.call(cfg.port, "POST", f"/api/docs/{doc_id}/{action}")
    target = "a new session" if result.get("fresh") else "the producing session"
    print(f"{result.get('count')} note(s) of {doc_id} sent to {target}; the "
          f"daemon runs it in the background (annotate list shows the status)")
    return 0


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
    running = sum(1 for d in docs if d.get("running"))
    parts = [f"{counts[s]} {s}" for s in registry.STATUSES if counts[s]]
    print(f"daemon up on :{cfg.port}, {len(docs)} document(s)"
          + (": " + ", ".join(parts) if parts else "")
          + (f"; {running} session(s) running" if running else ""))
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
    p.set_defaults(run=cmd_register)

    sub.add_parser("list", help="one line per document").set_defaults(run=cmd_list)

    for name, run, text in (("open", cmd_open, "open the document in the browser"),
                            ("send", cmd_send, "send the unsent notes to the session"),
                            ("new-session", cmd_new_session,
                             "send the unsent notes to a NEW session")):
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
