from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from annotate import browser, claude, listeners, registry, sender, server
from annotate.config import Config
from fakes import RecordingOpener, compact, conversation, register_session, transcript


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
    assert status == 200 and '<script src="/static/index.js">' in page
    status, js = daemon.request("GET", "/static/index.js", raw=True)
    assert status == 200 and b"/api/docs" in js and b"/api/folders" in js
    assert b"/api/scan" in js and b"/api/sessions" in js and b"/session'" in js


def test_a_missing_file_is_gone_not_a_crash(daemon, make_doc):
    path = make_doc("<p>a</p>")
    doc_id = registry.register(path)["id"]
    Path(path).unlink()
    assert daemon.request("GET", f"/docs/{doc_id}")[0] == 410


def test_urls_given_to_windows_use_the_ipv4_loopback(daemon):
    # Measured 2026-10-05 with networkingMode=mirrored: from Windows,
    # 127.0.0.1:8765 answers in 22 ms, localhost:8765 times out (tried as ::1).
    assert daemon.srv.base_url() == f"http://127.0.0.1:{daemon.port}"


def test_the_listing_groups_documents_by_session(daemon, make_doc):
    a = registry.register(make_doc("<p>a</p>", name="a.html"), session="s-one")["id"]
    b = registry.register(make_doc("<p>b</p>", name="b.html"), session="")["id"]
    listing = daemon.request("GET", "/api/docs")[1]
    assert [g["session_id"] for g in listing["sessions"]] == ["s-one", ""]
    assert listing["sessions"][0]["docs"] == [a] and listing["sessions"][1]["docs"] == [b]
    assert listing["sessions"][1]["title"] == "documents without a known session"


def test_the_daemon_buttons_go_through_service_control(daemon, monkeypatch):
    asked = []
    monkeypatch.setattr(server.service, "control", asked.append)
    assert daemon.request("POST", "/api/daemon/restart") == (202, {"daemon": "restart"})
    assert daemon.request("POST", "/api/daemon/stop")[0] == 202
    assert asked == ["restart", "stop"]


def test_a_daemon_button_without_the_client_header_is_refused(daemon, monkeypatch):
    asked = []
    monkeypatch.setattr(server.service, "control", asked.append)
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
    conn.request("POST", "/api/daemon/stop", headers={"Host": f"localhost:{daemon.port}"})
    assert conn.getresponse().status == 403 and asked == []


