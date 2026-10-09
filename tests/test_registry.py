from __future__ import annotations

import json

import pytest

from annotate import config, registry


def note(id_: str, text: str = "fix this", **extra):
    return {"id": id_, "selector": "body > p:nth-of-type(1)", "quote": "First",
            "note": text, "offset_x": 3, "offset_y": 4, **extra}


def test_register_is_idempotent_on_the_path(make_doc):
    path = make_doc("<p>First</p>")
    a = registry.register(path, session="s1")
    b = registry.register(path)
    assert a["id"] == b["id"] and len(a["id"]) == 8
    assert b["session_id"] == "s1"           # None keeps the session
    c = registry.register(path, session="")
    assert c["session_id"] == ""             # "" clears it
    assert list(registry.all_docs()) == [a["id"]]
    assert a["title"] == "Test doc"
    assert a["cwd"] == str(path.parent.parent)   # the enclosing git repository
    assert a["status"] == "new"


def test_a_write_after_a_delivery_is_the_answer(make_doc):
    """The hook re-registers on every write: that is how `answered` happens."""
    path = make_doc("<p>First</p>")
    doc_id = registry.register(path)["id"]
    assert registry.register(path)["status"] == "new"
    registry.replace_annotations(doc_id, [note("n1")])
    assert registry.get(doc_id)["status"] == "annotated"
    assert registry.register(path)["status"] == "annotated"       # notes still wait
    registry.mark_sent(doc_id, ["n1"], "T1")
    registry.update(doc_id, status="delivered")
    assert registry.register(path, session="s2")["status"] == "answered"
    assert registry.register(path)["status"] == "answered"        # later writes too
    registry.replace_annotations(doc_id, [note("n1"), note("n2")])
    assert registry.register(path)["status"] == "annotated"       # new notes win


def test_the_words_under_the_cursor_are_kept(make_doc):
    doc_id = registry.register(make_doc("<p>First</p>"))["id"]
    [item] = registry.replace_annotations(doc_id, [note("n1", at="[[First]]")])
    assert item["at"] == "[[First]]"
    assert registry.replace_annotations(doc_id, [note("n1")])[0]["at"] == ""


def test_annotations_never_live_next_to_the_document(make_doc):
    path = make_doc("<p>First</p>")
    doc_id = registry.register(path)["id"]
    registry.replace_annotations(doc_id, [note("n1")])
    assert sorted(p.name for p in path.parent.iterdir()) == ["doc.html"]
    stored = config.data_dir() / "annotations" / f"{doc_id}.json"
    assert json.loads(stored.read_text())["annotations"][0]["note"] == "fix this"


def test_a_stale_browser_copy_cannot_unsend_a_note(make_doc):
    doc_id = registry.register(make_doc("<p>First</p>"))["id"]
    registry.replace_annotations(doc_id, [note("n1")])
    registry.mark_sent(doc_id, ["n1"], "T1")
    # The browser still holds n1 without sent_at, and adds n2.
    items = registry.replace_annotations(
        doc_id, [note("n1", sent_at=""), note("n2", "second")])
    assert [(a["id"], a["sent_at"]) for a in items] == [("n1", "T1"), ("n2", "")]
    assert [a["id"] for a in registry.pending(items)] == ["n2"]


def test_unmark_sent_restores_exactly_one_batch(make_doc):
    doc_id = registry.register(make_doc("<p>First</p>"))["id"]
    registry.replace_annotations(doc_id, [note("n1"), note("n2"), note("n3")])
    registry.mark_sent(doc_id, ["n1"], "T1")
    registry.mark_sent(doc_id, ["n2", "n3"], "T2")
    assert registry.unmark_sent(doc_id, "T2") == 2
    assert [a["sent_at"] for a in registry.list_annotations(doc_id)] == ["T1", "", ""]


@pytest.mark.parametrize("bad", [
    {"not": "a list"},
    [{"id": "", "note": "x"}],
    [{"id": "a b", "note": "x"}],
    [{"id": "n1", "note": 3}],
    [{"id": "n1", "offset_x": "3"}],
    [note("n1"), note("n1")],
])
def test_invalid_annotation_lists_are_refused(make_doc, bad):
    doc_id = registry.register(make_doc("<p>First</p>"))["id"]
    with pytest.raises(registry.InvalidAnnotations):
        registry.replace_annotations(doc_id, bad)


def test_forget_removes_entry_annotations_and_optionally_the_file(make_doc):
    keep = make_doc("<p>a</p>", name="keep.html")
    drop = make_doc("<p>b</p>", name="drop.html")
    k = registry.register(keep)["id"]
    d = registry.register(drop)["id"]
    registry.replace_annotations(k, [note("n1")])
    registry.forget(k)
    registry.forget(d, delete_file=True)
    assert registry.all_docs() == {}
    assert keep.exists() and not drop.exists()
    assert not (config.data_dir() / "annotations" / f"{k}.json").exists()
    with pytest.raises(registry.UnknownDocument):
        registry.forget(k)


def test_writes_are_atomic_and_leave_no_temporary(make_doc):
    doc_id = registry.register(make_doc("<p>a</p>"))["id"]
    registry.replace_annotations(doc_id, [note("n1")])
    leftovers = [p.name for p in config.data_dir().rglob("*.tmp")]
    assert leftovers == []


