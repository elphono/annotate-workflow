"""Register -> annotate over HTTP -> send -> the notes reach a session.

Two loops, each with real processes and real sockets:

- an open session: `annotate wait` runs as a real subprocess (what a Claude
  Code session starts in the background); the send makes it print the notes
  and exit 0, which is what wakes the session up;
- no open session: the real `terminal.open_session` runs a fake `wt.exe`
  that executes the generated script, which starts a fake `claude` in the
  document's folder with `--resume` and the notes.

Only the NAMES of `wt.exe` and `claude` are fake (the suite guard refuses
the real ones).
"""
from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import time

import pytest

from annotate import claude, config, registry, sender, server
from fakes import read_log, transcript, write_fake_claude, write_fake_wt

BODY = ("<h1>Study</h1><h2>Reuse table</h2>"
        "<p id='verdict'>The transport layer is dropped entirely.</p>"
        "<h2>Phase two</h2><p>Highlighting comes later.</p>")
NOTE = [{"id": "n1", "selector": "#verdict", "quote": "The transport layer",
         "at": "The [[transport]] layer", "note": "say why it is dropped",
         "offset_x": 4, "offset_y": 2}]


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                 headers={"Host": f"localhost:{port}", "X-Annotate": "e2e"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data or b"null")


@pytest.fixture
def start(monkeypatch):
    """Start a daemon with the configuration of the environment AT THAT TIME."""
    started: list[server.AnnotateServer] = []

    def run() -> server.AnnotateServer:
        cfg = config.load_config()
        srv = server.make_server(cfg, port=0, send=sender.Sender(cfg))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("ANNOTATE_PORT", str(srv.port))
        started.append(srv)
        return srv

    yield run
    for srv in started:
        srv.shutdown()
        srv.server_close()


def test_the_notes_reach_the_open_session_that_waits(start, make_doc):
    daemon = start()
    doc_id = registry.register(make_doc(BODY), session="sess-prod")["id"]
    waiting = subprocess.Popen(
        [sys.executable, "-m", "annotate.cli", "wait", doc_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**__import__("os").environ, "CLAUDE_CODE_SESSION_ID": "sess-open"})
    try:
        deadline = time.monotonic() + 20
        while not _request(daemon.port, "GET", f"/api/docs/{doc_id}")[1]["listening"]:
            assert time.monotonic() < deadline, "the waiter never reached the daemon"
            assert waiting.poll() is None, waiting.communicate()
            time.sleep(0.05)
        assert _request(daemon.port, "PUT", f"/api/docs/{doc_id}/annotations", NOTE)[0] == 200
        status, body = _request(daemon.port, "POST", f"/api/docs/{doc_id}/send")
        assert (status, body["target"], body["session"]) == (200, "session", "sess-open")
        out, err = waiting.communicate(timeout=20)
    finally:
        if waiting.poll() is None:
            waiting.kill()
            waiting.communicate()
    assert waiting.returncode == 0, err
    assert "Section: Reuse table" in out and "say why it is dropped" in out
    assert 'Clicked on (the word between [[ ]]): "The [[transport]] layer"' in out
    assert f"annotate wait {doc_id}`" in out              # how to get the next ones
    assert registry.get(doc_id)["status"] == "delivered"
    assert registry.get(doc_id)["session_id"] == "sess-open"


def test_without_an_open_session_a_tab_resumes_the_producing_one(
        start, make_doc, tmp_path, monkeypatch):
    write_fake_wt(tmp_path / "wt", monkeypatch)
    fake, log = write_fake_claude(tmp_path / "bin")
    monkeypatch.setenv("ANNOTATE_CLAUDE", str(fake))
    monkeypatch.setenv("WSL_DISTRO_NAME", "Test-Distro")
    daemon = start()
    path = make_doc(BODY)
    doc_id = registry.register(path, session="sess-prod")["id"]
    transcript(claude.projects_dir(), "sess-prod")
    _request(daemon.port, "PUT", f"/api/docs/{doc_id}/annotations", NOTE)
    status, body = _request(daemon.port, "POST", f"/api/docs/{doc_id}/send")
    assert (status, body["target"], body["session"]) == (200, "terminal", "sess-prod")
    [record] = read_log(log)
    assert record["cwd"] == registry.get(doc_id)["cwd"]
    assert record["argv"][:2] == ["--resume", "sess-prod"]
    assert "say why it is dropped" in record["argv"][2]
