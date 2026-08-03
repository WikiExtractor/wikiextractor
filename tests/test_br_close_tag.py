"""
Tests for the </br> and </hr> closing-tag fix in lineBreak_tag_patterns.

br and hr are void HTML elements -- they never take a closing tag on
a well-formed page. But real wikitext sometimes has one anyway (a
malformed, if understandable, editing mistake -- someone treating a
void element as if it needed a matching close, the same instinct
behind XHTML-style self-closing syntax). Before this fix, only the
opening-tag form (<br>, <br/>, <br clear=all>, etc.) was recognized;
a stray </br> or </hr> survived untouched as literal, visible markup.

Confirmed on a real Sindhi Wikipedia article ("سنٿالي ماڻھو"): two
separate </br> tags, each sitting directly between the end of one
sentence and the start of the next with no surrounding whitespace at
all -- e.g. "...رهي ٿو.</br>هاڻوڪا..." -- carrying the exact same
word-fusion risk as an ordinary <br> tag (see the earlier br/hr
word-merging fix in clean()): simply deleting it would fuse the two
sentences together with nothing between them.

Fixed by adding a second, closing-tag pattern per lineBreakTags entry
(br, hr) to the same lineBreak_tag_patterns list, using the same
space-substitution mechanism as the opening-tag form -- no attributes
are possible on a closing tag, so the quote-aware matching the opening
pattern needs doesn't apply here.

Run with:
    python -m unittest tests.test_br_close_tag -v
or, from the tests/ directory:
    python -m unittest test_br_close_tag -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor


class ClosingBrHrTagTests(unittest.TestCase):

    def get_result(self, text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [text])
        return extractor.clean_text(text, expand_templates=False)

    def test_closing_br_between_words_becomes_a_space(self):
        # The real, confirmed shape: no whitespace at all on either
        # side of the tag in the source.
        result = self.get_result('word_one</br>word_two')
        self.assertEqual(result, ['word_one word_two'])

    def test_closing_br_with_whitespace_variants(self):
        for variant in ['</br>', '</ br>', '</br >', '</ br >', '</BR>']:
            with self.subTest(variant=variant):
                result = self.get_result(f'word_one{variant}word_two')
                self.assertEqual(result, ['word_one word_two'])

    def test_closing_hr_between_words_becomes_a_space(self):
        result = self.get_result('word_one</hr>word_two')
        self.assertEqual(result, ['word_one word_two'])

    def test_closing_br_at_start_of_line_does_not_add_leading_space(self):
        # Matches the existing behavior for the opening-tag form: a
        # line-break tag with nothing on one side needs no separator
        # there, or it just clutters every diff against that line.
        result = self.get_result('</br>word_two')
        self.assertEqual(result, ['word_two'])

    def test_closing_br_at_end_of_line_does_not_add_trailing_space(self):
        result = self.get_result('word_one</br>')
        self.assertEqual(result, ['word_one'])

    def test_paired_br_and_closing_br_both_handled(self):
        # An editor who mistakenly closes a void element might also
        # write the "opening" half -- both must resolve correctly
        # together, not just the closing tag in isolation.
        result = self.get_result('word_one<br>word_two</br>word_three')
        self.assertEqual(result, ['word_one word_two word_three'])

    def test_real_world_sindhi_article_shape(self):
        # The exact, real shape found: end of one sentence, </br>,
        # start of the next, no surrounding whitespace at all.
        text = 'رهي ٿو.</br>هاڻوڪا ڪول'
        result = self.get_result(text)
        self.assertEqual(result, ['رهي ٿو. هاڻوڪا ڪول'])


if __name__ == '__main__':
    unittest.main()
