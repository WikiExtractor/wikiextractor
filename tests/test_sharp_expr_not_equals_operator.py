"""
Tests for sharp_expr()'s "<>" to "!=" conversion.

#expr spells inequality both ways -- "<>" and "!=" -- and Python only
has the latter, having dropped "<>" in Python 3. So ast.parse()
rejected any expression containing one, and the whole thing returned
the error span rather than a number. Note this is not a case of the
comparison quietly coming out backwards: the entire expression failed.

The conversion runs before the "=" to "==" substitution. That ordering
is safe rather than accidental -- _EQUALS_TO_DOUBLE_EQUALS_RE's
lookbehind already excludes an "=" preceded by "!", so the "!=" this
produces passes through untouched -- but it is worth a test, because
the reverse order would turn "<>" into "<==" and reintroduce exactly
the failure this fixes.

Real-world impact, on jawiki. Template:Is-leap-year is

    {{#expr:{{{1}}} mod 4 = 0 and ({{{2}}}=0
            or ({{{1}}} mod 100 <> 0 or {{{1}}} mod 400 = 0))}}

and Template:Year-definition builds the opening sentence of every year
article around it:

    '''{{{1}}}年'''（{{{1}}} ねん）は、[[西暦]]（…）による、
    [[…{{#ifexpr:{{is-leap-year|{{{1}}}|…}}|閏年|平年}}]]。

With the #expr failing, is-leap-year returned the error span, the
#ifexpr around it failed too, and the link collapsed to [[]] -- so
794年 extracted as

    794年（794 ねん）は、西暦（ユリウス暦）による、。

with the 平年 that belongs between the comma and the full stop simply
gone. Every year article on the wiki was affected.

Run with:
    python -m unittest tests.test_sharp_expr_not_equals_operator -v
or, from the tests/ directory:
    python -m unittest test_sharp_expr_not_equals_operator -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def expand(wikitext, templates=None):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates=templates or {}, templatePrefix='Template:')
    return extractor.expandTemplates(wikitext)


class NotEqualsOperatorTests(unittest.TestCase):

    def test_angle_bracket_inequality(self):
        self.assertEqual(ex.sharp_expr('5 <> 3'), '1')
        self.assertEqual(ex.sharp_expr('3 <> 3'), '0')

    def test_angle_bracket_inequality_without_spaces(self):
        self.assertEqual(ex.sharp_expr('5<>3'), '1')
        self.assertEqual(ex.sharp_expr('3<>3'), '0')

    def test_bang_equals_spelling_still_works(self):
        # #expr accepts both spellings; the other one was already fine.
        self.assertEqual(ex.sharp_expr('5 != 3'), '1')
        self.assertEqual(ex.sharp_expr('3 != 3'), '0')

    def test_the_two_spellings_agree(self):
        for left, right in ((5, 3), (3, 3), (0, 0), (-1, 1)):
            with self.subTest(left=left, right=right):
                self.assertEqual(ex.sharp_expr('%d <> %d' % (left, right)),
                                 ex.sharp_expr('%d != %d' % (left, right)))

    def test_inequality_on_computed_operands(self):
        self.assertEqual(ex.sharp_expr('794 mod 100 <> 0'), '1')
        self.assertEqual(ex.sharp_expr('800 mod 100 <> 0'), '0')

    def test_several_inequalities_in_one_expression(self):
        self.assertEqual(ex.sharp_expr('(1 <> 2) and (3 <> 3)'), '0')
        self.assertEqual(ex.sharp_expr('(1 <> 2) or (3 <> 3)'), '1')


class OperatorInteractionTests(unittest.TestCase):
    """The conversion must not disturb the operators that share its
    characters, and must survive the "=" substitution that follows
    it."""

    def test_less_than_or_equal_is_unaffected(self):
        self.assertEqual(ex.sharp_expr('2 <= 3'), '1')
        self.assertEqual(ex.sharp_expr('3 <= 2'), '0')

    def test_greater_than_or_equal_is_unaffected(self):
        self.assertEqual(ex.sharp_expr('3 >= 2'), '1')
        self.assertEqual(ex.sharp_expr('2 >= 3'), '0')

    def test_bare_comparisons_are_unaffected(self):
        self.assertEqual(ex.sharp_expr('2 < 3'), '1')
        self.assertEqual(ex.sharp_expr('2 > 3'), '0')

    def test_equality_alongside_inequality(self):
        # "=" becomes "==" and "<>" becomes "!=" in the same
        # expression, neither interfering with the other.
        self.assertEqual(ex.sharp_expr('(2 = 2) and (2 <> 3)'), '1')
        self.assertEqual(ex.sharp_expr('(2 = 3) or (2 <> 2)'), '0')

    def test_inequality_does_not_become_a_mangled_comparison(self):
        # If "<>" were converted after the "=" substitution, or by
        # doubling, it would parse as "<==" and fail outright. A
        # correct result here is the whole point.
        self.assertNotEqual(ex.sharp_expr('5 <> 3'), ex._SHARP_EXPR_ERROR_SPAN)

    def test_a_genuinely_malformed_expression_still_fails(self):
        # The fix must not make "<>" so permissive that nonsense
        # containing it starts succeeding.
        self.assertEqual(ex.sharp_expr('<> 3'), ex._SHARP_EXPR_ERROR_SPAN)
        self.assertEqual(ex.sharp_expr('5 <>'), ex._SHARP_EXPR_ERROR_SPAN)


class IsLeapYearTemplateTests(unittest.TestCase):
    """jawiki's Template:Is-leap-year, verbatim apart from the
    noinclude documentation. The second argument selects the calendar:
    0 Julian, 1 Gregorian."""

    templates = {
        'Template:Is-leap-year': (
            '{{#expr:{{{1}}} mod 4 = 0 and ({{{2}}}=0 '
            'or ({{{1}}} mod 100 <> 0 or {{{1}}} mod 400 = 0))}}'),
    }

    def leap(self, year, calendar):
        return expand('{{is-leap-year|%d|%d}}' % (year, calendar), self.templates)

    def test_julian_calendar_is_every_fourth_year(self):
        self.assertEqual(self.leap(794, 0), '0')
        self.assertEqual(self.leap(796, 0), '1')
        self.assertEqual(self.leap(800, 0), '1')

    def test_gregorian_century_rule(self):
        # Divisible by 100 but not 400: not a leap year. This is the
        # branch the "<>" sits in, so it is the one that used to fail.
        self.assertEqual(self.leap(1900, 1), '0')
        self.assertEqual(self.leap(1800, 1), '0')

    def test_gregorian_four_hundred_rule(self):
        self.assertEqual(self.leap(2000, 1), '1')
        self.assertEqual(self.leap(1600, 1), '1')

    def test_gregorian_ordinary_years(self):
        self.assertEqual(self.leap(2004, 1), '1')
        self.assertEqual(self.leap(2001, 1), '0')

    def test_result_is_usable_as_an_ifexpr_condition(self):
        self.assertEqual(
            expand('{{#ifexpr:{{is-leap-year|800|0}}|閏年|平年}}', self.templates),
            '閏年')
        self.assertEqual(
            expand('{{#ifexpr:{{is-leap-year|794|0}}|閏年|平年}}', self.templates),
            '平年')


class YearDefinitionChainTests(unittest.TestCase):
    """The opening sentence of a jawiki year article, reduced to the
    part that broke: a wikilink whose target is computed through
    is-leap-year."""

    templates = dict(IsLeapYearTemplateTests.templates)
    templates['Template:Year-definition'] = (
        "'''{{{1}}}年'''（{{{1}}} ねん）は、[[西暦]]"
        "（{{#ifexpr:{{{1}}}<1582|[[ユリウス暦]]|[[グレゴリオ暦]]}}）による、"
        "[[{{#ifexpr:{{is-leap-year|{{{1}}}|{{#expr:{{{1}}}>=1582}} }}|閏年|平年}}]]。")

    def clean(self, wikitext):
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                 templates=self.templates, templatePrefix='Template:')
        return '\n'.join(extractor.clean_text(wikitext, expand_templates=True))

    def test_common_year_sentence_is_complete(self):
        self.assertEqual(self.clean('{{Year-definition|794}}'),
                         '794年（794 ねん）は、西暦（ユリウス暦）による、平年。')

    def test_leap_year_sentence_is_complete(self):
        self.assertEqual(self.clean('{{Year-definition|800}}'),
                         '800年（800 ねん）は、西暦（ユリウス暦）による、閏年。')

    def test_gregorian_year_sentence_is_complete(self):
        self.assertEqual(self.clean('{{Year-definition|1900}}'),
                         '1900年（1900 ねん）は、西暦（グレゴリオ暦）による、平年。')

    def test_no_dangling_punctuation(self):
        # The signature of the bug: a comma directly followed by the
        # full stop, with the noun that belongs between them gone.
        self.assertNotIn('、。', self.clean('{{Year-definition|794}}'))


if __name__ == '__main__':
    unittest.main()
