"""
Tests for clean_text()'s punctuation-spacing cleanup -- a real,
pre-existing bug found while investigating whether these regex passes
could be usefully condensed into fewer operations. Both patterns were
missing their square brackets: r' (,:\\.\\)\\]\u00bb)' and
r'(\\[\\(\u00ab) ' are literal, CONCATENATED 6-character sequences,
not character classes -- meaning each only ever matched a space
immediately followed (or preceded) by all six of ,:.)]\u00bb / [(\u00ab
appearing consecutively, in that exact order, which essentially never
happens in real text. So "remove a space before closing punctuation"
and "remove a space after opening punctuation" never actually applied
to ordinary single punctuation marks like a stray space before a
comma or period -- confirmed directly against real, current output
(en.wikipedia.org "Asteraceae"): "Latin word , "star"" kept its space
before the comma untouched.

Fixed by adding the missing [...] to make each a proper character
class. Kept as two separate re.sub() passes rather than combined into
one alternation-based regex -- measured directly, a single combined
pass was consistently and substantially slower (~3x) than two
separate ones on realistic article text, even with both correctly
fixed and producing identical output.

Run with:
    python -m unittest tests.test_punctuation_spacing_cleanup -v
or, from the tests/ directory:
    python -m unittest test_punctuation_spacing_cleanup -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class PunctuationSpacingCleanupTests(unittest.TestCase):

    def make_extractor(self):
        return ex.Extractor(1, "1", "https://x", "Test Article", [])

    def clean(self, text):
        return '\n'.join(self.make_extractor().clean_text(text, expand_templates=True))

    def test_space_before_comma_is_removed(self):
        self.assertEqual(self.clean('word , next'), 'word, next')

    def test_space_before_colon_is_removed(self):
        self.assertEqual(self.clean('word : next'), 'word: next')

    def test_space_before_period_is_removed(self):
        self.assertEqual(self.clean('word .'), 'word.')

    def test_space_before_closing_paren_is_removed(self):
        self.assertEqual(self.clean('Some text. (word )'), 'Some text. (word)')

    def test_space_before_closing_bracket_is_removed(self):
        self.assertEqual(self.clean('Some text. [word ]'), 'Some text. [word]')

    def test_space_before_guillemet_is_removed(self):
        self.assertEqual(self.clean('Some text. word \u00bb'), 'Some text. word\u00bb')

    def test_space_after_opening_paren_is_removed(self):
        self.assertEqual(self.clean('Some text. ( word)'), 'Some text. (word)')

    def test_space_after_opening_bracket_is_removed(self):
        self.assertEqual(self.clean('Some text. [ word]'), 'Some text. [word]')

    def test_space_after_opening_guillemet_is_removed(self):
        self.assertEqual(self.clean('Some text. \u00ab word'), 'Some text. \u00abword')

    def test_the_exact_real_article_regression(self):
        # The literal shape confirmed still broken in real, current
        # output before this fix: en.wikipedia.org "Asteraceae"'s
        # "...Classical Latin word , "star", which came from..."
        self.assertEqual(
            self.clean('Classical Latin word , "star", which came'),
            'Classical Latin word, "star", which came')

    def test_multiple_spaces_before_punctuation_still_collapse_correctly(self):
        # spaces.sub() (2+ spaces -> 1) must run before this cleanup,
        # not after -- confirms that ordering is still intact.
        self.assertEqual(self.clean('word   , next'), 'word, next')

    def test_punctuation_with_no_adjacent_space_is_unaffected(self):
        self.assertEqual(self.clean('word, (fine) already'), 'word, (fine) already')


if __name__ == '__main__':
    unittest.main()
