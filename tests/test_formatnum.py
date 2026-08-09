"""
Tests for formatnum -- note no '#' prefix, unlike most parser
functions in extract.py; matches real MediaWiki's own syntax for
this one (it's technically a "string function", not a
ParserFunctions-family branching function like #expr/#ifexpr/#switch,
though it's registered in the same parserFunctions dict).

Real MediaWiki's formatnum is locale-dependent: digit-grouping
convention and whether to substitute "national" digits (e.g. Urdu/
Persian ۰-۹) both come from the source wiki's own language
configuration, which nothing in this codebase tracks. This is
necessarily an approximation:
  - Forward mode (no |R): comma thousands separators only (the
    English-Wikipedia convention, and the most common on Wikipedia
    overall). No national-digit output.
  - Reverse mode (|R): strips comma grouping and maps known local
    digit sets (Arabic-Indic, Extended Arabic-Indic) back to ASCII --
    both fixed, context-free substitutions with no locale ambiguity,
    unlike guessing a thousands-separator convention from the text
    alone.

Reverse mode is the direction that actually matters in practice:
confirmed directly against a real, on-wiki template (Format price on
ur.wikipedia.org) that every #ifexpr comparison in its digit-grouping
chain had a blank left operand before this existed, since
{{formatnum:{{{1}}}|R}} -- used throughout to normalize a value
before doing #expr arithmetic on it -- always returned nothing.

Run with:
    python -m unittest tests.test_formatnum -v
or, from the tests/ directory:
    python -m unittest test_formatnum -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class FormatnumForwardModeTests(unittest.TestCase):

    def test_integer_gets_comma_grouped(self):
        self.assertEqual(ex.sharp_formatnum('1234567'), '1,234,567')

    def test_float_gets_comma_grouped_preserving_decimal(self):
        self.assertEqual(ex.sharp_formatnum('1234567.89'), '1,234,567.89')

    def test_small_number_unaffected(self):
        self.assertEqual(ex.sharp_formatnum('42'), '42')

    def test_negative_number(self):
        self.assertEqual(ex.sharp_formatnum('-1234567'), '-1,234,567')

    def test_non_numeric_input_passed_through_unchanged(self):
        # Matches real MediaWiki's own graceful degradation on
        # malformed input, rather than raising.
        self.assertEqual(ex.sharp_formatnum('not a number'), 'not a number')


class FormatnumReverseModeTests(unittest.TestCase):

    def test_strips_comma_grouping(self):
        self.assertEqual(ex.sharp_formatnum('1,234,567', 'R'), '1234567')

    def test_negative_number_sign_preserved(self):
        self.assertEqual(ex.sharp_formatnum('-1,234', 'R'), '-1234')

    def test_converts_extended_arabic_indic_urdu_persian_digits(self):
        self.assertEqual(ex.sharp_formatnum('۱۲۳۴', 'R'), '1234')

    def test_converts_arabic_indic_digits(self):
        self.assertEqual(ex.sharp_formatnum('١٢٣٤', 'R'), '1234')

    def test_plain_ascii_number_unaffected(self):
        self.assertEqual(ex.sharp_formatnum('1234', 'R'), '1234')

    def test_result_is_usable_as_sharp_expr_input(self):
        # The actual, real-world motivation: formatnum|R exists to
        # normalize a value immediately before #expr arithmetic on
        # it -- confirm the round trip actually works.
        normalized = ex.sharp_formatnum('1,234', 'R')
        self.assertEqual(ex.sharp_expr(normalized + ' + 1'), '1235')


class FormatnumRealEndToEndTests(unittest.TestCase):
    """Not sharp_formatnum() in isolation -- the real chain
    (clean_text() -> expandTemplate() -> callParserFunction() ->
    sharp_formatnum()), matching the real on-wiki shape (formatnum|R
    feeding a value into a #expr comparison) that motivated this.
    """

    def test_real_pipeline_normalizes_a_comma_grouped_value_for_expr(self):
        templates = {
            'Template:DoubleIt': '{{#expr: {{formatnum:{{{1}}}|R}} * 2 }}',
        }
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [], templates=templates,
                                  templatePrefix='Template:')
        result = extractor.clean_text('{{DoubleIt|1,234}}', expand_templates=True)
        self.assertIn('2468', '\n'.join(result))

    def test_real_pipeline_forward_mode_formats_a_computed_result(self):
        templates = {
            'Template:BigNumber': '{{formatnum:{{#expr: 1000 * 1234 }}}}',
        }
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [], templates=templates,
                                  templatePrefix='Template:')
        result = extractor.clean_text('{{BigNumber}}', expand_templates=True)
        self.assertIn('1,234,000', '\n'.join(result))


if __name__ == '__main__':
    unittest.main()
