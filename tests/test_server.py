from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from annotate import browser, listeners, registry, sender, server
from annotate.config import Config
from fakes import RecordingOpener


class Daemon:
    def __init__(self, srv: server.AnnotateServer) -> None:
        self.srv = srv
        self.port = srv.port

    def request(self, method, path, body=None, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"localhost:{self.port}"}
        if method in ("PUT", "POST", "DELETE"):
            hdrs["X-Annotate"] = "test"
        hdrs.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        if raw:
            return resp.status, payload
        try:
            return resp.status, json.loads(payload or b"null")
        except json.JSONDecodeError:
            return resp.status, payload.decode("utf-8", "replace")


@pytest.fixture
def daemon():
    opener = RecordingOpener()
    cfg = Config()
    srv = server.make_server(cfg, port=0, send=sender.Sender(cfg, opener=opener))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    d = Daemon(srv)
    d.opener = opener  # type: ignore[attr-defined]
    yield d
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def doc_with_files(make_doc):
    path = make_doc("<h1>T</h1><p>Hello</p><img src='figures/f.png'>")
    figures = path.parent / "figures"
    figures.mkdir()
    (figures / "f.png").write_bytes(b"\x89PNG fake")
    (path.parent.parent / "secret.txt").write_text("outside the docs folder")
    return path


def test_document_is_served_from_disk_with_the_overlay(daemon, make_doc):
    path = make_doc("<p>version one</p>")
    doc_id = registry.register(path)["id"]
    status, page = daemon.request("GET", f"/docs/{doc_id}")
    assert status == 200
    assert 'src="/static/annotate.js"' in page and "version one" in page
    path.write_text(path.read_text().replace("version one", "version two"))
    assert "version two" in daemon.request("GET", f"/docs/{doc_id}")[1]


def test_relative_files_are_served_and_nothing_outside_the_folder(daemon, doc_with_files):
    doc_id = registry.register(doc_with_files)["id"]
    status, body = daemon.request("GET", f"/docs/{doc_id}/files/figures/f.png", raw=True)
    assert (status, body) == (200, b"\x89PNG fake")
    outside = doc_with_files.parent.parent / "secret.txt"
    (doc_with_files.parent / "link.txt").symlink_to(outside)
    for attempt in ("../secret.txt", "%2e%2e/secret.txt", "..%2fsecret.txt",
                    "figures/../../secret.txt", "%2Fetc%2Fpasswd", "link.txt"):
        status, body = daemon.request("GET", f"/docs/{doc_id}/files/{attempt}", raw=True)
        assert status == 403, (attempt, status)
        assert b"outside the docs folder" not in body


def test_contained_file_refuses_every_escape(tmp_path):
    folder = tmp_path / "docs"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "ok.txt").write_text("ok")
    (tmp_path / "secret").write_text("no")
    assert server.contained_file(folder, "sub/ok.txt").read_text() == "ok"
    for bad in ("../secret", "sub/../../secret", str(tmp_path / "secret"), "a\x00b"):
        with pytest.raises(server.Refused):
            server.contained_file(folder, bad)


def test_annotations_round_trip_and_status(daemon, make_doc):
    doc_id = registry.register(make_doc("<p>Hello</p>"))["id"]
    items = [{"id": "n1", "selector": "body > p:nth-of-type(1)", "quote": "Hello",
              "note": "louder", "offset_x": 5, "offset_y": 6}]
    status, body = daemon.request("PUT", f"/api/docs/{doc_id}/annotations", items)
    assert status == 200 and body["annotations"][0]["note"] == "louder"
    assert daemon.request("GET", f"/api/docs/{doc_id}/annotations")[1]["annotations"][0]["id"] == "n1"
    status, listing = daemon.request("GET", "/api/docs")
    entry = listing["docs"][0]
    assert (entry["status"], entry["annotations"], entry["pending"]) == ("annotated", 1, 1)
    assert daemon.request("PUT", f"/api/docs/{doc_id}/annotations", {"x": 1})[0] == 400


def test_send_without_a_listener_opens_a_tab(daemon, make_doc):
    doc_id = registry.register(make_doc("<p>Hello</p>"), session="s0")["id"]
    assert daemon.request("POST", f"/api/docs/{doc_id}/send")[0] == 409   # nothing yet
    daemon.request("PUT", f"/api/docs/{doc_id}/annotations",
                   [{"id": "n1", "selector": "body", "quote": "", "note": "x"}])
    status, body = daemon.request("POST", f"/api/docs/{doc_id}/send")
    assert status == 200 and body["count"] == 1 and body["target"] == "terminal"
    assert registry.get(doc_id)["status"] == "delivered"
    assert len(daemon.opener.calls) == 1


def _hold(daemon, doc_id, body=None):
    """A real `wait` request on its own connection: (thread, box)."""
    box: dict[str, object] = {}

    def run():
        box["answer"] = daemon.request("POST", f"/api/docs/{doc_id}/wait",
                                       body or {"session": "abc-123", "cwd": "/tmp"})

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(200):
        if daemon.request("GET", f"/api/docs/{doc_id}")[1].get("listening"):
            break
        threading.Event().wait(0.01)
    return thread, box


