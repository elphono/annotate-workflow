"""The daemon: an HTTP server on 127.0.0.1 that serves documents and the API.

    GET    /                                 index of registered documents
    GET    /docs/<id>                        the HTML, overlay injected, read from disk
    GET    /docs/<id>/files/<relative>       a file next to the document (images…)
    GET    /static/<name>                    annotate.js, annotate.css
    GET    /api/docs                         the registry, with annotation counts
    GET    /api/docs/<id>                    one entry
    GET    /api/docs/<id>/annotations        the annotations
    PUT    /api/docs/<id>/annotations        replace them (status -> annotated)
    POST   /api/docs/<id>/send               send unsent notes to the session
    POST   /api/docs/<id>/new-session        same, in a NEW session
    POST   /api/docs/<id>/open               open the document in the browser
    DELETE /api/docs/<id>[?delete=1]         forget it (and delete the file)

**Local only, and no open CORS.** The socket is bound to 127.0.0.1. Any web
page in the user's browser can still fire a "simple" cross-site POST at
localhost, and `send` starts a session allowed to edit files: so every
mutating request must carry the `X-Annotate` header (a custom header forces a
CORS preflight, which this server never grants), and an `Origin`, when
present, must be this server's. The `Host` header must name localhost too,
which closes DNS rebinding.

**`/docs/<id>/files/` never serves outside the document's folder**: the
relative path is resolved (symlinks included) and must stay under that
folder; anything else is a 403.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import browser, htmldoc, registry, sender
from .config import Config

log = logging.getLogger("annotate")

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 2 * 1024 * 1024
CLIENT_HEADER = "X-Annotate"


class ServerError(Exception):
    """The daemon could not start."""


class Refused(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def contained_file(folder: Path, relative: str) -> Path:
    """`folder/relative`, resolved, or Refused if it leaves `folder`."""
    if "\x00" in relative:
        raise Refused(HTTPStatus.BAD_REQUEST, "invalid path")
    root = folder.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise Refused(HTTPStatus.FORBIDDEN, "path outside the document folder")
    if not target.is_file():
        raise Refused(HTTPStatus.NOT_FOUND, "no such file")
    return target


class AnnotateServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], cfg: Config,
                 send: sender.Sender) -> None:
        super().__init__(address, Handler)
        self.cfg = cfg
        self.sender = send

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def base_url(self) -> str:
        """The URL handed to Windows. IPv4 loopback, never `localhost`:
        with WSL `networkingMode=mirrored`, Windows resolves `localhost` to
        ::1 first, which a socket bound to 127.0.0.1 in WSL never receives
        (measured 2026-10-05: 127.0.0.1 answers in 22 ms, localhost times
        out)."""
        return f"http://127.0.0.1:{self.port}"


class Handler(BaseHTTPRequestHandler):
    server: AnnotateServer
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, data: object) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _check_host(self) -> None:
        host = (self.headers.get("Host") or "").lower()
        allowed = {f"{name}:{self.server.port}" for name in
                   ("localhost", "127.0.0.1", "[::1]")}
        if host not in allowed:
            raise Refused(HTTPStatus.FORBIDDEN, f"unexpected Host {host!r}")

    def _check_client(self) -> None:
        if self.headers.get(CLIENT_HEADER) is None:
            raise Refused(HTTPStatus.FORBIDDEN,
                          f"missing {CLIENT_HEADER} header (cross-site request?)")
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {
                f"http://localhost:{self.server.port}",
                f"http://127.0.0.1:{self.server.port}"}:
            raise Refused(HTTPStatus.FORBIDDEN, f"foreign Origin {origin!r}")

    def _body(self) -> object:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise Refused(HTTPStatus.BAD_REQUEST, "bad Content-Length") from exc
        if length > MAX_BODY:
            raise Refused(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Refused(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc

    def _route(self, method: str) -> None:
        try:
            self._check_host()
            if method in ("PUT", "POST", "DELETE"):
                self._check_client()
            split = urlsplit(self.path)
            parts = [unquote(p) for p in split.path.split("/") if p]
            self._dispatch(method, parts, parse_qs(split.query), split.path)
        except Refused as exc:
            self._error(exc.status, str(exc))
        except registry.UnknownDocument as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except registry.InvalidAnnotations as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (sender.Busy, sender.NothingToSend) as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (registry.RegistryError, sender.SendError, browser.OpenError,
                htmldoc.HtmlDocError) as exc:
            log.error("%s %s: %s", method, self.path, exc)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._route("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._route("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    # -- routes -----------------------------------------------------------
    def _dispatch(self, method: str, parts: list[str],
                  query: dict[str, list[str]], raw_path: str) -> None:
        if method == "GET" and not parts:
            return self._index()
        if method == "GET" and parts[0] == "static" and len(parts) == 2:
            target = contained_file(STATIC, parts[1])
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self._send(HTTPStatus.OK, target.read_bytes(),
                              f"{kind}; charset=utf-8")
        if method == "GET" and parts[0] == "docs" and len(parts) >= 2:
            entry = registry.get(parts[1])
            path = Path(entry["path"])
            if len(parts) == 2:
                return self._document(entry, path)
            if parts[2] == "files" and len(parts) >= 4:
                # Rebuild the relative path from the RAW url, so that an
                # encoded slash or `..` reaches the containment check as is.
                prefix = f"/docs/{parts[1]}/files/"
                relative = unquote(raw_path[len(prefix):]) if raw_path.startswith(prefix) \
                    else "/".join(parts[3:])
                target = contained_file(path.parent, relative)
                kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return self._send(HTTPStatus.OK, target.read_bytes(), kind)
            raise Refused(HTTPStatus.NOT_FOUND, "not found")
        if parts[:2] == ["api", "docs"]:
            return self._api(method, parts[2:], query)
        raise Refused(HTTPStatus.NOT_FOUND, "not found")

    def _document(self, entry: dict[str, Any], path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise Refused(HTTPStatus.GONE, f"{path} no longer exists") from exc
        except OSError as exc:
            raise Refused(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from exc
        page = htmldoc.inject_overlay(text, entry["id"])
        self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")

    def _with_running(self, item: dict[str, Any]) -> dict[str, Any]:
        return {**item, "running": item["id"] in self.server.sender.running()}

    def _api(self, method: str, rest: list[str], query: dict[str, list[str]]) -> None:
        if not rest:
            if method != "GET":
                raise Refused(HTTPStatus.METHOD_NOT_ALLOWED, "GET only")
            docs = [self._with_running(d) for d in registry.summary()]
            return self._json(HTTPStatus.OK, {"port": self.server.port, "docs": docs})
        doc_id, action = rest[0], (rest[1] if len(rest) > 1 else "")
        if len(rest) > 2:
            raise Refused(HTTPStatus.NOT_FOUND, "not found")
        if not action:
            if method == "GET":
                items = [d for d in registry.summary() if d["id"] == doc_id]
                if not items:
                    raise registry.UnknownDocument(f"no document {doc_id!r}")
                return self._json(HTTPStatus.OK, self._with_running(items[0]))
            if method == "DELETE":
                if doc_id in self.server.sender.running():
                    raise sender.Busy("a session is running for this document")
                delete = query.get("delete", ["0"])[0] in ("1", "true", "yes")
                entry = registry.forget(doc_id, delete_file=delete)
                log.info("forgot %s (%s)%s", doc_id, entry["path"],
                         ", file deleted" if delete else "")
                return self._json(HTTPStatus.OK, {"forgotten": doc_id,
                                                  "deleted": delete})
        if action == "annotations":
            if method == "GET":
                registry.get(doc_id)
                return self._json(HTTPStatus.OK,
                                  {"annotations": registry.list_annotations(doc_id)})
            if method == "PUT":
                items = registry.replace_annotations(doc_id, self._body())
                return self._json(HTTPStatus.OK, {"annotations": items})
        if method == "POST" and action in ("send", "new-session"):
            result = self.server.sender.dispatch(doc_id, fresh=action == "new-session")
            return self._json(HTTPStatus.ACCEPTED, result)
        if method == "POST" and action == "open":
            registry.get(doc_id)
            url = f"{self.server.base_url()}/docs/{doc_id}"
            return self._json(HTTPStatus.OK, {"url": url,
                                              "opener": browser.open_url(url)})
        raise Refused(HTTPStatus.NOT_FOUND, "not found")

    def _index(self) -> None:
        rows = []
        for doc in registry.summary():
            title = htmldoc.html.escape(doc.get("title") or doc["path"])
            rows.append(
                f'<tr><td><a href="/docs/{doc["id"]}">{title}</a></td>'
                f'<td>{doc["status"]}</td><td>{doc["pending"]}/{doc["annotations"]}</td>'
                f'<td><code>{htmldoc.html.escape(doc["path"])}</code></td></tr>')
        page = ("<!doctype html><meta charset='utf-8'><title>annotate</title>"
                "<style>body{font:15px system-ui;margin:2em}td{padding:4px 10px}"
                "code{font-size:12px;color:#555}</style><h1>annotate</h1>"
                "<table><tr><th>Document</th><th>Status</th><th>To send / all</th>"
                "<th>Path</th></tr>" + "".join(rows) + "</table>")
        self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")


def make_server(cfg: Config, *, port: int | None = None,
                send: sender.Sender | None = None) -> AnnotateServer:
    try:
        return AnnotateServer(("127.0.0.1", cfg.port if port is None else port),
                              cfg, send or sender.Sender(cfg))
    except OSError as exc:
        raise ServerError(f"cannot listen on 127.0.0.1:{cfg.port}: {exc}") from exc


class _Stop:
    """Set by the signal handler. A plain attribute, no lock: a handler that
    takes a lock can deadlock when a second signal interrupts it
    (remarkable-sync, 2026-09-15, `Event.set()` re-entered)."""
    requested = False


def serve(cfg: Config) -> int:
    srv = make_server(cfg)
    for doc_id in sender.recover_stale():
        log.info("recovered %s", doc_id)

    def on_signal(signum: int, frame: FrameType | None) -> None:
        _Stop.requested = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    worker = threading.Thread(target=srv.serve_forever, name="http", daemon=True)
    worker.start()
    log.info("annotate daemon listening on %s", srv.base_url())
    while not _Stop.requested:
        time.sleep(0.2)
    # From here on, a late signal must not kill the interpreter while it
    # finalises (default action restored by CPython at shutdown).
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    srv.shutdown()
    srv.server_close()
    running = srv.sender.running()
    log.info("annotate daemon stopped%s", f" ({len(running)} session(s) interrupted)"
             if running else "")
    return 0
