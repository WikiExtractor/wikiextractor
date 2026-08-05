"""
Tests for discardElements: base mechanism coverage, plus the
'includeonly' and 'categorytree' additions.

None of the existing discardElements-adjacent test files actually
cover the ordinary base case -- they each test a specific extension or
fix on top of it:
  - test_noinclude_removal.py: noinclude's matched-pair-vs-unmatched-
    tag-fallback behavior specifically.
  - test_orphaned_discard_close_tags.py: the orphaned-closing-tag
    fallback mechanism, tag-agnostic but focused on that one fix.
  - text_discard_element_slash.py: the opening-tag regex fix for '/'
    inside attribute values specifically.
This file covers the foundational behavior none of those assert
directly (BaseDiscardElementsTests below), plus two real additions to
the discardElements list itself (IncludeonlyTests, CategorytreeTests).

BaseDiscardElementsTests covers, using dropNested() + discardElements
directly (see extract.py):
  - an ordinary matched tag pair is discarded, tag and content together
  - TRUE nesting: a tag nested inside itself (dropNested's `nest`
    depth counter) discards the whole outer span as one unit, not just
    up to the first inner closing tag
  - multiple independent (non-nested) instances on the same page are
    each discarded separately
  - attributes on the opening tag don't prevent a match
  - the mechanism generalizes across several different tags in the
    list (div, table, ref, gallery), not just whichever one happens to
    get tested elsewhere
  - case-insensitive tag matching
  - a self-closing form (e.g. <ref name="x" />) is discarded cleanly,
    consuming nothing as "content"
  - a tag that never appears at all leaves everything else untouched

IncludeonlyTests and CategorytreeTests cover two independent gaps,
both found on real pnb.wikipedia.org pages once the <ns> == '0' fix
(see test_colon_in_title.py) started correctly including ns=0 pages
whose title merely looks like it belongs to another namespace,
surfacing pages whose content nobody had previously seen extracted at
all:

1. includeonly (id 38683, "سچہ:·"): the entire visible body was just
   "<includeonly> · </includeonly>". includeonly is the direct
   counterpart of noinclude -- its content is only ever meant to be
   visible when the page is TRANSCLUDED elsewhere, the opposite of
   noinclude, and never on a direct/standalone view, which is the
   only context a regular article is ever extracted in. noinclude was
   already in discardElements (correctly discarding tag and content
   together for a regular article); includeonly had no equivalent
   entry at all, so its tags fell through to generic HTML-escaping
   while its content survived untouched.

2. categorytree (id 40471, "بوآ: چترال"): raw
   "<categorytree>ضلع چترال</categorytree>" markup leaked through
   verbatim (also HTML-escaped, for the same reason). categorytree is
   a MediaWiki extension tag that dynamically renders a live category
   hierarchy from the wiki's own database at view time -- nothing
   meaningful for a static extractor to produce from it, the same
   category as gallery/timeline, which were already handled.

Both are straightforward discardElements additions -- ordinary matched
open/close pairs, not the unmatched-tag edge case test_noinclude_removal.py
covers for noinclude specifically.

Run with:
    python -m unittest tests.test_discard_elements -v
or, from the tests/ directory:
    python -m unittest test_discard_elements -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class DiscardElementsTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.Template.parse.cache_clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class BaseDiscardElementsTests(DiscardElementsTestCase):
    """The ordinary, foundational behavior of discardElements + dropNested()
    -- not specific to any one fix or tag, and not directly asserted by
    any of the three existing discardElements-adjacent test files.

    Deliberately avoids 'div' as an example tag here: div is registered
    in BOTH discardElements and the separate ignoredTags list (which
    drops the tag but KEEPS its content), and ignoredTags' handling
    currently wins in practice -- already a known, separately-flagged
    inconsistency elsewhere in extract.py, not something these tests
    are meant to characterize. table/ref/gallery/caption are cleanly
    discardElements-only and exercise the mechanism unambiguously.
    """

    def test_matched_pair_discarded_with_content(self):
        text = "before<table>discarded</table>after"
        result = self.get_result(text)
        self.assertEqual(result, ["beforeafter"])

    def test_true_nesting_discards_whole_outer_span(self):
        # dropNested()'s whole point is tracking nesting depth (its
        # `nest` counter) -- a tag nested inside itself must discard
        # the entire outer span as one unit, not stop early at the
        # first inner closing tag.
        text = "A<table>B<table>C</table>D</table>E"
        result = self.get_result(text)
        self.assertEqual(result, ["AE"])
        for leftover in ("B", "C", "D"):
            self.assertNotIn(leftover, result[0])

    def test_multiple_independent_instances_each_discarded(self):
        text = "X<table>1</table>Y<table>2</table>Z"
        result = self.get_result(text)
        self.assertEqual(result, ["XYZ"])

    def test_attributes_on_opening_tag_dont_prevent_match(self):
        text = 'before<table class="foo" style="color:red">discarded</table>after'
        result = self.get_result(text)
        self.assertEqual(result, ["beforeafter"])

    def test_mechanism_generalizes_across_different_tags(self):
        for tag in ('table', 'ref', 'gallery', 'caption'):
            with self.subTest(tag=tag):
                text = f"before<{tag}>discarded</{tag}>after"
                result = self.get_result(text)
                self.assertEqual(result, ["beforeafter"])

    def test_case_insensitive_tag_matching(self):
        text = "Before.<TABLE>discard me</TABLE>After."
        result = self.get_result(text)
        self.assertEqual(result, ["Before.After."])

    def test_self_closing_form_discarded_cleanly(self):
        # A self-closing discardElements tag (e.g. a bare <ref .../>
        # with no separate closing tag) should vanish entirely,
        # consuming nothing as "content" and without confusing the
        # matched-pair search into treating it as an opener awaiting a
        # distant closer elsewhere in the text.
        text = 'before<ref name="x" />after'
        result = self.get_result(text)
        self.assertEqual(result, ["beforeafter"])

    def test_no_match_leaves_everything_untouched(self):
        text = "Just ordinary prose, no discardElements tag anywhere."
        result = self.get_result(text)
        self.assertEqual(result, [text])


class IncludeonlyTests(DiscardElementsTestCase):

    def test_real_pnb_article_shape(self):
        # Reconstruction of the real case: an article whose entire
        # body is just includeonly-wrapped content. Should extract to
        # nothing, not leak the tag text with the content intact.
        text = "<includeonly> · </includeonly>"
        result = self.get_result(text)
        self.assertEqual(result, [])

    def test_content_discarded_alongside_real_prose(self):
        text = "Real content before.<includeonly>hidden on direct view</includeonly>Real content after."
        result = self.get_result(text)
        self.assertEqual(result, ["Real content before.Real content after."])
        self.assertNotIn("hidden on direct view", result[0] if result else "")

    def test_case_insensitive(self):
        text = "Before.<IncludeOnly>discard me</IncludeOnly>After."
        result = self.get_result(text)
        self.assertEqual(result, ["Before.After."])


class CategorytreeTests(DiscardElementsTestCase):

    def test_real_pnb_article_shape(self):
        # Reconstruction of the real case: the categorytree tag and
        # its content are discarded, but the OTHER (unrelated, already
        # malformed in the source) fragments on neighboring lines are
        # left alone -- this fix only addresses the categorytree leak
        # specifically, not other pre-existing wikitext issues.
        text = ("[ تازہ انتخابات ویکھو]\n"
                "<categorytree>ضلع چترال</categorytree>\n"
                " کیہ نيں؟ | |")
        result = self.get_result(text)
        full = '\n'.join(result)
        self.assertNotIn("categorytree", full)
        self.assertNotIn("ضلع چترال", full)
        self.assertIn("تازہ انتخابات ویکھو", full)
        self.assertIn("کیہ نيں", full)

    def test_case_insensitive(self):
        text = "Before.<CategoryTree>Some Category</CategoryTree>After."
        result = self.get_result(text)
        self.assertEqual(result, ["Before.After."])


if __name__ == '__main__':
    unittest.main()
