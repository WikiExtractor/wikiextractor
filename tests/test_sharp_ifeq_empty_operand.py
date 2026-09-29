"""
Tests for #ifeq when one or both operands are empty.

An empty operand is an operand like any other: "" equals "" and
differs from anything else. sharp_ifeq() skipped the comparison
whenever the right operand came out empty and returned "", choosing
neither branch.

Template:Main other is what that broke, and it is everywhere -- it is
how an infobox, citation or maintenance template asks whether it is
being rendered in an article or somewhere else:

    {{#ifeq: {{NAMESPACE}} | {{ns:0}} | main | other }}

In the main namespace both operands are legitimately empty, so the
question could never be answered and every page fell through to the
"other" branch. Since collect_pages() only ever yields ns=0 pages,
that is every page the extractor sees.

slwiki's Sekunda (page id 164) is where it surfaced: its quote
template emitted the wikitable markup it keeps for non-article
namespaces, and the cell styling reached the article as a line of raw
CSS beginning "width: 20px; vertical-align: bottom; border: none;".

Run with:
    python -m unittest tests.test_sharp_ifeq_empty_operand -v
or, from the tests/ directory:
    python -m unittest test_sharp_ifeq_empty_operand -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class EmptyOperandTests(unittest.TestCase):

    def test_two_empty_operands_are_equal(self):
        self.assertEqual(ex.sharp_ifeq('', '', 'yes', 'no'), 'yes')

    def test_an_empty_right_operand_differs_from_a_value(self):
        self.assertEqual(ex.sharp_ifeq('x', '', 'yes', 'no'), 'no')

    def test_an_empty_left_operand_differs_from_a_value(self):
        self.assertEqual(ex.sharp_ifeq('', 'x', 'yes', 'no'), 'no')

    def test_whitespace_only_operands_are_equal(self):
        self.assertEqual(ex.sharp_ifeq('  ', '\n', 'yes', 'no'), 'yes')

    def test_an_empty_comparison_with_no_false_branch_gives_empty(self):
        self.assertEqual(ex.sharp_ifeq('x', '', 'yes'), '')


class UnchangedBehaviourTests(unittest.TestCase):
    """Comparisons between two non-empty operands answer as before."""

    def test_equal_values_match(self):
        self.assertEqual(ex.sharp_ifeq('a', 'a', 'yes', 'no'), 'yes')

    def test_unequal_values_differ(self):
        self.assertEqual(ex.sharp_ifeq('a', 'b', 'yes', 'no'), 'no')

    def test_operands_are_stripped_before_comparing(self):
        self.assertEqual(ex.sharp_ifeq('  a ', '\na\n', 'yes', 'no'), 'yes')

    def test_a_missing_false_branch_gives_empty(self):
        self.assertEqual(ex.sharp_ifeq('a', 'b', 'yes'), '')

    def test_an_empty_true_branch_gives_empty(self):
        self.assertEqual(ex.sharp_ifeq('a', 'a', '', 'no'), '')


class MainOtherTests(unittest.TestCase):
    """Template:Main other, reduced to the test it makes."""

    templates = {
        'Template:Main other': ('{{#switch: {{#ifeq:{{NAMESPACE}}|{{ns:0}}'
                                '|main|other}}'
                                '|main = {{{1|}}}'
                                '|other'
                                '|#default = {{{2|}}}'
                                '}}'),
    }

    def expand(self, wikitext):
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                 templates=self.templates,
                                 templatePrefix='Template:')
        return extractor.expandTemplates(wikitext)

    def test_an_article_takes_the_main_branch(self):
        self.assertEqual(self.expand('{{Main other|1=ARTICLE|2=ELSEWHERE}}'),
                         'ARTICLE')

    def test_the_other_branch_is_not_reached_from_an_article(self):
        self.assertNotIn('ELSEWHERE',
                         self.expand('{{Main other|1=ARTICLE|2=ELSEWHERE}}'))

    def test_a_template_with_only_an_other_branch_stays_silent(self):
        # The common maintenance-template shape: nothing in articles.
        self.assertEqual(self.expand('{{Main other|2=ELSEWHERE}}'), '')


if __name__ == '__main__':
    unittest.main()