def test_unknown_or_malformed_ids_are_refused():
    with pytest.raises(registry.UnknownDocument):
        registry.get("deadbeef")
    with pytest.raises(registry.UnknownDocument):
        registry.annotations_path("../etc")


def test_documents_are_grouped_by_session_newest_group_first(make_doc, tmp_path):
    root = tmp_path / "workspace"
    a = registry.register(make_doc("<p>a</p>", name="a.html"), session="s-old",
                          cwd=root / "repo")
    b = registry.register(make_doc("<p>b</p>", name="b.html"), session="")
    c = registry.register(make_doc("<p>c</p>", name="c.html"), session="s-new",
                          cwd=root / "repo")
    d = registry.register(make_doc("<p>d</p>", name="d.html"), session="s-old",
                          cwd=root / "repo")
    # Distinct dates: registered in the same second, they would tie, and the
    # order would prove nothing.
    for rank, entry in enumerate((a, b, c, d)):
        registry.update(entry["id"], registered_at=f"2026-10-05T10:0{rank}:00+00:00")
    titles = {"s-new": "Timeline du déploiement"}
    groups = registry.group_by_session(registry.summary(), lambda s: titles.get(s, ""), root)
    assert [g["session_id"] for g in groups] == ["s-old", "s-new", ""]
    assert groups[0]["docs"] == [d["id"], a["id"]]
    assert groups[1]["label"] == "Timeline du déploiement · repo"
    assert groups[0]["label"] == "session s-old · repo"         # no title known
    assert groups[2]["label"] == "documents without a known session"
    assert groups[2]["docs"] == [b["id"]] and c["id"] in groups[1]["docs"]


def test_each_session_group_names_the_repository_it_works_in(make_doc, tmp_path):
    root = tmp_path / "workspace"
    repo = root / "team" / "backend"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    linked = root / "linked"                               # a git worktree: .git is a file
    linked.mkdir()
    (linked / ".git").write_text("gitdir: elsewhere")
    plain = root / "team"                                  # holds a repo, is not one
    sessions = {"s-root": repo, "s-sub": repo / "sub", "s-linked": linked, "s-plain": plain}
    for session, cwd in sessions.items():
        registry.register(make_doc("<p>x</p>", name=f"{session}.html"), session=session, cwd=cwd)
    registry.register(make_doc("<p>y</p>", name="none.html"), session="")
    groups = {g["session_id"]: g for g in
              registry.group_by_session(registry.summary(), lambda s: "", root)}
    assert groups["s-root"]["repo"] == groups["s-sub"]["repo"] == str(repo)
    assert groups["s-root"]["repo_label"] == "team/backend" and groups["s-root"]["is_repo"]
    assert groups["s-sub"]["folder"] == "team/backend/sub"     # the session's own folder stays
    assert groups["s-linked"]["repo"] == str(linked) and groups["s-linked"]["is_repo"]
    assert groups["s-plain"]["repo"] == str(plain) and not groups["s-plain"]["is_repo"]
    assert groups["s-plain"]["repo_label"] == "team"
    assert groups[""]["repo"] == "" and not groups[""]["is_repo"]


def test_a_repository_outside_the_workspace_is_labelled_by_its_name(make_doc, tmp_path):
    repo = tmp_path / "elsewhere" / "tool"
    (repo / ".git").mkdir(parents=True)
    registry.register(make_doc("<p>x</p>"), session="s1", cwd=repo)
    group = registry.group_by_session(registry.summary(), lambda s: "", tmp_path / "ws")[0]
    assert group["repo"] == str(repo) and group["repo_label"] == "tool"


def test_a_repository_in_the_home_directory_holds_no_session(make_doc, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".git").mkdir()                            # a dotfiles repository
    notes = tmp_path / "notes"
    notes.mkdir()
    registry.register(make_doc("<p>x</p>"), session="s1", cwd=notes)
    group = registry.group_by_session(registry.summary(), lambda s: "", tmp_path / "ws")[0]
    assert group["repo"] == str(notes) and not group["is_repo"]


def test_an_agent_worktree_is_never_kept_as_the_session_folder(make_doc, tmp_path):
    path = make_doc("<p>x</p>")
    repo = path.parent.parent
    gone = repo / ".claude" / "worktrees" / "agent-a90e957b"
    entry = registry.register(path, session="s1", cwd=gone)
    assert entry["cwd"] == str(repo)                      # the repo holding the worktree
    gone.mkdir(parents=True)                              # even while it still exists
    assert registry.register(path, cwd=gone)["cwd"] == str(repo)
    assert registry.usable_cwd(tmp_path / "vanished", path) == repo
    elsewhere = tmp_path / "other-repo"
    elsewhere.mkdir()
    assert registry.usable_cwd(elsewhere, path) == elsewhere


def test_an_unmanaged_document_is_remembered_with_its_date_until_written_again(make_doc):
    import os
    path = make_doc("<p>x</p>")
    os.utime(path, (5000.0, 5000.0))
    registry.forget(registry.register(path)["id"])
    assert registry.dismissed(path, 5000.0) and not registry.dismissed(path, 5001.0)
    registry.register(path)                         # written again: the hook took it
    assert not registry.dismissed(path, 5000.0)
