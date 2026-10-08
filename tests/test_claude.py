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


# -- the conversations a document can be attached to (claude.sessions) ---------

def _date(path, when):
    import os
    os.utime(path, (when, when))


def test_the_list_offers_only_resumable_conversations_and_never_a_subagent():
    from fakes import compact, conversation
    projects = claude.projects_dir()
    conversation(projects, "s-real", when=1000.0)
    transcript(projects, "s-title-only", lines=(compact({"type": "ai-title", "aiTitle": "T"}),))
    transcript(projects, "s-empty", lines=("",))
    # A subagent transcript holds real messages: only its place keeps it out.
    conversation(projects, "agent-a1b2", folder="-home-x-repo/s-real/subagents", when=5000.0)
    conversation(projects, "not an id", when=5000.0)
    assert [s["id"] for s in claude.sessions()] == ["s-real"]


def test_open_sessions_come_first_then_the_most_recently_active(tmp_path):
    from fakes import conversation, register_session
    projects = claude.projects_dir()
    conversation(projects, "s-old-open", when=1000.0)
    conversation(projects, "s-newest", when=3000.0)
    conversation(projects, "s-middle", when=2000.0)
    inbox_socket = tmp_path / "s.sock"
    inbox_socket.write_text("")              # only its existence is checked here
    register_session("s-old-open", inbox_socket)
    assert [(s["id"], s["open"]) for s in claude.sessions()] == [
        ("s-old-open", True), ("s-newest", False), ("s-middle", False)]


def test_the_list_is_bounded_and_never_cuts_an_open_session(tmp_path):
    from fakes import compact, conversation, register_session
    projects = claude.projects_dir()
    total = claude.SESSION_LIMIT + 2
    for i in range(total):
        conversation(projects, f"s-{i:03d}", when=1000.0 + i)
    # The newest transcript cannot be resumed: it must not take a place.
    _date(transcript(projects, "s-title-only",
                     lines=(compact({"type": "ai-title", "aiTitle": "T"}),)), 9999.0)
    listed = [s["id"] for s in claude.sessions()]
    assert len(listed) == claude.SESSION_LIMIT
    assert listed[0] == f"s-{total - 1:03d}" and "s-000" not in listed
    inbox_socket = tmp_path / "s.sock"
    inbox_socket.write_text("")
    register_session("s-000", inbox_socket, name="a")    # the two oldest are open
    register_session("s-001", inbox_socket, name="b")
    assert [s["id"] for s in claude.sessions(limit=3)] == [
        "s-001", "s-000", f"s-{total - 1:03d}"]
    assert [s["id"] for s in claude.sessions(limit=1)] == ["s-001", "s-000"], \
        "more sessions are open than the bound: none of them is cut"


def test_each_session_carries_its_title_starting_folder_and_last_activity():
    from fakes import compact, conversation
    path = conversation(claude.projects_dir(), "s-t", cwd="/work/repo",
                        title="Fix the report", when=1234.5)
    with path.open("a") as handle:        # it moved on later: it STARTED in /work/repo
        handle.write(compact({"type": "user", "sessionId": "s-t", "cwd": "/elsewhere",
                              "message": {"role": "user", "content": "go on"}}) + "\n")
    _date(path, 1234.5)
    assert claude.sessions() == [{"id": "s-t", "title": "Fix the report", "cwd": "/work/repo",
                                  "last_active": 1234.5, "open": False}]


def test_a_conversation_in_two_project_folders_is_listed_once_at_its_latest_date():
    from fakes import conversation
    conversation(claude.projects_dir(), "s-dup", folder="-a", when=1000.0)
    conversation(claude.projects_dir(), "s-dup", folder="-b", when=2000.0)
    assert [(s["id"], s["last_active"]) for s in claude.sessions()] == [("s-dup", 2000.0)]
