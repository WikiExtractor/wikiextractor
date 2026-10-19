"""Tests for dropNested() when a later group is unbalanced.

dropNested() collects a span per matched open/close pair. When it runs
out of closing delimiters partway through, only the group it is matching
at that moment is unbalanced -- the spans already collected were matched
against closing delimiters of their own and are still correct.

Discarding them and stretching a single span from the first open to the
last close instead deletes everything in between. On an article whose
first table is well formed and whose last one is not, that is the whole
article: pnbwiki's افغانستان extracted to nothing at all. Its expanded
text opens with a complete 1,400-character infobox, then 30,000
characters of prose, then three more tables of which the last never
closes -- and 50,862 of its 51,689 characters were removed as one span.

Articles affected this way extract to an empty document, so the loss
leaves nothing behind to notice. pnbwiki had 280 of them in a 1,761
document sample.

Run with:
    python -m unittest tests.test_unbalanced_nesting -v
or, from the tests/ directory:
    python -m unittest test_unbalanced_nesting -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import dropNested

OPEN, CLOSE = r'{\|', r'\|}'


class UnbalancedGroupTests(unittest.TestCase):

    def test_a_matched_table_before_an_unbalanced_one_is_the_only_one_dropped(self):
        text = 'A\n{|\nrow\n|}\nPROSE\n{|\nrow\n{|\ninner\n|}\nmore\n{|\ndangling\n'
        self.assertIn('PROSE', dropNested(text, OPEN, CLOSE))

    def test_the_unbalanced_group_itself_is_still_dropped(self):
        text = 'A\n{|\nrow\n|}\nPROSE\n{|\nrow\n{|\ninner\n|}\nmore\n{|\ndangling\n'
        result = dropNested(text, OPEN, CLOSE)
        self.assertNotIn('row', result)
        self.assertNotIn('inner', result)

    def test_several_matched_tables_before_an_unbalanced_one(self):
        text = ('one\n{|\na\n|}\nTWO\n{|\nb\n|}\nTHREE\n'
                '{|\nc\n{|\nd\n|}\ne\n{|\nf\n')
        result = dropNested(text, OPEN, CLOSE)
        for kept in ('one', 'TWO', 'THREE'):
            self.assertIn(kept, result)


class BalancedInputUnaffectedTests(unittest.TestCase):

    def test_two_separate_tables(self):
        self.assertEqual(dropNested('A\n{|\nr\n|}\nPROSE\n{|\nr\n|}\nTAIL\n', OPEN, CLOSE),
                         'A\n\nPROSE\n\nTAIL\n')

    def test_a_nested_table(self):
        self.assertEqual(dropNested('A\n{|\nr\n{|\ninner\n|}\nr2\n|}\nTAIL\n', OPEN, CLOSE),
                         'A\n\nTAIL\n')

    def test_no_table_at_all(self):
        self.assertEqual(dropNested('just prose\n', OPEN, CLOSE), 'just prose\n')


class SingleUnbalancedGroupTests(unittest.TestCase):
    """With nothing matched before it, an unbalanced group on its own
    is left alone -- unchanged behavior, and the case the old branch
    was presumably written for."""

    def test_one_table_that_never_closes(self):
        text = 'A\n{|\nr\nPROSE\n'
        self.assertEqual(dropNested(text, OPEN, CLOSE), text)


if __name__ == '__main__':
    unittest.main()
