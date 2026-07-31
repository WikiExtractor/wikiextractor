"""
Tests for orphaned-closing-tag handling in discardElements processing
(extract.py's clean()). discardElements (table, tr, td, th, div, and
others -- see the list itself) drops a tag AND its content together,
via dropNested() matching (open, close) pairs. dropNested() only ever
removes a close tag as part of a matched pair with some earlier open
tag -- confirmed directly: an unpaired close tag is left completely
untouched by its pairing logic, rather than throwing off matching for
the rest of the document.

That means a genuinely orphaned closing tag -- one whose own opening
tag was consumed or malformed elsewhere on the same page, e.g. by a
failed nested template expansion swallowing it -- previously survived
as literal, HTML-escaped text in the final output (confirmed on a
real PNB Wikipedia article: an infobox row invoking a Wikidata-value
template that itself depends on a Lua module wikiextractor can't
execute; the resulting cascade of malformed, leaked wikitext consumed
the row's own opening <td ...> while leaving its </td> behind,
producing a literal "}&lt;/td&gt;" in the extracted text).

Fixed with the same "strip the stray tag rather than guess at
pairing" approach already used for orphaned noinclude tags: after
dropNested()'s normal pairing pass, anything still matching the
close-tag pattern is genuinely unpaired within this text, and gets
stripped directly.

Also fixes a related, smaller gap found while testing this: the
close-tag pattern here didn't tolerate trailing whitespace before the
final '>' (confirmed via a real HTML tokenizer, in earlier work on
ignoreTag()'s own closing pattern, that real parsers do tolerate this)
-- brought in line with that same fix.

Run with:
    python -m unittest tests.test_orphaned_discard_close_tags -v
or, from the tests/ directory:
    python -m unittest test_orphaned_discard_close_tags -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class OrphanedDiscardCloseTagTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class OrphanedCloseTagStrippedTests(OrphanedDiscardCloseTagTestCase):

    def test_orphaned_td_close_with_no_opening_tag_at_all(self):
        text = "word</td>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_orphaned_th_close_with_no_opening_tag_at_all(self):
        text = "word</th>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_orphaned_close_tolerates_trailing_whitespace_before_close_bracket(self):
        # Matches the same trailing-whitespace tolerance already
        # established for ignoreTag()'s own closing pattern.
        text = "word</  td  >word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_orphaned_close_in_realistic_surrounding_wikitext(self):
        # Approximates the shape of the real case this was found on:
        # a stray "}" (leftover from an unrelated, failed expansion)
        # immediately followed by the orphaned close tag. The "}" on
        # its own line then gets separately dropped by compact()'s
        # existing "drop residuals of lists" line filter -- a
        # different, pre-existing mechanism, not this fix.
        text = "Some lead-in text.\n}</td>\nMore article text follows."
        result = self.get_result(text)
        self.assertEqual(result, ["Some lead-in text.", "More article text follows."])


class ValidPairingStillWorksTests(OrphanedDiscardCloseTagTestCase):
    """Sanity check: normal, correctly-paired discardElements tags are
    completely unaffected -- tag AND content still both discarded
    together, same as before this fix.
    """

    def test_normal_paired_td_still_discards_tag_and_content(self):
        text = "word<td>content</td>word"
        result = self.get_result(text)
        self.assertEqual(result, ["wordword"])

    def test_valid_pair_plus_orphaned_close_plus_another_valid_pair(self):
        # The valid pairs get discarded (tag + content, as
        # discardElements always does); only the genuinely orphaned
        # close in between is stripped without any content, since it
        # never had a matching open at all.
        text = "a<td>one</td>b</td>c<td>two</td>d"
        result = self.get_result(text)
        self.assertEqual(result, ["abcd"])


if __name__ == '__main__':
    unittest.main()
