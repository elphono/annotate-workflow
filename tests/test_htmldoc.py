from __future__ import annotations

from annotate import htmldoc


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>  The   study </title></head><body>
<h1>Intro</h1>
<p>First paragraph.
<p>Second paragraph, never closed.
<section id="flow"><h2>The flow</h2>
  <p>Inside the flow.</p>
  <table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>
  <ul><li>one<li>two<li>three</ul>
  <svg viewBox="0 0 10 10"><rect x="1"/><rect x="2"/><text>label</text></svg>
  <p>After the figure.</p>
</section>
<h3>Late</h3><div><span>deep</span></div>
</body></html>"""


def _tag(node):
    return node.tag if node else None


def test_title_is_collapsed_and_falls_back_to_the_name():
    assert htmldoc.title_of(PAGE, "x.html") == "The study"
    assert htmldoc.title_of("<p>no title</p>", "x.html") == "x.html"


def test_resolve_follows_the_selector_grammar_of_the_overlay():
    doc = htmldoc.parse(PAGE)
    # Unclosed <p>: the browser closes the first when the second opens, so
    # the second paragraph is p:nth-of-type(2), a SIBLING of the first.
    second = htmldoc.resolve(doc, "body > p:nth-of-type(2)")
    assert second is not None and second.parent.tag == "body"
    # The id shortcut, then the implied <tbody> browsers insert.
    cell = htmldoc.resolve(
        doc, "#flow > table:nth-of-type(1) > tbody:nth-of-type(1) > "
             "tr:nth-of-type(2) > td:nth-of-type(1)")
    assert _tag(cell) == "td"
    # Unclosed <li>: three siblings.
    assert _tag(htmldoc.resolve(doc, "#flow > ul:nth-of-type(1) > li:nth-of-type(3)")) == "li"
    # Self-closed SVG children do not swallow their siblings.
    assert _tag(htmldoc.resolve(doc, "#flow > svg:nth-of-type(1) > text:nth-of-type(1)")) == "text"
    assert _tag(htmldoc.resolve(doc, "body > div:nth-of-type(1) > span:nth-of-type(1)")) == "span"


def test_resolve_returns_none_when_the_element_is_gone():
    doc = htmldoc.parse(PAGE)
    assert htmldoc.resolve(doc, "#nowhere > p:nth-of-type(1)") is None
    assert htmldoc.resolve(doc, "body > p:nth-of-type(9)") is None
    assert htmldoc.resolve(doc, "body > p:first-child") is None
    assert htmldoc.resolve(doc, "") is None
    assert htmldoc.resolve(doc, "div.x") is None


def test_heading_before_is_the_nearest_preceding_heading():
    doc = htmldoc.parse(PAGE)
    first = htmldoc.resolve(doc, "body > p:nth-of-type(1)")
    inner = htmldoc.resolve(doc, "#flow > p:nth-of-type(2)")
    deep = htmldoc.resolve(doc, "body > div:nth-of-type(1) > span:nth-of-type(1)")
    assert htmldoc.heading_before(doc, first) == "Intro"
    assert htmldoc.heading_before(doc, inner) == "The flow"
    assert htmldoc.heading_before(doc, deep) == "Late"
    assert htmldoc.heading_before(doc, htmldoc.parse("<p>x</p>").by_order[0]) == ""


def test_css_escaped_ids_resolve():
    doc = htmldoc.parse('<body><div id="a.b"><p>x</p></div><div id="1st">y</div></body>')
    assert _tag(htmldoc.resolve(doc, r"#a\.b > p:nth-of-type(1)")) == "p"
    assert _tag(htmldoc.resolve(doc, r"#\31 st")) == "div"


def test_inject_overlay_adds_base_and_script_once():
    out = htmldoc.inject_overlay(PAGE, "abc123")
    assert out.count('<base href="/docs/abc123/files/">') == 1
    assert out.index("<base") < out.index("<title>")
    assert out.count('src="/static/annotate.js"') == 1
    assert 'data-annotate-doc="abc123"' in out
    assert out.index("annotate.js") < out.lower().rindex("</body>")


def test_inject_overlay_respects_an_existing_base_and_a_missing_body():
    own = '<html><head><base href="https://x/"></head><body>x</body></html>'
    assert htmldoc.inject_overlay(own, "d1").count("<base") == 1
    bare = htmldoc.inject_overlay("<p>fragment</p>", "d1")
    assert bare.startswith('<base href="/docs/d1/files/">')
    assert bare.rstrip().endswith("</script>")
    # <header> must not be mistaken for <head>.
    tricky = htmldoc.inject_overlay("<html><body><header>h</header></body></html>", "d1")
    assert tricky.index("<base") < tricky.index("<body>")


def test_svg_titles_are_not_the_document_title():
    page = ("<html><head><title>Real title</title></head><body>"
            "<svg><title>Figure one</title></svg><svg><title>Figure two</title></svg>"
            "</body></html>")
    assert htmldoc.title_of(page, "x.html") == "Real title"
    only_svg = "<body><svg><title>Figure</title></svg></body>"
    assert htmldoc.title_of(only_svg, "x.html") == "x.html"
