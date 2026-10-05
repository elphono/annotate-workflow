from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from annotate import browser, registry, sender, server
from annotate.config import Config
from fakes import FakePopen, ok


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
    fake = FakePopen(*[ok("sess-srv")] * 5)
    cfg = Config()
    srv = server.make_server(cfg, port=0, send=sender.Sender(cfg, popen=fake))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    d = Daemon(srv)
    d.fake = fake  # type: ignore[attr-defined]
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


def test_send_goes_through_the_sender_and_answers_at_once(daemon, make_doc):
    doc_id = registry.register(make_doc("<p>Hello</p>"), session="s0")["id"]
    assert daemon.request("POST", f"/api/docs/{doc_id}/send")[0] == 409   # nothing yet
    daemon.request("PUT", f"/api/docs/{doc_id}/annotations",
                   [{"id": "n1", "selector": "body", "quote": "", "note": "x"}])
    status, body = daemon.request("POST", f"/api/docs/{doc_id}/send")
    assert status == 202 and body["count"] == 1
    for _ in range(100):
        if registry.get(doc_id)["status"] == "answered":
            break
        threading.Event().wait(0.05)
    assert registry.get(doc_id)["status"] == "answered"
    assert daemon.fake.procs[0].argv[-1] == "s0"


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
    assert status == 200 and opened == [f"http://localhost:{daemon.port}/docs/{doc_id}"]


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
