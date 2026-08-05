"""
Tests for the blockSeparatorTags mechanism in extract.py: a small set
of tags (p, center, h1-h4) that are block-level by default HTML
semantics -- unlike the tags handled by ignoredTags (span, b, i, etc.,
which are genuinely inline, no implied line break at all, confirmed
against real HTML tokenizer/CSS default-display behavior -- see
test_ignored_tags.py).

Stripped the same tag-syntax-removed, content-kept way as ignoredTags,
but via substituteLineBreakTag() with a newline separator rather than
plain deletion. Without this, two adjacent blocks with no whitespace
between them in the source fuse into one run-on string -- the same
class of bug as the earlier br/hr word-merging fix, just for a
different set of tags (confirmed directly: "<p>First.</p><p>Second.</p>"
used to produce "First.Second." as a single line before this fix).

A newline specifically, not just a space, to match how compact()
already treats section/paragraph boundaries elsewhere in this file:
wikitext "==heading==" is only ever recognized when it's on its own
line (section.match(line), applied line-by-line via text.split('\\n'))
-- an HTML heading ends up the same way now, as its own line, not
fused onto the same line as surrounding prose.

div is deliberately NOT covered by this mechanism yet, despite also
being block-level -- it's registered in both ignoredTags and
discardElements, a separate, distinct piece of work. Neither is
blockquote. Scope here is intentionally narrow: p, center, and the
h1-h4 headers only.

Run with:
    python -m unittest tests.test_block_separator_tags -v
or, from the tests/ directory:
    python -m unittest test_block_separator_tags -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class BlockSeparatorTagsTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.Template.parse.cache_clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class BasicSeparationTests(BlockSeparatorTagsTestCase):
    """The tag syntax is removed; the content survives, on its own
    line rather than fused onto whatever's adjacent.
    """

    def test_p_separates_adjacent_paragraphs(self):
        text = "<p>First paragraph.</p><p>Second paragraph.</p>"
        result = self.get_result(text)
        self.assertEqual(result, ["First paragraph.", "Second paragraph."])

    def test_center_separates_from_surrounding_words(self):
        text = "word<center>middle</center>word"
        result = self.get_result(text)
        self.assertEqual(result, ["word", "middle", "word"])

    def test_heading_separates_from_surrounding_prose(self):
        text = "Some text<h3>A Heading</h3>More text"
        result = self.get_result(text)
        self.assertEqual(result, ["Some text", "A Heading", "More text"])

    def test_all_four_heading_levels(self):
        for level in (1, 2, 3, 4):
            with self.subTest(level=level):
                text = f"before<h{level}>Heading {level}</h{level}>after"
                result = self.get_result(text)
                self.assertEqual(result, ["before", f"Heading {level}", "after"])


class BoundaryAwarenessTests(BlockSeparatorTagsTestCase):
    """No spurious separator when one's already there, or when there's
    nothing on that side to separate from at all -- same boundary
    logic already verified for the br/hr case.
    """

    def test_heading_at_very_start_of_text(self):
        text = "<h1>Title</h1>Body text follows."
        result = self.get_result(text)
        self.assertEqual(result, ["Title", "Body text follows."])

    def test_paragraph_at_very_end_of_text(self):
        text = "Body text.<p>Final paragraph.</p>"
        result = self.get_result(text)
        self.assertEqual(result, ["Body text.", "Final paragraph."])

    def test_no_extra_blank_line_when_already_adjacent_to_newline(self):
        text = "Text before.\n<p>Paragraph.</p>\nText after."
        result = self.get_result(text)
        self.assertEqual(result, ["Text before.", "Paragraph.", "Text after."])


class UnaffectedNeighboringMechanismsTests(BlockSeparatorTagsTestCase):
    """Sanity check: this is a distinct mechanism from ignoredTags
    (genuinely inline tags, no separator) and from div's own, separate
    situation (deliberately out of scope here).
    """

    def test_span_still_gets_no_separator(self):
        text = "word<span>middle</span>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])

    def test_div_still_unaffected_deliberately_out_of_scope(self):
        text = "<div>First block.</div><div>Second block.</div>"
        result = self.get_result(text)
        self.assertEqual(result, ["First block.Second block."])


if __name__ == '__main__':
    unittest.main()
