"""
Tests for sharp_expr()'s "trunc" support -- #expr's own prefix,
unary truncate-toward-zero operator, previously completely
unsupported (not handled anywhere in preprocessing or the AST
evaluator, so any expression using it always failed).

Motivated by a real article (en.wikipedia.org "Asteraceae") using it
directly in an infobox: "{{#expr: trunc (150 * 800 / 532)}}". Real-
world usage confirmed to always already parenthesize trunc's operand
("trunc (EXPR)"), which the fix relies on: only converts "trunc" to
"TRUNC" when immediately followed by "(", so the result is always
valid Python function-call syntax rather than needing to guess where
an unparenthesized operand would end. _sharp_expr_eval_node()
recognizes only this exact "TRUNC(...)" shape -- one positional
argument, no keywords -- not general-purpose function-call support,
the same narrow, specific-shape-only approach already used for the
"round" operator.

Run with:
    python -m unittest tests.test_sharp_expr_trunc -v
or, from the tests/ directory:
    python -m unittest test_sharp_expr_trunc -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class TruncBasicTests(unittest.TestCase):

    def test_truncates_positive_float_toward_zero(self):
        self.assertEqual(ex.sharp_expr('trunc (3.7)'), '3')

    def test_truncates_negative_float_toward_zero(self):
        # Toward zero, not floor -- -3.7 truncates to -3, not -4.
        self.assertEqual(ex.sharp_expr('trunc (-3.7)'), '-3')

    def test_integer_input_is_unchanged(self):
        self.assertEqual(ex.sharp_expr('trunc (5)'), '5')

    def test_works_without_space_before_parenthesis(self):
        self.assertEqual(ex.sharp_expr('trunc(3.7)'), '3')

    def test_zero(self):
        self.assertEqual(ex.sharp_expr('trunc (0)'), '0')


class TruncRealExpressionShapeTests(unittest.TestCase):
    """The literal expressions confirmed used in a real article
    (en.wikipedia.org "Asteraceae"), computing thumbnail width from a
    fixed base size and an image's own aspect ratio.
    """

    def test_the_exact_expressions_from_the_real_article(self):
        self.assertEqual(ex.sharp_expr('trunc (150 * 800 / 532)'), '225')
        self.assertEqual(ex.sharp_expr('trunc (150 * 800 / 600)'), '200')

    def test_trunc_of_a_nested_expression_with_operators(self):
        self.assertEqual(ex.sharp_expr('trunc (10 / 3 + 1)'), '4')


class TruncSecurityBoundaryTests(unittest.TestCase):
    """Confirms the "TRUNC(...)" recognition stays narrow -- this
    must not become a general-purpose function-call mechanism.
    #expr never supports function calls other than this one,
    specific, pre-processed shape.
    """

    def test_unrelated_function_names_are_rejected(self):
        self.assertIn('error', ex.sharp_expr('print (1)'))
        self.assertIn('error', ex.sharp_expr('__import__ ("os")'))

    def test_wrong_argument_count_is_rejected(self):
        self.assertIn('error', ex.sharp_expr('TRUNC(1, 2)'))
        self.assertIn('error', ex.sharp_expr('TRUNC()'))

    def test_keyword_arguments_are_rejected(self):
        self.assertIn('error', ex.sharp_expr('TRUNC(x=1)'))


class TruncRealPipelineEndToEndTests(unittest.TestCase):
    """Not sharp_expr() directly -- the real chain (clean_text() ->
    expandTemplate() -> callParserFunction()), confirming a template
    using trunc for its own logic resolves correctly instead of
    leaving a malformed-expression error span in the output.
    """

    def test_real_pipeline_template_using_trunc(self):
        templates = {
            'Template:ThumbWidth': '{{#expr: trunc ({{{base}}} * {{{ratio}}})}}',
        }
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [], templates=templates,
                                  templatePrefix='Template:')
        result = extractor.clean_text('{{ThumbWidth|base=150|ratio=1.5}}', expand_templates=True)
        self.assertIn('225', '\n'.join(result))


if __name__ == '__main__':
    unittest.main()
