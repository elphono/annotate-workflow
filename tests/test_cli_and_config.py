from __future__ import annotations

import json
import socket
import threading

import pytest

from annotate import browser, cli, config, registry, sender, server
from fakes import RecordingOpener


def test_config_precedence_env_over_file_over_default(tmp_path, monkeypatch):
    assert config.load_config() == config.Config()
    path = tmp_path / "annotate-config.toml"
    path.write_text('port = 9001\nclaude_bin = "/opt/c"\n')
    assert config.load_config() == config.Config(9001, "/opt/c")
    monkeypatch.setenv("ANNOTATE_PORT", "9002")
    assert config.load_config().port == 9002


@pytest.mark.parametrize("value", ["0", "70000", "abc", "true"])
def test_invalid_ports_are_refused(monkeypatch, value):
    monkeypatch.setenv("ANNOTATE_PORT", value)
    with pytest.raises(config.ConfigError):
        config.load_config()


def test_register_list_forget(make_doc, capsys, monkeypatch):
    path = make_doc("<p>x</p>", title="CLI doc")
    assert cli.main(["register", str(path), "--session", "s1"]) == 0
    out = capsys.readouterr().out
    doc_id = out.split()[0]
    assert f"http://127.0.0.1:8765/docs/{doc_id}" in out
    assert cli.main(["list"]) == 0
    line = capsys.readouterr().out
    assert doc_id in line and "new" in line and str(path) in line
    # No daemon on the port: forget works directly.
    monkeypatch.setenv("ANNOTATE_PORT", str(_free_port()))
    assert cli.main(["forget", doc_id]) == 0
    assert registry.all_docs() == {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_status_says_unreachable_with_exit_1(monkeypatch, capsys):
    monkeypatch.setenv("ANNOTATE_PORT", str(_free_port()))
    assert cli.main(["status"]) == 1
    assert "unreachable" in capsys.readouterr().out


@pytest.fixture
def daemon(monkeypatch):
    cfg = config.Config()
    opener = RecordingOpener()
    srv = server.make_server(cfg, port=0, send=sender.Sender(cfg, opener=opener))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANNOTATE_PORT", str(srv.port))
    srv.opener = opener  # type: ignore[attr-defined]
    yield srv
    srv.shutdown()
    srv.server_close()


def _listening(doc_id):
    for _ in range(300):
        if registry.summary() and cli._listening(config.load_config(), doc_id):
            return True
        threading.Event().wait(0.01)
    return False


def test_send_and_status_go_through_the_daemon(daemon, make_doc, capsys):
    doc_id = registry.register(make_doc("<p>x</p>"), session="s0")["id"]
    assert cli.main(["send", doc_id]) == 1               # nothing to send: refused
    assert "409" in capsys.readouterr().err
    registry.replace_annotations(doc_id, [{"id": "n1", "selector": "body",
                                           "quote": "x", "note": "y"}])
    assert cli.main(["send", doc_id]) == 0
    assert "opened in a terminal tab, in a NEW session" in capsys.readouterr().out
    assert cli.main(["status"]) == 0
    assert "1 delivered" in capsys.readouterr().out


def test_wait_prints_the_notes_and_exits_when_they_are_sent(daemon, make_doc, capsys,
                                                             monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session-1")
    doc_id = registry.register(make_doc("<p>x</p>"), session="s0")["id"]
    box: dict[str, int] = {}
    # daemon: if the notes never come, the failing test must not hang the suite
    waiting = threading.Thread(target=lambda: box.update(code=cli.main(["wait", doc_id])),
                               daemon=True)
    waiting.start()
    assert _listening(doc_id)
    registry.replace_annotations(doc_id, [{"id": "n1", "selector": "body",
                                           "quote": "x", "note": "bigger title"}])
    assert cli.main(["send", doc_id]) == 0
    waiting.join(10)
    out = capsys.readouterr().out
    assert box["code"] == 0 and "bigger title" in out
    assert "delivered to the open session live-session-1" in out
    assert registry.get(doc_id)["session_id"] == "live-session-1"


def test_wait_outlives_a_daemon_that_is_not_there_yet(make_doc, monkeypatch, capsys):
    """A daemon restart must not wake the session up with an error."""
    port = _free_port()
    monkeypatch.setenv("ANNOTATE_PORT", str(port))
    doc_id = registry.register(make_doc("<p>x</p>"))["id"]
    box: dict[str, int] = {}
    waiting = threading.Thread(
        target=lambda: box.update(code=cli.main(["wait", doc_id, "--retry", "0.05"])),
        daemon=True)
    waiting.start()
    threading.Event().wait(0.4)
    assert waiting.is_alive(), "wait gave up while the daemon was away"
    cfg = config.Config(port=port)
    srv = server.make_server(cfg, port=port, send=sender.Sender(cfg, opener=RecordingOpener()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _listening(doc_id)
        registry.replace_annotations(doc_id, [{"id": "n1", "selector": "body",
                                               "quote": "x", "note": "late note"}])
        srv.sender.dispatch(doc_id)
        waiting.join(10)
        assert box["code"] == 0 and "late note" in capsys.readouterr().out
    finally:
        srv.shutdown()
        srv.server_close()


def test_wait_on_an_unknown_document_fails_at_once(capsys):
    assert cli.main(["wait", "nope1234"]) == 1
    assert "nope1234" in capsys.readouterr().err


def test_register_for_the_hook_tells_the_session_to_wait_unless_one_listens(
        daemon, make_doc, capsys):
    path = make_doc("<p>x</p>")
    assert cli.main(["register", str(path), "--session", "s1", "--hook"]) == 0
    answer = json.loads(capsys.readouterr().out)
    doc_id = registry.register(path)["id"]
    context = answer["hookSpecificOutput"]["additionalContext"]
    assert answer["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert f"annotate wait {doc_id}`" in context and "run_in_background" in context
    box: dict[str, object] = {}
    waiter = threading.Thread(target=lambda: box.update(
        w=daemon.sender.board.wait(doc_id, "s1", "/", lambda: True, 10)), daemon=True)
    waiter.start()
    assert _listening(doc_id)
    assert cli.main(["register", str(path), "--hook"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    daemon.sender.board.deliver(doc_id, "release")
    waiter.join(5)


def test_open_prefers_wslview_then_cmd_then_explorer(monkeypatch):
    found = {"cmd.exe": "/w/cmd.exe", "explorer.exe": "/w/explorer.exe"}
    monkeypatch.setattr(browser.shutil, "which", lambda name: found.get(name))
    assert [c[0] for c in browser.openers("http://x")] == ["/w/cmd.exe", "/w/explorer.exe"]
    assert browser.openers("http://x")[0][1:] == ["/c", "start", "", "http://x"]
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    with pytest.raises(browser.OpenError):
        browser.open_url("http://x")


def test_register_for_the_hook_takes_only_a_document_of_a_tracked_folder(tmp_path, capsys):
    """The hook hands over every .html; `register --hook` applies the rule."""
    from annotate import folders
    ws = tmp_path / "workspace"
    inside = ws / "repo" / "docs" / "a.html"
    outside = ws / "repo" / "src" / "b.html"
    for path in (inside, outside):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<title>x</title>")
    assert cli.main(["register", str(outside), "--hook"]) == 0
    assert capsys.readouterr().out == "" and registry.all_docs() == {}
    assert cli.main(["register", str(inside), "--hook"]) == 0
    assert [d["path"] for d in registry.all_docs().values()] == [str(inside)]
    folders.add(str(ws))                                   # now: every .html under it
    assert cli.main(["register", str(outside), "--hook"]) == 0
    assert len(registry.all_docs()) == 2
    assert cli.main(["register", str(outside)]) == 0      # by hand: always


def test_folders_and_rescan_from_the_command_line(tmp_path, capsys):
    import os
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "r.html").write_text("<title>R</title>")
    assert cli.main(["folders", "add", str(reports)]) == 0
    assert cli.main(["folders"]) == 0
    listed = capsys.readouterr().out
    assert f"{reports.resolve()}  every .html" in listed and "only under a docs/" in listed
    assert cli.main(["rescan", "--folder", str(reports), "--days", "1"]) == 0
    assert "1 document(s)" in capsys.readouterr().out
    os.utime(reports / "r.html", (1000.0, 1000.0))
    assert cli.main(["folders", "remove", str(reports.resolve())]) == 0
    assert cli.main(["folders", "remove", str(reports.resolve())]) == 1
    assert cli.main(["rescan", "--days", "0"]) == 1
    assert "not a tracked folder" in capsys.readouterr().err