def test_a_refused_daemon_action_is_a_409_with_its_reason(daemon, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    status, body = daemon.request("POST", "/api/daemon/stop")
    assert status == 409 and "not started by systemd" in body["error"]
    assert daemon.request("POST", "/api/daemon/start")[0] == 409


def test_the_send_answer_carries_the_sentence_every_surface_shows(daemon, make_doc):
    doc_id = registry.register(make_doc("<p>Hello</p>"))["id"]
    daemon.request("PUT", f"/api/docs/{doc_id}/annotations",
                   [{"id": "n1", "selector": "body", "quote": "", "note": "x"}])
    body = daemon.request("POST", f"/api/docs/{doc_id}/send")[1]
    assert body["message"] == "1 note(s) opened in a terminal tab, in a NEW session."


# -- tracked folders and the rescan (2026-10-07) --------------------------------

def _settled(daemon):
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = daemon.request("GET", "/api/folders")[1]
        if body["scan"]["running"] is None:
            return body
        time.sleep(0.02)
    raise AssertionError("the rescan never ended")


def test_a_folder_added_from_the_page_is_tracked_and_its_recent_files_taken(daemon,
                                                                            tmp_path):
    import os
    import time
    reports = tmp_path / "reports"
    (reports / "run-1").mkdir(parents=True)
    fresh = reports / "run-1" / "fresh.html"
    fresh.write_text("<title>Fresh</title>")
    old = reports / "old.html"
    old.write_text("<title>Old</title>")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    status, body = daemon.request("POST", "/api/folders", {"path": str(reports), "days": 7})
    assert status == 201 and body["started"] is True
    listing = _settled(daemon)
    assert {"path": str(reports.resolve()), "docs_only": False, "exists": True} \
        in listing["folders"]
    assert listing["scan"]["last"]["added"] == 1
    assert [d["path"] for d in registry.all_docs().values()] == [str(fresh)]


def test_a_rescan_from_the_page_covers_every_folder(daemon, tmp_path):
    doc = tmp_path / "workspace" / "repo" / "docs" / "a.html"
    doc.parent.mkdir(parents=True)
    doc.write_text("<title>A</title>")
    assert daemon.request("POST", "/api/scan", {"days": 1}) == \
        (202, {"started": True, "days": 1.0})
    assert _settled(daemon)["scan"]["last"]["added"] == 1
    assert daemon.request("POST", "/api/scan")[1]["days"] == 7.0    # no body: the default
    _settled(daemon)


def test_a_folder_removed_from_the_page_is_no_longer_listed(daemon, tmp_path):
    ws = str((tmp_path / "workspace").resolve())
    (tmp_path / "workspace").mkdir()
    assert [f["path"] for f in daemon.request("GET", "/api/folders")[1]["folders"]] == [ws]
    status, body = daemon.request("DELETE", "/api/folders?path=" + ws)
    assert status == 200 and body["removed"]["path"] == ws
    assert daemon.request("GET", "/api/folders")[1]["folders"] == []
    assert daemon.request("DELETE", "/api/folders?path=" + ws)[0] == 400


@pytest.mark.parametrize("body", [{"path": "/usr"}, {"path": "relative"}, {},
                                  {"path": "~", "days": 0}])
def test_a_bad_folder_or_window_is_a_400_with_its_reason(daemon, body):
    status, answer = daemon.request("POST", "/api/folders", body)
    assert status == 400 and answer["error"]
    assert daemon.request("POST", "/api/scan", {"days": "x"})[0] == 400


def test_folder_and_rescan_requests_need_the_client_header(daemon, tmp_path):
    for method, path in (("POST", "/api/folders"), ("POST", "/api/scan"),
                         ("DELETE", "/api/folders?path=/x")):
        conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
        conn.request(method, path, body=json.dumps({"path": str(tmp_path)}),
                     headers={"Host": f"localhost:{daemon.port}"})
        assert conn.getresponse().status == 403
        conn.close()
    assert daemon.srv.catchups.state() == {"running": None, "last": None}


# -- attaching a document to a conversation (2026-10-08) -----------------------

def _attach(daemon, doc_id, body, headers=None):
    return daemon.request("POST", f"/api/docs/{doc_id}/session", body, headers=headers)


def _groups(daemon):
    """(session, documents) per group; documents sorted, since two registered
    within the same second have no defined order."""
    return [(g["session_id"], sorted(g["docs"]))
            for g in daemon.request("GET", "/api/docs")[1]["sessions"]]


def test_attaching_a_document_writes_its_session_and_moves_it_to_that_group(daemon, make_doc,
                                                                              tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    conversation(claude.projects_dir(), "s-att", cwd=str(work), title="Attached session")
    a = registry.register(make_doc("<p>a</p>", name="a.html"), session="")["id"]
    b = registry.register(make_doc("<p>b</p>", name="b.html"), session="")["id"]
    assert _groups(daemon) == [("", sorted([a, b]))]
    status, body = _attach(daemon, a, {"session": "s-att"})
    assert status == 200 and body["doc"]["session_id"] == "s-att"
    assert body["message"].startswith("Test doc attached to Attached session")
    assert registry.get(a)["session_id"] == "s-att" and registry.get(a)["cwd"] == str(work)
    assert registry.get(b)["session_id"] == ""
    assert _groups(daemon) == [("s-att", [a]), ("", [b])]
    assert daemon.request("GET", "/api/docs")[1]["sessions"][0]["title"] == "Attached session"


def test_a_session_this_machine_cannot_resume_is_refused_with_a_400(daemon, make_doc):
    projects = claude.projects_dir()
    transcript(projects, "s-title-only", lines=(compact({"type": "ai-title", "aiTitle": "T"}),))
    conversation(projects, "agent-sub", folder="-r/s-parent/subagents")
    doc = registry.register(make_doc("<p>a</p>"), session="s-keep")["id"]
    for body in ({"session": "nope-1234"}, {"session": "s-title-only"},
                 {"session": "agent-sub"}, {"session": "../etc"}, {"session": "a b"},
                 {"session": 42}, {}, {"session": "s-keep", "cwd": 7}):
        status, answer = _attach(daemon, doc, body)
        assert status == 400 and answer["error"], (body, status, answer)
    assert "no conversation nope-1234 on this machine" in _attach(
        daemon, doc, {"session": "nope-1234"})[1]["error"]
    assert registry.get(doc)["session_id"] == "s-keep"
    assert _attach(daemon, "nope0000", {"session": ""})[0] == 404


def test_attaching_needs_the_client_header_and_a_local_origin(daemon, make_doc):
    conversation(claude.projects_dir(), "s-ok")
    doc = registry.register(make_doc("<p>a</p>"), session="")["id"]
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
    conn.request("POST", f"/api/docs/{doc}/session", body=json.dumps({"session": "s-ok"}),
                 headers={"Host": f"localhost:{daemon.port}"})
    assert conn.getresponse().status == 403
    conn.close()
    assert _attach(daemon, doc, {"session": "s-ok"},
                   headers={"Origin": "https://evil.example"})[0] == 403
    assert registry.get(doc)["session_id"] == ""
    assert _attach(daemon, doc, {"session": "s-ok"})[0] == 200     # the same, with the header
    assert registry.get(doc)["session_id"] == "s-ok"


def test_detaching_puts_the_document_back_among_those_without_a_session(daemon, make_doc):
    conversation(claude.projects_dir(), "s-one")
    doc = registry.register(make_doc("<p>a</p>"), session="s-one")["id"]
    assert _groups(daemon) == [("s-one", [doc])]
    status, body = _attach(daemon, doc, {"session": ""})
    assert status == 200 and "detached" in body["message"]
    assert registry.get(doc)["session_id"] == ""
    assert _groups(daemon) == [("", [doc])]


def test_an_attach_keeps_the_status_and_never_a_deleted_worktree_folder(daemon, make_doc,
                                                                         tmp_path):
    path = make_doc("<p>a</p>")
    repo = tmp_path / "workspace" / "repo"
    conversation(claude.projects_dir(), "s-wt",
                 cwd=str(repo / ".claude" / "worktrees" / "agent-x"))   # gone since
    doc = registry.register(path, session="s0")["id"]
    registry.update(doc, status="delivered")
    assert _attach(daemon, doc, {"session": "s-wt"})[0] == 200
    assert registry.get(doc)["cwd"] == str(repo)
    assert registry.get(doc)["status"] == "delivered", "an attach is not the session's answer"
    assert _attach(daemon, doc, {"session": "s-wt", "cwd": str(tmp_path)})[0] == 200
    assert registry.get(doc)["cwd"] == str(tmp_path)
    assert _attach(daemon, doc, {"session": "s-wt", "cwd": "relative/dir"})[0] == 400
    assert registry.get(doc)["cwd"] == str(tmp_path)


def test_the_session_list_is_read_without_the_client_header(daemon, tmp_path):
    conversation(claude.projects_dir(), "s-a", cwd="/work", title="A", when=1000.0)
    conversation(claude.projects_dir(), "s-b", cwd="/other", when=2000.0)
    inbox_socket = tmp_path / "s.sock"
    inbox_socket.write_text("")
    register_session("s-a", inbox_socket)
    conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
    conn.request("GET", "/api/sessions", headers={"Host": f"localhost:{daemon.port}"})
    response = conn.getresponse()
    body = json.loads(response.read())
    conn.close()
    assert response.status == 200 and body["limit"] == claude.SESSION_LIMIT
    assert body["sessions"] == [
        {"id": "s-a", "title": "A", "cwd": "/work", "last_active": 1000.0, "open": True},
        {"id": "s-b", "title": "", "cwd": "/other", "last_active": 2000.0, "open": False}]
    assert daemon.request("POST", "/api/sessions", {})[0] == 405


def test_the_listing_says_which_session_groups_are_open(daemon, make_doc, tmp_path):
    registry.register(make_doc("<p>a</p>", name="a.html"), session="s-open")
    registry.register(make_doc("<p>b</p>", name="b.html"), session="s-closed")
    registry.register(make_doc("<p>c</p>", name="c.html"), session="")
    inbox_socket = tmp_path / "s.sock"
    inbox_socket.write_text("")
    register_session("s-open", inbox_socket)
    groups = daemon.request("GET", "/api/docs")[1]["sessions"]
    assert {g["session_id"]: g["open"] for g in groups} == {
        "s-open": True, "s-closed": False, "": False}


def test_the_folder_field_is_offered_subfolders(daemon, tmp_path):
    (tmp_path / "pick" / "alpha").mkdir(parents=True)
    from urllib.parse import quote
    body = daemon.request("GET", "/api/folders/suggest?path=" + quote(str(tmp_path / "pick") + "/"))[1]
    assert body["folders"] == [str((tmp_path / "pick" / "alpha").resolve())]
