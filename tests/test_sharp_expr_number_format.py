"""
Tests for how sharp_expr() renders its result.

#expr is PHP arithmetic, and a PHP float reaches the page through
PHP's float-to-string conversion, which uses the "precision" ini
setting -- 14 significant digits by default. Python's str() is a
different convention, and the difference shows up in extracted text
two ways.

A whole-number result printed a fractional part. MediaWiki's
{{#expr: 2022/10 round 0}} is "202"; str() makes it "202.0". That
matters beyond looks, because #expr output flows onward: bhwiki's
2022 builds its opening sentence from arithmetic on the year and came
out reading "202.00 के दशक", and a template feeding #expr into a
#switch finds "3.0" matching no case labelled "3".

Binary floating point's representation error was printed in full.
{{#expr: 0.1+0.2}} is "0.3" on a wiki; str() gives
"0.30000000000000004".

int results are deliberately untouched: they are exact, PHP prints
them however long they are, and the comparison operators' 1 and 0
arrive as ints already.

Run with:
    python -m unittest tests.test_sharp_expr_number_format -v
or, from the tests/ directory:
    python -m unittest test_sharp_expr_number_format -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class WholeNumberResultTests(unittest.TestCase):

    def test_division_landing_on_a_whole_number(self):
        self.assertEqual(ex.sharp_expr('2022/10 round 0'), '202')

    def test_multiplication_landing_on_a_whole_number(self):
        self.assertEqual(ex.sharp_expr('2.5*2'), '5')

    def test_a_float_operand_with_a_whole_result(self):
        self.assertEqual(ex.sharp_expr('5.0 mod 2'), '1')

    def test_exponent_notation_input(self):
        self.assertEqual(ex.sharp_expr('1e3'), '1000')

    def test_round_to_a_whole_number(self):
        self.assertEqual(ex.sharp_expr('3.7 round 0'), '4')

    def test_no_result_carries_a_trailing_point_zero(self):
        for expr in ('1000/10', '4/2', '0.5*4', '1e2', '9 round 0'):
            with self.subTest(expr=expr):
                self.assertNotIn('.', ex.sharp_expr(expr))


class PrecisionTests(unittest.TestCase):

    def test_representation_error_does_not_reach_the_page(self):
        self.assertEqual(ex.sharp_expr('0.1+0.2'), '0.3')

    def test_a_repeating_fraction_stops_at_fourteen_digits(self):
        self.assertEqual(ex.sharp_expr('1/3'), '0.33333333333333')

    def test_a_genuine_fraction_is_kept(self):
        self.assertEqual(ex.sharp_expr('7/2'), '3.5')

    def test_a_short_decimal_is_unchanged(self):
        self.assertEqual(ex.sharp_expr('2022/1000'), '2.022')

    def test_a_large_magnitude_keeps_exponent_form(self):
        self.assertEqual(ex.sharp_expr('1e16'), '1e+16')


class IntegerResultTests(unittest.TestCase):
    """int arithmetic never went through the float path and must not
    start now -- PHP prints an integer in full, at any length."""

    def test_integer_arithmetic(self):
        self.assertEqual(ex.sharp_expr('(1+2)*3'), '9')

    def test_integer_modulo(self):
        self.assertEqual(ex.sharp_expr('12 mod 5'), '2')

    def test_trunc_returns_an_integer(self):
        self.assertEqual(ex.sharp_expr('trunc (150*800/532)'), '225')

    def test_a_long_integer_keeps_every_digit(self):
        self.assertEqual(ex.sharp_expr('12345678901234567890+1'),
                         '12345678901234567891')

    def test_comparisons_still_give_one_and_zero(self):
        self.assertEqual(ex.sharp_expr('1<2'), '1')
        self.assertEqual(ex.sharp_expr('2<1'), '0')


class DownstreamTests(unittest.TestCase):
    """The reason this matters: #expr output is re-read as wikitext."""

    @staticmethod
    def clean(wikitext, templates):
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                 templates=templates, templatePrefix='Template:')
        return '\n'.join(extractor.clean_text(wikitext, expand_templates=True))

    def test_a_whole_number_result_matches_a_switch_case(self):
        templates = {
            'Template:Millennium': ('{{#switch: {{#expr: {{{1}}}/1000 round 0}}'
                                    '|2=second|3=third|unknown}}'),
        }
        self.assertEqual(self.clean('{{Millennium|3000}}', templates), 'third')

    def test_a_whole_number_result_matches_an_ifeq(self):
        templates = {
            'Template:IsTwo': '{{#ifeq:{{#expr:{{{1}}}/2}}|2|yes|no}}',
        }
        self.assertEqual(self.clean('{{IsTwo|4}}', templates), 'yes')

    def test_the_bhwiki_decade_reads_as_a_whole_number(self):
        # 2022's opening sentence, reduced: "... the 3rd year of the
        # 2020s decade". The decade came out as "202.00".
        templates = {
            'Template:Decade': '{{#expr: {{{1}}}/10 round 0}}0',
        }
        self.assertEqual(self.clean('{{Decade|2022}} के दशक', templates),
                         '2020 के दशक')


if __name__ == '__main__':
    unittest.main()