def test_a_held_wait_receives_the_notes_and_no_tab_opens(daemon, make_doc):
    doc_id = registry.register(make_doc("<p>Hello</p>"), session="s0")["id"]
    assert daemon.request("GET", f"/api/docs/{doc_id}")[1]["listening"] is False
    thread, box = _hold(daemon, doc_id)
    assert daemon.request("GET", "/api/docs")[1]["docs"][0]["listening"] is True
    daemon.request("PUT", f"/api/docs/{doc_id}/annotations",
                   [{"id": "n1", "selector": "body", "quote": "", "note": "make it red"}])
    status, body = daemon.request("POST", f"/api/docs/{doc_id}/send")
    thread.join(10)
    assert (status, body["target"], body["session"]) == (200, "session", "abc-123")
    answer_status, answer = box["answer"]  # type: ignore[misc]
    assert answer_status == 200 and "make it red" in answer["prompt"]
    assert daemon.opener.calls == []
    assert registry.get(doc_id)["cwd"] == "/tmp"


def test_a_wait_answers_nothing_when_its_hold_expires(daemon, make_doc):
    doc_id = registry.register(make_doc("<p>Hello</p>"))["id"]
    daemon.srv.wait_hold = 0.3
    assert daemon.request("POST", f"/api/docs/{doc_id}/wait", {}) == (200, {"prompt": None})


def test_a_wait_whose_client_left_stops_listening(daemon, make_doc, monkeypatch):
    monkeypatch.setattr(listeners, "STEP", 0.05)
    doc_id = registry.register(make_doc("<p>Hello</p>"))["id"]
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
    conn.request("POST", f"/api/docs/{doc_id}/wait", body=b"{}",
                 headers={"Host": f"localhost:{daemon.port}", "X-Annotate": "t"})
    for _ in range(200):
        if daemon.request("GET", f"/api/docs/{doc_id}")[1]["listening"]:
            break
        threading.Event().wait(0.01)
    assert daemon.request("GET", f"/api/docs/{doc_id}")[1]["listening"] is True
    conn.close()                      # the session ended: its background command died
    for _ in range(200):
        if not daemon.request("GET", f"/api/docs/{doc_id}")[1]["listening"]:
            break
        threading.Event().wait(0.01)
    assert daemon.request("GET", f"/api/docs/{doc_id}")[1]["listening"] is False


def test_a_wait_on_an_unknown_document_is_a_404(daemon):
    assert daemon.request("POST", "/api/docs/nope1234/wait", {})[0] == 404


def test_a_bogus_session_or_folder_in_a_wait_is_ignored(daemon, make_doc):
    doc_id = registry.register(make_doc("<p>Hello</p>"), session="s0")["id"]
    before = registry.get(doc_id)["cwd"]
    thread, _ = _hold(daemon, doc_id, {"session": "x; rm -rf /", "cwd": "relative/dir"})
    daemon.request("PUT", f"/api/docs/{doc_id}/annotations",
                   [{"id": "n1", "selector": "body", "quote": "", "note": "x"}])
    status, body = daemon.request("POST", f"/api/docs/{doc_id}/send")
    thread.join(10)
    assert body["target"] == "session" and body["session"] == ""
    assert registry.get(doc_id)["cwd"] == before and registry.get(doc_id)["session_id"] == "s0"


def test_mutations_need_the_client_header_and_a_local_origin(daemon, make_doc):
    doc_id = registry.register(make_doc("<p>Hello</p>"))["id"]
    path = f"/api/docs/{doc_id}/send"
    # With the header and no Origin, the request reaches the sender (409:
    # nothing to send yet).
    assert daemon.request("POST", path)[0] == 409
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
    conn.request("POST", path, headers={"Host": f"localhost:{daemon.port}"})
    assert conn.getresponse().status == 403          # header missing
    conn.close()
    assert daemon.request("POST", path, headers={"Origin": "https://evil.example"})[0] == 403
    assert daemon.request("GET", "/api/docs", headers={"Host": "evil.example:80"})[0] == 403


def test_forget_and_delete(daemon, make_doc):
    keep = make_doc("<p>a</p>", name="a.html")
    drop = make_doc("<p>b</p>", name="b.html")
    a = registry.register(keep)["id"]
    b = registry.register(drop)["id"]
    assert daemon.request("DELETE", f"/api/docs/{a}")[0] == 200
    assert daemon.request("DELETE", f"/api/docs/{b}?delete=1")[0] == 200
    assert keep.exists() and not drop.exists()
    assert daemon.request("GET", "/api/docs")[1]["docs"] == []
    assert daemon.request("GET", f"/docs/{a}")[0] == 404


def test_open_uses_the_browser_module(daemon, make_doc, monkeypatch):
    doc_id = registry.register(make_doc("<p>a</p>"))["id"]
    opened = []
    monkeypatch.setattr(browser, "open_url", lambda url: opened.append(url) or "fake")
    status, body = daemon.request("POST", f"/api/docs/{doc_id}/open")
    assert status == 200 and opened == [f"http://127.0.0.1:{daemon.port}/docs/{doc_id}"]


def test_static_files_and_index(daemon, make_doc):
    registry.register(make_doc("<p>a</p>", title="Indexed"))
    status, js = daemon.request("GET", "/static/annotate.js", raw=True)
    assert status == 200 and b"data-annotate-doc" in js
    assert daemon.request("GET", "/static/../server.py", raw=True)[0] in (403, 404)
    status, page = daemon.request("GET", "/")
    assert status == 200 and "Indexed" in page


def test_a_missing_file_is_gone_not_a_crash(daemon, make_doc):
    path = make_doc("<p>a</p>")
    doc_id = registry.register(path)["id"]
    Path(path).unlink()
    assert daemon.request("GET", f"/docs/{doc_id}")[0] == 410


def test_urls_given_to_windows_use_the_ipv4_loopback(daemon):
    # Measured 2026-10-05 with networkingMode=mirrored: from Windows,
    # 127.0.0.1:8765 answers in 22 ms, localhost:8765 times out (tried as ::1).
    assert daemon.srv.base_url() == f"http://127.0.0.1:{daemon.port}"
