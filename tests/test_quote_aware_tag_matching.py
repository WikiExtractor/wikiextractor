"""
Tests for quote-aware tag matching in extract.py (lineBreak_tag_patterns,
selfClosing_tag_patterns, and discardElements' opening pattern).

A literal '>' inside a quoted attribute value is legal HTML -- e.g.
<br style="a > b" /> -- and doesn't end the tag; confirmed directly
against a real HTML tokenizer, which correctly tracks quote state.
The plain [^>]* character class these patterns previously used has no
concept of quote state at all: it simply stops at the first '>' it
sees, wherever that is. That truncated the match at the inner '>',
leaving the tag's own real ending stranded as literal, escaped text
afterward.

Fixed with the standard "quoted-string-or-single-character" alternation
((?:"[^"]*"|'[^']*'|[^>])*), which treats a full quoted string as one
atomic unit rather than character-by-character. That alone isn't
sufficient, though -- discovered directly, not assumed: combined with
discardElements' (?<!/) self-closing exclusion, the two interact
badly. When the "correct" (quote-respecting) parse fails the (?<!/)
check -- i.e. the tag genuinely IS self-closing, e.g.
<ref style="a > b" /> -- plain backtracking falls back to
re-interpreting the quote characters as individual [^>] matches
instead of giving up, finding a DIFFERENT, wrong match that ends at
the quoted value's own inner '>'. Fixed with the (?=(...))\\1
lookahead+backreference trick, which emulates atomic/possessive
matching portably (works on Python versions before 3.11's native
atomic groups too): once a quoted string is matched, the engine can
never backtrack into re-interpreting its own quote characters.

Also confirmed and tested here (see NormalCasesUnaffectedTests /
GenuineSelfClosingStillHandledTests): this doesn't change behavior for
any of the ordinary cases already covered by earlier fixes -- bare
tags, a literal slash inside an attribute value (unrelated to quoting,
a separate earlier fix), and genuine self-closing tags without quoted
attributes at all.

Run with:
    python -m unittest tests.test_quote_aware_tag_matching -v
or, from the tests/ directory:
    python -m unittest test_quote_aware_tag_matching -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class QuoteAwareTagMatchingTestCase(unittest.TestCase):

    def setUp(self):
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        self.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text], templatePrefix=self.templatePrefix)
        return extractor.clean_text(article_text, expand_templates=True)


class LineBreakTagQuoteAwarenessTests(QuoteAwareTagMatchingTestCase):
    """lineBreak_tag_patterns (br/hr) -- the exact case reported."""

    def test_quoted_greater_than_with_self_closing_slash(self):
        text = 'word<br style="a > b" />word'
        result = self.get_result(text)
        self.assertEqual(result, ["word word"])

    def test_quoted_greater_than_without_self_closing_slash(self):
        text = 'word<br style="a > b">word'
        result = self.get_result(text)
        self.assertEqual(result, ["word word"])

    def test_ordinary_forms_still_work(self):
        for text, label in [
            ('word<br>word', 'bare'),
            ('word<br/>word', 'self-closing, no space'),
            ('word<br />word', 'self-closing, with space'),
        ]:
            with self.subTest(label=label):
                result = self.get_result(text)
                self.assertEqual(result, ["word word"])


class DiscardElementsQuoteAwarenessTests(QuoteAwareTagMatchingTestCase):
    """discardElements' opening pattern (ref, div, etc.) -- the harder
    case, since the self-closing exclusion (?<!/) has to coexist with
    quote-awareness correctly.
    """

    def test_quoted_greater_than_with_self_closing_slash_correctly_not_matched_as_open(self):
        # Genuinely self-closing -- must NOT be treated as a
        # discardElements-style content-wrapping open at all.
        text = 'word<ref style="a > b" />word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_quoted_greater_than_paired_non_self_closing(self):
        text = 'word<ref style="a > b">content</ref>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


class NormalCasesUnaffectedTests(QuoteAwareTagMatchingTestCase):
    """Sanity check: ordinary cases, and the earlier slash-in-attribute
    fix, still behave exactly as before this change.
    """

    def test_bare_tag(self):
        text = 'word<ref>content</ref>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_slash_in_attribute_value_unrelated_to_quoting(self):
        text = 'word<ref name="geo/18aug2018-1">content</ref>word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


class GenuineSelfClosingStillHandledTests(QuoteAwareTagMatchingTestCase):

    def test_self_closing_no_quoted_attributes(self):
        text = 'word<ref name="x" />word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_nobr_self_closing_with_quoted_greater_than(self):
        text = 'word<nobr style="a > b" />word'
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])


if __name__ == '__main__':
    unittest.main()
