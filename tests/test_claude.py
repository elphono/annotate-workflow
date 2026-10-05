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
