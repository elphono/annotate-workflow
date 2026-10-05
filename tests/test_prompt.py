from __future__ import annotations

import pytest

from annotate import prompt, registry

BODY = "<h1>Intro</h1><p>Alpha text.</p><h2>Second part</h2><p>Beta text.</p>"


def item(id_, selector, quote, note_text, number=1):
    return {"id": id_, "selector": selector, "quote": quote, "note": note_text,
            "number": number, "offset_x": 0.0, "offset_y": 0.0, "sent_at": ""}


def test_each_note_carries_section_quote_and_text(make_doc):
    entry = registry.register(make_doc(BODY))
    text = prompt.build(entry, [
        item("a", "body > p:nth-of-type(1)", "Alpha text.", "shorten this"),
        item("b", "body > p:nth-of-type(2)", "Beta   text.", "add a figure", 2),
    ])
    first, second = text.split("--- Note 1")[1].split("--- Note 2")
    assert "Section: Intro" in first and '"Alpha text."' in first
    assert "shorten this" in first
    assert "Section: Second part" in second and '"Beta text."' in second
    assert "add a figure" in second
    assert entry["path"] in text and '"Test doc"' in text
    assert "edit the file in place" in text and "self-contained HTML" in text
    assert "read it first" not in text
    assert "Clicked on" not in text                 # nothing captured, nothing said
    assert text.rstrip().endswith(f"annotate wait {entry['id']}`")

def test_the_words_under_the_cursor_go_with_the_note(make_doc):
    entry = registry.register(make_doc(BODY))
    pinned = {**item("a", "body > p:nth-of-type(1)", "Alpha text.", "this word"),
              "at": "[[Alpha]] text."}
    text = prompt.build(entry, [pinned])
    assert 'Clicked on (the word between [[ ]]): "[[Alpha]] text."' in text


def test_a_lost_anchor_is_still_sent_with_its_quote(make_doc):
    entry = registry.register(make_doc(BODY))
    text = prompt.build(entry, [
        item("a", "#removed-section > p:nth-of-type(4)", "The old sentence", "keep the idea"),
    ])
    assert "Anchor: lost" in text
    assert '"The old sentence"' in text and "keep the idea" in text


def test_fresh_sessions_get_the_read_it_first_preamble(make_doc):
    entry = registry.register(make_doc(BODY))
    text = prompt.build(entry, [item("a", "body", "", "x")], fresh=True)
    assert text.startswith("You did not produce this document in this conversation")
    assert "Quoted passage: (none captured)" in text


def test_nothing_to_send_is_an_error(make_doc):
    entry = registry.register(make_doc(BODY))
    with pytest.raises(prompt.PromptError):
        prompt.build(entry, [])
