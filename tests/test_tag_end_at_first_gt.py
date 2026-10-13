"""Tests that a tag ends at its first '>', quotes included.

This is MediaWiki's rule. An extension tag (ref, gallery, nowiki,
math, ...) ends at a plain scan for the next '>' --
strpos( $text, '>', $attrStart ) in
includes/Parser/Preprocessor_Hash.php -- and an ordinary HTML tag at
the [^>]*? run of Sanitizer::ELEMENT_BITS_REGEX. Quotes are read only
afterwards, by Sanitizer::decodeTagAttributes, out of the text already
cut at that '>'.

So a literal '>' inside what looks like a quoted attribute value ends
the tag on a wiki, and <ref style="a > b" /> is broken there: it is
not self-closing (the character before that '>' is a space), no
</ref> follows, and MediaWiki falls to its "no end tag" branch, which
adds the opening tag to the output as literal text and resumes
parsing after it. The cases below assert the same readings, so
extracted text matches what a reader of the page sees.

Reading whole quoted values instead, so that an inner '>' does not end
the tag, is kinder to the two or three articles a year that write one
-- and cannot be made safe. A quote that never closes where its editor
intended runs on to the next quote character in the text, and the tag
swallows everything up to whatever '>' follows that; urwiki's
ابن سینا lost 33,000 characters that way. The only bound that stops it
is forbidding '>' inside a quoted value, which matches exactly what
[^>]* matches anyway. tests/test_malformed_tag_attributes.py has that
article and the two others it was found with.

Run with:
    python -m unittest tests.test_tag_end_at_first_gt -v
or, from the tests/ directory:
    python -m unittest test_tag_end_at_first_gt -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class TagEndTestCase(unittest.TestCase):

    def setUp(self):
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        self.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                              "Test Article", [article_text],
                              templatePrefix=self.templatePrefix)
        return extractor.clean_text(article_text, expand_templates=True)


class PairedTagWithInnerGreaterThanTests(TagEndTestCase):
    """A paired element survives an inner '>' intact, because whatever
    the open tag leaves behind sits inside the span its closing tag
    ends -- so the whole element goes."""

    def test_ref_with_a_greater_than_in_its_value_is_still_dropped(self):
        self.assertEqual(self.get_result('word<ref style="a > b">content</ref>word'),
                         ["wordword"])


class SelfClosingTagWithInnerGreaterThanTests(TagEndTestCase):
    """With the tag ending at the inner '>', the trailing '/' is never
    reached, so the tag is not self-closing. MediaWiki finds no </ref>
    either and emits the opening tag as literal text."""

    def test_a_ref_is_left_in_the_text(self):
        self.assertEqual(self.get_result('word<ref style="a > b" />word'),
                         ['word&lt;ref style="a &gt; b" /&gt;word'])

    def test_a_br_leaves_the_remainder_of_its_attributes(self):
        # br is substituted with a space wherever it is recognized, and
        # what the tag no longer covers stays as text.
        self.assertEqual(self.get_result('word<br style="a > b" />word'),
                         ['word b" /&gt;word'])

    def test_a_br_without_the_slash_behaves_the_same(self):
        self.assertEqual(self.get_result('word<br style="a > b">word'),
                         ['word b"&gt;word'])

    def test_a_nobr_behaves_the_same(self):
        self.assertEqual(self.get_result('word<nobr style="a > b" />word'),
                         ['word b" /&gt;word'])


class OrdinaryTagsUnaffectedTests(TagEndTestCase):
    """Tags without a '>' in an attribute value -- which is nearly all
    of them -- are unaffected by where the scan stops."""

    def test_bare_ref(self):
        self.assertEqual(self.get_result('word<ref>content</ref>word'), ["wordword"])

    def test_named_ref(self):
        self.assertEqual(self.get_result('word<ref name="x">content</ref>word'),
                         ["wordword"])

    def test_self_closing_ref(self):
        self.assertEqual(self.get_result('word<ref name="x" />word'), ["wordword"])

    def test_a_slash_inside_an_attribute_value(self):
        # Only a '/' immediately before the final '>' marks a tag self-
        # closing, so a ref name containing one still pairs normally.
        self.assertEqual(self.get_result('word<ref name="geo/18aug2018-1">content</ref>word'),
                         ["wordword"])

    def test_the_ordinary_line_break_forms(self):
        for text, label in [
            ('word<br>word', 'bare'),
            ('word<br/>word', 'self-closing, no space'),
            ('word<br />word', 'self-closing, with space'),
            ('word<br clear="all">word', 'with an attribute'),
        ]:
            with self.subTest(label=label):
                self.assertEqual(self.get_result(text), ["word word"])


if __name__ == '__main__':
    unittest.main()
