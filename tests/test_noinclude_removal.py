"""Tests for <noinclude> handling in extract.py, covering two distinct
mechanisms:

1. The ORIGINAL processing (discardElements + dropNested, unchanged by
   this fix): a genuinely matched <noinclude>...</noinclude> pair --
   opening tag before closing tag, within the same page's text -- is
   discarded entirely, tags AND content together. This is the correct,
   pre-existing behavior for how noinclude is actually meant to work
   when it appears (whether directly, or via template transclusion).

2. The NEW fallback (added by this fix): a genuinely UNMATCHED
   <noinclude> or </noinclude> -- one dropNested's open-then-close
   pairing can never resolve -- has its literal tag text stripped,
   while everything else is left untouched. Found on a real PNB
   Wikipedia article ("اربیم"/Erbium, id 113): the article's raw
   wikitext had a closing </noinclude> appearing BEFORE its "opening"
   counterpart -- almost certainly periodic-table navbox wikitext
   copy-pasted directly from a template page, dragging the template's
   own noinclude wrapper along in the wrong relative order. Since the
   tags are structurally out of order within that one article, no
   pairing logic could ever match them to each other. Rather than
   guess at what content was "supposed" to be discarded (which the
   malformed source gives no reliable way to determine), the fallback
   only strips the literal tag syntax itself, leaving all surrounding
   content -- including the tag's own now-orphaned partner -- intact.

Verified directly that a naive raw XML dump diff of this same article
showed single-escaped "&lt;/noinclude&gt;", confirming the source
genuinely contains a real (if misordered) tag, not literal typed-out
entity text.

Run with:
    python -m unittest tests.test_noinclude_removal -v
or, from the tests/ directory:
    python -m unittest test_noinclude_removal -v

"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class NoincludeTestCase(unittest.TestCase):

    def setUp(self):
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        self.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text], templatePrefix=self.templatePrefix)
        return extractor.clean_text(article_text, expand_templates=True)


class MatchedPairOriginalProcessingTests(NoincludeTestCase):
    """The original, pre-existing mechanism: a properly matched pair
    (open before close, both present) is discarded entirely -- tags
    AND wrapped content together. Unaffected by this fix; these
    confirm it still works exactly as before.
    """

    def test_matched_pair_discarded_entirely(self):
        text = "Real content before.<noinclude>[[Category:Something]]</noinclude>Real content after."
        result = self.get_result(text)
        self.assertEqual(result, ["Real content before.Real content after."])

    def test_matched_pair_wrapping_real_prose_also_discarded(self):
        # Unlike the fallback case, a genuinely matched pair discards
        # its content even if that content looks like meaningful
        # prose -- this is the deliberate, existing behavior for a
        # real, well-formed noinclude pair.
        text = "Before.<noinclude>This text should not survive.</noinclude>After."
        result = self.get_result(text)
        self.assertEqual(result, ["Before.After."])
        self.assertNotIn("should not survive", result[0])

    def test_multiple_matched_pairs_each_discarded(self):
        text = "A<noinclude>one</noinclude>B<noinclude>two</noinclude>C"
        result = self.get_result(text)
        self.assertEqual(result, ["ABC"])

    def test_matched_pair_case_insensitive(self):
        text = "Before.<NoInclude>discard me</NoInclude>After."
        result = self.get_result(text)
        self.assertEqual(result, ["Before.After."])


class UnmatchedTagFallbackTests(NoincludeTestCase):
    """The new fix: a genuinely unmatched <noinclude> or </noinclude>
    has only its literal tag syntax stripped -- surrounding content,
    including real prose that would otherwise have been "wrapped" by
    the orphaned tag, is left untouched rather than guessed at.
    """

    def test_real_erbium_article_shape(self):
        # Reconstruction of the real bug: closing tag appears BEFORE
        # its "opening" counterpart within the same article, so they
        # can never be matched to each other as a pair no matter how
        # the pairing logic works. All real content on both sides,
        # including the periodic-table-style content and the final
        # category line, must survive.
        text = ("Infobox content here.\n"
                "</noinclude>Real navbox content that must survive.\n"
                "More real content.<noinclude>\n"
                "Category line content that must survive.")
        result = self.get_result(text)
        full = '\n'.join(result)
        self.assertNotIn("&lt;noinclude", full)
        self.assertNotIn("&lt;/noinclude&gt;", full)
        self.assertIn("Infobox content here.", full)
        self.assertIn("Real navbox content that must survive.", full)
        self.assertIn("More real content.", full)
        self.assertIn("Category line content that must survive.", full)

    def test_orphaned_opening_tag_no_close_anywhere(self):
        text = "Real content before.<noinclude>Real content after, never closed."
        result = self.get_result(text)
        self.assertEqual(result, ["Real content before.Real content after, never closed."])

    def test_orphaned_closing_tag_no_open_before_it(self):
        text = "Real content before, never opened.</noinclude>Real content after."
        result = self.get_result(text)
        self.assertEqual(result, ["Real content before, never opened.Real content after."])

    def test_self_closing_form_stripped(self):
        text = "word<noinclude/>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_case_insensitive_and_whitespace_variants(self):
        cases = [
            "word<NOINCLUDE>word",
            "word< noinclude >word",
            "word<noinclude >word",
        ]
        for text in cases:
            with self.subTest(text=text):
                result = self.get_result(text)
                self.assertEqual(result, ["wordword"])

    def test_no_space_substituted_unlike_br(self):
        # Unlike br/hr (which substitute a space to avoid merging
        # words), noinclude carries no line-break semantics -- pure
        # deletion of the tag syntax is the correct, deliberate choice
        # here, consistent with how nobr/ref/templatestyles are
        # already treated.
        text = "word<noinclude>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


class OtherDiscardElementsTagsUnaffectedTests(NoincludeTestCase):
    """Sanity check: the fallback stripping is specific to noinclude
    only. Other discardElements tags (e.g. gallery) don't get this
    treatment -- an unmatched one is simply left as no-op by
    dropNested, same as always.
    """

    def test_unmatched_gallery_tag_not_specially_stripped(self):
        text = "word<gallery>word, never closed"
        result = self.get_result(text)
        # gallery is NOT given noinclude's fallback treatment -- its
        # literal tag text survives untouched (HTML-escaped), exactly
        # as discardElements alone has always handled an unmatched tag.
        self.assertIn("gallery", result[0])


if __name__ == '__main__':
    unittest.main()
