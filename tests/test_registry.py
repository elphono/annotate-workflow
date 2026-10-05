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
