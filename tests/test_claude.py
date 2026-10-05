"""`claude.resumable`: the criterion measured in remarkable-sync (2026-09-15)."""
from __future__ import annotations

from annotate import claude
from fakes import transcript


def test_a_transcript_with_a_message_is_resumable_from_any_project_folder():
    transcript(claude.projects_dir(), "abc12345-0000", folder="-some-other-repo")
    assert claude.resumable("abc12345-0000")


def test_no_transcript_an_empty_one_or_a_title_only_one_is_not():
    assert not claude.resumable("abc12345-0001")
    transcript(claude.projects_dir(), "abc12345-0002", lines=("",))
    transcript(claude.projects_dir(), "abc12345-0003",
               lines=('{"type":"summary","summary":"t"}',))
    assert not claude.resumable("abc12345-0002")
    assert not claude.resumable("abc12345-0003")


def test_only_the_compact_form_counts():
    """`claude --resume` refused lines rewritten with spaces (measured)."""
    transcript(claude.projects_dir(), "abc12345-0004",
               lines=('{"type": "user", "message": "hi"}',))
    assert not claude.resumable("abc12345-0004")


def test_a_malformed_id_is_never_looked_up():
    transcript(claude.projects_dir(), "abc12345-0005")
    assert not claude.resumable("")
    assert not claude.resumable("../abc12345-0005")
    assert not claude.resumable("*")


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def test_the_writer_is_the_last_call_naming_the_file_before_it_was_written(tmp_path):
    import os
    from fakes import tool_call
    doc = tmp_path / "docs" / "timeline.html"
    doc.parent.mkdir()
    doc.write_text("x")
    os.utime(doc, (5000.0, 5000.0))
    transcript(claude.projects_dir(), "early-one", lines=(
        tool_call("early-one", _iso(4000.0), "python gen.py > docs/timeline.html", "/a"),))
    transcript(claude.projects_dir(), "the-writer", folder="-p/the-writer/subagents",
               lines=(tool_call("the-writer", _iso(4998.0), "cp /tmp/x/timeline.html docs/", "/b"),))
    transcript(claude.projects_dir(), "a-reader", lines=(
        tool_call("a-reader", _iso(5100.0), "ls -l docs/timeline.html", "/c"),))
    assert claude.writer_of(doc, 5000.0) == ("the-writer", "/b")


def test_no_call_naming_the_file_means_no_writer(tmp_path):
    from fakes import tool_call
    doc = tmp_path / "nobody.html"
    doc.write_text("x")
    transcript(claude.projects_dir(), "s-other", lines=(
        tool_call("s-other", _iso(4999.0), "cp a.html b.html", "/a"),))
    assert claude.writer_of(doc, 5000.0) is None


def test_the_rename_title_wins_and_the_latest_title_is_read_incrementally():
    from fakes import compact
    path = transcript(claude.projects_dir(), "titled-1", lines=(
        compact({"type": "ai-title", "aiTitle": "First guess", "sessionId": "titled-1"}),))
    assert claude.title("titled-1") == "First guess"
    with path.open("a") as handle:
        handle.write(compact({"type": "ai-title", "aiTitle": "Better guess"}) + "\n")
        handle.write('{"type":"custom-title","customTi')          # still being written
    assert claude.title("titled-1") == "Better guess"
    with path.open("a") as handle:
        handle.write('tle":"V1.7.0"}\n')
        handle.write(compact({"type": "ai-title", "aiTitle": "Even later"}) + "\n")
    assert claude.title("titled-1") == "V1.7.0"
    assert claude.title("no-such-session") == ""
