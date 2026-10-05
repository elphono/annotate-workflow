from __future__ import annotations

import socket
import threading

import pytest

from annotate import browser, cli, config, registry, sender, server
from fakes import FakePopen, ok


def test_config_precedence_env_over_file_over_default(tmp_path, monkeypatch):
    assert config.load_config() == config.Config()
    path = tmp_path / "annotate-config.toml"
    path.write_text('port = 9001\nsend_timeout = 60\nclaude_bin = "/opt/c"\n')
    assert config.load_config() == config.Config(9001, 60, "/opt/c")
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


def test_send_and_status_go_through_the_daemon(make_doc, monkeypatch, capsys):
    cfg = config.Config()
    fake = FakePopen(ok("s-cli"))
    srv = server.make_server(cfg, port=0, send=sender.Sender(cfg, popen=fake))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANNOTATE_PORT", str(srv.port))
    try:
        doc_id = registry.register(make_doc("<p>x</p>"), session="s0")["id"]
        assert cli.main(["send", doc_id]) == 1               # nothing to send: refused
        assert "409" in capsys.readouterr().err
        registry.replace_annotations(doc_id, [{"id": "n1", "selector": "body",
                                               "quote": "x", "note": "y"}])
        assert cli.main(["send", doc_id]) == 0
        assert "sent to the producing session" in capsys.readouterr().out
        for _ in range(100):
            if not srv.sender.running():
                break
            threading.Event().wait(0.05)
        assert cli.main(["status"]) == 0
        assert "1 answered" in capsys.readouterr().out
    finally:
        srv.shutdown()
        srv.server_close()


def test_open_prefers_wslview_then_cmd_then_explorer(monkeypatch):
    found = {"cmd.exe": "/w/cmd.exe", "explorer.exe": "/w/explorer.exe"}
    monkeypatch.setattr(browser.shutil, "which", lambda name: found.get(name))
    assert [c[0] for c in browser.openers("http://x")] == ["/w/cmd.exe", "/w/explorer.exe"]
    assert browser.openers("http://x")[0][1:] == ["/c", "start", "", "http://x"]
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    with pytest.raises(browser.OpenError):
        browser.open_url("http://x")
