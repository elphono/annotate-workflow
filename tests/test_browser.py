"""The overlay in a real browser (headless Chromium through Playwright).

Excluded from the default run, like remarkable-sync's `device` tests: it needs
Node and a Playwright install the repository does not ship. Run it with

    ANNOTATE_PLAYWRIGHT_DIR=<dir whose node_modules holds playwright> \\
        uv run pytest -m browser -v

Without that variable, it FAILS rather than skips: a browser run that skips
leaves a green line having seen nothing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from annotate import config, registry, sender, server
from fakes import RecordingOpener

SCRIPT = Path(__file__).resolve().parent / "browser" / "overlay_check.mjs"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
       b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.mark.browser
def test_overlay_in_a_real_browser(tmp_path, monkeypatch, make_doc):
    pw_dir = os.environ.get("ANNOTATE_PLAYWRIGHT_DIR", "")
    node = shutil.which("node")
    assert pw_dir and node, ("set ANNOTATE_PLAYWRIGHT_DIR to a directory whose "
                             "node_modules holds playwright, and put node on the PATH")
    path = make_doc(
        "<h1>Overlay check</h1><nav><a href='#second'>to the second part</a></nav>"
        "<div id='body-text'><p>First paragraph, a long enough line of text.</p>"
        "<p>Second paragraph, which the test annotates with Alt+click.</p></div>"
        "<img src='figures/dot.png' alt='dot'>"
        "<svg id='fig' width='400' height='120' viewBox='0 0 400 120'>"
        "<rect x='10' y='10' width='120' height='60' fill='#ddd'/>"
        "<text x='20' y='45'>Session</text>"
        "<rect x='250' y='10' width='120' height='60' fill='#ddd'/>"
        "<text x='260' y='45'>Browser</text></svg>"
        "<div style='height:1200px'></div><h2 id='second'>Second part</h2><p>End.</p>",
        title="Overlay check")
    (path.parent / "figures").mkdir()
    (path.parent / "figures" / "dot.png").write_bytes(PNG)
    doc_id = registry.register(path, session="sess-browser")["id"]
    registry.replace_annotations(doc_id, [{
        "id": "orphan1", "number": 1, "selector": "#deleted-section > p:nth-of-type(3)",
        "quote": "A sentence that was deleted", "note": "Keep this idea somewhere",
        "offset_x": 0, "offset_y": 0}])

    cfg = config.load_config()
    opener = RecordingOpener()
    srv = server.make_server(cfg, port=0, send=sender.Sender(cfg, opener=opener))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # An open session listens, as `annotate wait` would make it.
    box: dict[str, object] = {}
    listener = threading.Thread(target=lambda: box.update(
        waiter=srv.sender.board.wait(doc_id, "sess-open", "/", lambda: True, 110)),
        daemon=True)
    listener.start()
    out = Path(os.environ.get("ANNOTATE_BROWSER_OUT", tmp_path))
    out.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run([node, str(SCRIPT), pw_dir, f"http://localhost:{srv.port}",
                               doc_id, str(out), str(path)],
                              capture_output=True, text=True, timeout=120)
    finally:
        srv.shutdown()
        srv.server_close()
    print(done.stdout)
    assert done.returncode == 0, done.stdout + done.stderr

    listener.join(5)
    items = {a["id"]: a for a in registry.list_annotations(doc_id)}
    assert items["orphan1"]["note"] == "Keep this idea somewhere"   # never dropped
    by_number = {a["number"]: a for a in items.values()}
    para, fig = by_number[2], by_number[3]
    assert para["selector"] == "#body-text > p:nth-of-type(2)"
    assert para["quote"].startswith("Second paragraph, which the test annotates")
    assert para["note"] == "Rephrase this, it is unclear." and para["sent_at"]
    # The words under the cursor, not the whole paragraph (user note 2, 2026-10-05).
    assert para["at"].startswith("[[Second]] paragraph, which the test")
    assert para["at"].endswith(" …") and len(para["at"]) < 100
    assert fig["selector"] == "#fig" and fig["at"] == 'in the figure, next to the label "Browser"'
    prompt = box["waiter"].prompt  # type: ignore[union-attr]
    assert "Rephrase this, it is unclear." in prompt and "[[Second]]" in prompt
    assert "Keep this idea somewhere" in prompt and "Anchor: lost" in prompt
    assert "Section: Overlay check" in prompt
    assert opener.calls == [], "a session listened: no terminal tab"
