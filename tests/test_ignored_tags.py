"""
Tests for the ignoredTags mechanism in extract.py: a fixed list of
tags (div, span, b, i, p, sub, sup, and others -- see ignoredTags
itself) whose markup gets stripped while their CONTENT is preserved,
via ignoreTag()/ignored_tag_patterns and the "Drop ignored tags" step
in clean(). This is a distinct mechanism from selfClosingTags/
lineBreakTags (see test_selfclosing_tags.py, which covers br/hr/nobr/
ref/references/nowiki/templatestyles) and from discardElements (which
drops the tag AND its content together, e.g. noinclude) -- ignoredTags
is specifically for wrapper-style markup where only the tag syntax
itself is presentational, not the text it wraps.

There wasn't previously a dedicated test file for this mechanism on
its own -- only a brief interaction test between ignored tags and
bracket-handling. This file exists to cover the mechanism itself.

Precise matching behavior confirmed directly against the actual regex
patterns (ignoreTag() builds `<tag\\b.*?>` for the opening half and
`</\\s*tag>` for the closing half) rather than assumed:
  - The opening tag requires the tag name immediately after `<` --
    NO whitespace tolerated there (`< div>` does not match `<div`).
  - The closing tag tolerates whitespace between `</` and the tag
    name (`</  div>` matches), but NOT after the tag name before `>`
    (`</div  >` does not match) -- an asymmetry worth knowing, not
    just assuming symmetric whitespace handling either way.

Run with:
    python -m unittest tests.test_ignored_tags -v
or, from the tests/ directory:
    python -m unittest test_ignored_tags -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class IgnoredTagsTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class BasicStrippingTests(IgnoredTagsTestCase):
    """The tag syntax itself is removed; the wrapped content survives.
    Checked across a representative sample of ignoredTags, not just
    one -- they all share the same underlying mechanism, but each
    entry is still its own literal string in the tuple.
    """

    def test_span_stripped_content_kept(self):
        text = "word<span>middle</span>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])

    def test_b_stripped_content_kept(self):
        text = "word<b>middle</b>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])

    def test_p_stripped_content_kept(self):
        text = "word<p>middle</p>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])

    def test_sub_and_sup_stripped_content_kept(self):
        text = "H<sub>2</sub>O and x<sup>2</sup>"
        result = self.get_result(text)
        self.assertEqual(result, ["H2O and x2"])

    def test_multiple_separate_occurrences_of_same_tag(self):
        text = "<div>one</div> middle <div>two</div>"
        result = self.get_result(text)
        self.assertEqual(result, ["one middle two"])


class NestingAndAttributeTests(IgnoredTagsTestCase):

    def test_nested_different_ignored_tags(self):
        text = "word<b><i>nested</i></b>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordnestedword"])

    def test_opening_tag_with_attributes_stripped(self):
        text = 'word<span class="notice" id="x">middle</span>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])

    def test_self_closing_form_of_an_ignored_tag(self):
        # No corresponding close anywhere -- only the opening half
        # should match and get stripped; nothing else should break.
        text = "word<span/>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


class CaseInsensitivityTests(IgnoredTagsTestCase):

    def test_uppercase_tag_name_stripped(self):
        text = "word<SPAN>middle</SPAN>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])

    def test_mixed_case_tag_name_stripped(self):
        text = "word<SpAn>middle</sPaN>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])


class WhitespaceHandlingTests(IgnoredTagsTestCase):
    """Confirmed asymmetric, not a symmetric "whitespace is always
    fine" or "whitespace never matches" rule -- verified directly
    against the actual patterns before writing these. Uses span
    rather than div specifically to avoid a separate, real interaction
    (div is registered in BOTH ignoredTags and discardElements -- see
    DivDiscardElementsOverlapTests below) that would otherwise
    confound what these tests are isolating.
    """

    def test_space_before_tag_name_in_opening_tag_breaks_match(self):
        # <%s\b.*?> requires the tag name immediately after '<' --
        # confirmed this survives completely unmatched (both halves),
        # HTML-escaped, rather than being stripped.
        text = "word< span >middle</ span >word"
        result = self.get_result(text)
        self.assertEqual(result, ["word&lt; span &gt;middle&lt;/ span &gt;word"])

    def test_space_between_close_slash_and_tag_name_is_tolerated(self):
        # </\s*%s> tolerates whitespace here specifically.
        text = "word<span>middle</  span>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])

    def test_space_after_tag_name_before_close_angle_bracket_breaks_match(self):
        # No \s* before the final '>' in the closing pattern -- this
        # closing tag does NOT match, and survives HTML-escaped, even
        # though the opening tag on the same text matched fine.
        text = "word<span>middle</span >word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddle&lt;/span &gt;word"])


class DivDiscardElementsOverlapTests(IgnoredTagsTestCase):
    """div specifically is registered in BOTH ignoredTags (strip tag,
    keep content) and discardElements (drop tag AND content together).
    ignoredTags runs first, so under normal, intact configuration div
    is stripped -- content kept -- exactly like any other ignoredTags
    entry, before discardElements ever gets a chance to see it.
    Documented here explicitly since it's a real, easy-to-miss
    interaction: if ignoredTags' own div entry were ever accidentally
    removed, div content would NOT survive escaped the way an
    ordinary unlisted tag would (see UnlistedTagUnaffectedTests) --
    it would vanish entirely instead, picked up by discardElements as
    a paired, discardable element. Confirmed directly by testing
    against a build with ignoredTags emptied out.
    """

    def test_div_stripped_content_kept_under_normal_config(self):
        text = "word<div>middle</div>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordmiddleword"])


class UnlistedTagUnaffectedTests(IgnoredTagsTestCase):
    """Sanity check: this mechanism only strips tags actually present
    in ignoredTags -- an arbitrary tag name that was never registered
    survives untouched (HTML-escaped), rather than being generically
    stripped by some catch-all rule.
    """

    def test_tag_not_in_ignored_list_survives_escaped(self):
        text = "word<madeuptagfortesting>middle</madeuptagfortesting>word"
        result = self.get_result(text)
        self.assertEqual(
            result,
            ["word&lt;madeuptagfortesting&gt;middle&lt;/madeuptagfortesting&gt;word"])


if __name__ == '__main__':
    unittest.main()
