"""Register -> annotate over HTTP -> send -> a real subprocess receives the prompt.

The subprocess is `fake-claude` (tests/fakes.py), a real executable launched
by the real `subprocess.Popen`, through real pipes, in the document's
working directory. Only its name and its answer are fake.
"""
from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from annotate import config, registry, sender, server
from fakes import read_log, write_fake_claude

BODY = ("<h1>Study</h1><h2>Reuse table</h2>"
        "<p id='verdict'>The transport layer is dropped entirely.</p>"
        "<h2>Phase two</h2><p>Highlighting comes later.</p>")


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                 headers={"Host": f"localhost:{port}", "X-Annotate": "e2e"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data or b"null")


def _wait_status(doc_id, wanted, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if registry.get(doc_id)["status"] == wanted:
            return
        time.sleep(0.05)
    raise AssertionError(f"status stayed {registry.get(doc_id)['status']!r}")


@pytest.fixture
def running(tmp_path, monkeypatch):
    def start(gone=False):
        fake, log = write_fake_claude(tmp_path / "bin", gone=gone)
        monkeypatch.setenv("ANNOTATE_CLAUDE", str(fake))
        cfg = config.load_config()
        srv = server.make_server(cfg, port=0, send=sender.Sender(cfg))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv.port, log
    started: list[server.AnnotateServer] = []
    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def test_a_note_reaches_the_producing_session(running, make_doc):
    port, log = running()
    path = make_doc(BODY, title="Reuse study")
    doc_id = registry.register(path, session="sess-producer")["id"]
    note = {"id": "n1", "number": 1, "selector": "#verdict",
            "quote": "The transport layer is dropped entirely.",
            "note": "Say WHY the transport layer is dropped.",
            "offset_x": 12, "offset_y": 4}
    assert _request(port, "PUT", f"/api/docs/{doc_id}/annotations", [note])[0] == 200
    status, body = _request(port, "POST", f"/api/docs/{doc_id}/send")
    assert status == 202
    _wait_status(doc_id, "answered")

    [call] = read_log(log)
    assert "Say WHY the transport layer is dropped." in call["stdin"]
    assert '"The transport layer is dropped entirely."' in call["stdin"]
    assert "Section: Reuse table" in call["stdin"]
    assert str(path) in call["stdin"] and '"Reuse study"' in call["stdin"]
    assert call["argv"][-2:] == ["--resume", "sess-producer"]
    assert "acceptEdits" in call["argv"]
    assert call["cwd"] == str(path.parent.parent)
    assert call["marker"] == "1"
    entry = registry.get(doc_id)
    assert entry["session_id"] == "fake-session-42"
    assert entry["last_cost_usd"] == 0.0123
    assert registry.list_annotations(doc_id)[0]["sent_at"] == entry["sent_at"]
    # A second send has nothing left: the note is not sent twice.
    assert _request(port, "POST", f"/api/docs/{doc_id}/send")[0] == 409
    assert len(read_log(log)) == 1


def test_a_vanished_session_reopens_with_the_same_notes(running, make_doc):
    port, log = running(gone=True)
    doc_id = registry.register(make_doc(BODY), session="sess-purged")["id"]
    _request(port, "PUT", f"/api/docs/{doc_id}/annotations",
             [{"id": "n1", "selector": "body > p:nth-of-type(2)",
               "quote": "Highlighting comes later.", "note": "Give a date."}])
    _request(port, "POST", f"/api/docs/{doc_id}/send")
    _wait_status(doc_id, "answered")
    resumed, fresh = read_log(log)
    assert "--resume" in resumed["argv"] and "--resume" not in fresh["argv"]
    assert fresh["stdin"].startswith("You did not produce this document")
    assert "Give a date." in fresh["stdin"] and "Section: Phase two" in fresh["stdin"]
