"""
Tests for two related br/hr fixes:

1. Missing-trailing-slash matching (selfClosing_tag_patterns /
   lineBreak_tag_patterns): old-style HTML4 syntax like <br clear=all>
   (no trailing slash) previously never matched at all, surviving into
   extracted text HTML-escaped as literal "&lt;br clear=all&gt;". A
   bare <br> with no attributes had exactly the same problem.

2. Word-merging on deletion (this file's main focus): even where a
   br/hr tag WAS correctly matched, it was deleted with nothing
   substituted in its place (dropSpans() just concatenates the text on
   either side of a removed span). If there was no surrounding
   whitespace in the source -- a real, confirmed case on Saraiki
   Wikipedia, "اُٹھا<br>رب" with no spaces at all around the tag --
   deleting it merges two separate words into one ("اُٹھارب").

The fix: br/hr are pulled out into their own lineBreakTags group,
matched with the same permissive (optional trailing slash) pattern as
before, but substituted with a single space instead of being folded
into the generic drop-with-nothing spans mechanism.

nobr/ref/references/nowiki are NOT affected by the space-substitution
part of this fix (see RefTagSafetyTests / NobrTagTests below):
"no line break" doesn't call for inserting a space where the tag was,
and ref's self-closing form has a distinct, real meaning from its
paired form (see test_br_tag_handling's sibling investigation) that
must not be disturbed.

Run with:
    python -m unittest tests.test_br_tag_handling -v
or, from the tests/ directory:
    python -m unittest test_br_tag_handling -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class VoidElementTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class WordMergingRegressionTests(VoidElementTestCase):
    """The core bug this fix addresses: br/hr with no surrounding
    whitespace in the source must not merge adjacent words together.
    """

    def test_real_saraiki_example_no_word_merging(self):
        # The exact real example found on Saraiki Wikipedia: <br> sits
        # directly between two words with no whitespace anywhere.
        text = "وطنوں اُٹھا<br>رب جانڑے کِیہ جیا قاتل ھِے جس آڈھے خان نوں کُٹھا"
        result = self.get_result(text)
        self.assertEqual(
            result,
            ["وطنوں اُٹھا رب جانڑے کِیہ جیا قاتل ھِے جس آڈھے خان نوں کُٹھا"]
        )
        # The specific failure mode being guarded against: words fused
        # together with no separator at all.
        self.assertNotIn("اُٹھارب", result[0])

    def test_old_style_br_with_attribute_no_surrounding_space(self):
        text = "اُٹھا<br clear=all>رب"
        result = self.get_result(text)
        self.assertEqual(result, ["اُٹھا رب"])

    def test_self_closed_br_no_surrounding_space(self):
        text = "اُٹھا<br/>رب"
        result = self.get_result(text)
        self.assertEqual(result, ["اُٹھا رب"])

    def test_no_double_space_when_source_already_has_spaces(self):
        # If the source already had whitespace around the tag, the
        # substituted space must not create a visible double space in
        # the final output.
        text = "اُٹھا <br> رب"
        result = self.get_result(text)
        self.assertEqual(result, ["اُٹھا رب"])


class BrTagMatchingVariantsTests(VoidElementTestCase):
    """Every real-world variant of <br> should be recognized and
    replaced with a space, whether or not it has attributes and
    whether or not it's self-closed.
    """

    def test_old_style_br_with_attribute_no_slash(self):
        text = "Line one.<br clear=all>Line two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one. Line two."])

    def test_old_style_br_with_quoted_attribute_no_slash(self):
        text = 'Line one.<br clear="all">Line two.'
        result = self.get_result(text)
        self.assertEqual(result, ["Line one. Line two."])

    def test_bare_br_no_attributes_no_slash(self):
        text = "Line one.<br>Line two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one. Line two."])

    def test_self_closed_br_no_space(self):
        text = "Line one.<br/>Line two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one. Line two."])

    def test_self_closed_br_with_space(self):
        text = "Line one.<br />Line two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one. Line two."])


class HrTagVariantsTests(VoidElementTestCase):
    """hr is the same class of genuinely void, line-break-carrying
    element as br.
    """

    def test_bare_hr_no_slash(self):
        text = "Line one.<hr>Line two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one. Line two."])

    def test_hr_with_attribute_no_slash(self):
        text = "Line one.<hr noshade>Line two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one. Line two."])


class RefTagSafetyTests(VoidElementTestCase):
    """Critical safety check: ref/references must NOT be affected by
    either fix. A real paired <ref>...</ref> tag must continue to be
    handled as a genuine pair, not misidentified as self-closing just
    because its opening tag happens to lack a trailing slash (which is
    normal -- paired tags never have one) -- and self-closing ref
    reuse must not have a space substituted in its place, since that
    has no real-content-separation justification the way br/hr do.
    """

    def test_real_paired_ref_tag_still_handled_as_a_pair(self):
        text = 'See citation.<ref name="foo">Some Real Citation Text</ref> more text.'
        result = self.get_result(text)
        self.assertEqual(result, ["See citation. more text."])
        self.assertNotIn("Some Real Citation Text", result[0])

    def test_self_closing_ref_reuse_no_space_substituted(self):
        # <ref name="x" /> is deleted with nothing substituted, same
        # as before this fix -- only br/hr get space substitution.
        text = 'word<ref name="foo" />word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_adjacent_br_and_ref_do_not_interfere(self):
        text = 'Line one.<br><ref name="x">cite</ref>Line two.'
        result = self.get_result(text)
        self.assertEqual(result, ["Line one. Line two."])


class NobrTagTests(VoidElementTestCase):
    """nobr keeps the permissive (optional trailing slash) matching,
    but stays in the pure-deletion group: "no line break" doesn't call
    for inserting a space where the tag was.
    """

    def test_bare_nobr_no_space_substituted(self):
        text = "word<nobr>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


class LineBoundaryTests(VoidElementTestCase):
    """A line-break tag sitting at the very start/end of a line (or
    the whole text) should not get a space substituted on that side --
    there's nothing on the empty side to merge with, so adding one
    just creates an invisible leading/trailing space that clutters
    diffs without affecting meaning. This is what a real large diff on
    Saraiki Wikipedia surfaced: many lines showing as "changed" in a
    diff while looking completely identical, because of an invisible
    trailing space where a <br/> had been at the end of the line.
    """

    def test_br_at_very_start_of_text_no_leading_space(self):
        text = "<br>Line one."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one."])

    def test_br_at_very_end_of_text_no_trailing_space(self):
        text = "Line one.<br>"
        result = self.get_result(text)
        self.assertEqual(result, ["Line one."])

    def test_br_immediately_after_a_newline_no_leading_space(self):
        text = "Line one.\n<br>Line two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one.", "Line two."])

    def test_br_immediately_before_a_newline_no_trailing_space(self):
        text = "Line one.<br>\nLine two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one.", "Line two."])

    def test_br_alone_on_its_own_line(self):
        text = "Line one.\n<br>\nLine two."
        result = self.get_result(text)
        self.assertEqual(result, ["Line one.", "Line two."])

    def test_middle_of_line_still_gets_a_space(self):
        # The regression check: boundary-awareness must not accidentally
        # disable the original word-merging fix for the common case.
        text = "وطنوں اُٹھا<br>رب جانڑے"
        result = self.get_result(text)
        self.assertEqual(result, ["وطنوں اُٹھا رب جانڑے"])

    def test_existing_single_space_before_tag_no_double_space(self):
        # A plain space (not a newline) already adjacent to the tag
        # must count as a boundary too -- otherwise this function can
        # produce a double space on its own, relying on some other,
        # unrelated part of the pipeline to clean it up afterward.
        result = ex.substituteLineBreakTag(ex.lineBreak_tag_patterns[0], "text <br>more")
        self.assertEqual(result, "text more")

    def test_existing_single_space_after_tag_no_double_space(self):
        result = ex.substituteLineBreakTag(ex.lineBreak_tag_patterns[0], "text<br> more")
        self.assertEqual(result, "text more")


class UnrelatedTagHandlingUnaffectedTests(VoidElementTestCase):
    """Sanity check that ignoredTags (b, i, nowiki, etc.) -- an
    entirely separate code path -- are unaffected by either fix.
    """

    def test_ignored_tags_still_drop_wrapper_keep_content(self):
        text = "Some <b>bold</b> and <i>italic</i> text."
        result = self.get_result(text)
        self.assertEqual(result, ["Some bold and italic text."])

    def test_nowiki_still_preserves_literal_content(self):
        text = "<nowiki>literal [[not a link]] text</nowiki>"
        result = self.get_result(text)
        self.assertEqual(result, ["literal not a link text"])


if __name__ == '__main__':
    unittest.main()
