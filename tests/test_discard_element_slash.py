"""
Tests for discardElements' opening-tag pattern fix in extract.py.

discardElements (table, tr, td, th, div, ref, and others) drops a tag
AND its content together, via dropNested() matching (open, close)
pairs. Its opening-tag pattern previously excluded '/' entirely from
the attribute-matching character class ([^>/]*) -- intended to avoid
matching a genuine self-closing tag (e.g. <ref name="x" />, which
reuses an earlier-defined reference and is handled separately by
selfClosing_tag_patterns, not discardElements) as if it were a
content-wrapping open.

But that blanket exclusion also broke matching for any opening tag
whose ATTRIBUTE VALUE happens to contain a literal '/' -- e.g. a real
<ref name="geo/18aug2018-1"> found in an actual Saraiki Wikipedia
article, where the reference name itself includes a slash. The
opening tag's own pattern then failed to match at all, surviving as
literal, escaped text -- while its closing </ref> (left unpaired by
the failed opening match) got correctly stripped by the
orphaned-close-tag handling elsewhere, producing the confusing
"opening tag remains, closing tag vanished" result this fix addresses.

Fixed via a negative lookbehind, (?<!/), rather than excluding '/'
from the character class outright: matches any characters up to '>'
freely (including '/' anywhere within an attribute value), only
excluding the specific case where '/' sits immediately before the
final '>' -- which is what actually indicates a genuine self-closing
tag, not the mere presence of a slash anywhere in the attributes.

Run with:
    python -m unittest tests.test_discard_element_slash_in_attribute -v
or, from the tests/ directory:
    python -m unittest test_discard_element_slash_in_attribute -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class SlashInAttributeTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class SlashInAttributeValueTests(SlashInAttributeTestCase):

    def test_slash_within_attribute_value_still_discards_tag_and_content(self):
        text = 'word<ref name="geo/18aug2018-1">content</ref>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_real_world_saraiki_case(self):
        # The exact real case this fix was found on.
        text = ('عثمان بزدار&lt;ref name="geo/18aug2018-1"&gt; news '
                '|title=PTI nominee|date=18 اگست 2018&lt;/ref&gt; مقامی میڈیا')
        result = self.get_result(text)
        self.assertEqual(result, ["عثمان بزدار مقامی میڈیا"])

    def test_multiple_slashes_in_attribute_value(self):
        text = 'word<ref name="a/b/c/d">content</ref>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


class NormalCasesUnaffectedTests(SlashInAttributeTestCase):
    """Sanity check: ordinary cases (no slash at all, or a bare tag)
    still behave exactly as before.
    """

    def test_bare_tag_no_attributes(self):
        text = 'word<ref>content</ref>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_attribute_with_no_slash(self):
        text = 'word<ref name="geo18aug2018">content</ref>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


class GenuineSelfClosingTagStillHandledTests(SlashInAttributeTestCase):
    """The case the original '/'-exclusion was actually trying to
    protect: a genuine self-closing tag (reusing an earlier-defined
    reference) must not be treated as a discardElements-style
    content-wrapping open -- it's handled separately, by
    selfClosing_tag_patterns.
    """

    def test_self_closing_ref_with_space_before_slash(self):
        text = 'word<ref name="x" />word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_self_closing_ref_no_space_before_slash(self):
        text = 'word<ref name="x"/>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


if __name__ == '__main__':
    unittest.main()
