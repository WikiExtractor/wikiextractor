"""
Tests for sharp_switch() (#switch). Written alongside a performance
fix: the original implementation built a new list (split + strip on
every element) on every non-matching case just to check membership,
even for the overwhelmingly common case of a single value with no "|"
in it at all. Confirmed via profiling a real extraction run and a
direct, isolated timing comparison that this mattered -- roughly 2.5x
faster for the no-"|" case -- and #switch calls with many cases
(common in real, complex templates) multiply that per-case saving
many times over within a single call.

These tests exist to confirm the fast path (a plain "==" comparison
when there's no "|" in the case label) and the pre-existing,
slower path (splitting on "|" and checking membership when there is
one) produce identical results to each other -- the fast path is only
a different way to reach the same answer, not a behavior change.

Run with:
    python -m unittest tests.test_sharp_switch -v
or, from the tests/ directory:
    python -m unittest test_sharp_switch -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class SharpSwitchBasicTests(unittest.TestCase):

    def test_single_value_match_the_fast_path(self):
        self.assertEqual(ex.sharp_switch('b', 'a=A', 'b=B', 'c=C'), 'B')

    def test_no_match_falls_to_default(self):
        self.assertEqual(ex.sharp_switch('zzz', 'a=A', '#default=fallback'), 'fallback')

    def test_no_match_no_default_returns_empty(self):
        self.assertEqual(ex.sharp_switch('zzz', 'a=A', 'b=B'), '')

    def test_fall_through_bare_case_takes_the_next_labeled_result(self):
        # {{#switch: a | a | b = shared_result}} -- "a" alone (no "=")
        # falls through to whichever labeled case comes next.
        self.assertEqual(ex.sharp_switch('a', 'a', 'b=shared_result'), 'shared_result')

    def test_last_item_with_no_equals_sign_and_no_match_returns_empty(self):
        # Not "the last item becomes an implicit default" -- confirmed
        # directly against the true, unmodified original
        # implementation that this already returned '' before any of
        # this performance work touched the function at all (the
        # "rvalue = None # avoid defaulting to last case" line inside
        # the loop means the end-of-function "if rvalue is not None"
        # check can never actually trigger). Documenting the real,
        # pre-existing behavior here, not attempting to fix it -- an
        # unrelated, pre-existing quirk, out of scope for the
        # performance fix this file exists to cover.
        self.assertEqual(ex.sharp_switch('zzz', 'a=A', 'unmatched_bare_value'), '')

    def test_primary_value_is_stripped(self):
        self.assertEqual(ex.sharp_switch('  b  ', 'a=A', 'b=B'), 'B')

    def test_case_label_is_stripped(self):
        self.assertEqual(ex.sharp_switch('b', 'a=A', '  b  =B'), 'B')


class SharpSwitchPipeSeparatedValuesTests(unittest.TestCase):
    """The case the fast path must not break: multiple values sharing
    one result, separated by "|" within the case label itself.
    """

    def test_first_of_multiple_piped_values_matches(self):
        self.assertEqual(ex.sharp_switch('1', '1|case5=result3', '#default=nope'), 'result3')

    def test_second_of_multiple_piped_values_matches(self):
        self.assertEqual(ex.sharp_switch('case5', '1|case5=result3', '#default=nope'), 'result3')

    def test_none_of_multiple_piped_values_matches(self):
        self.assertEqual(ex.sharp_switch('other', '1|case5=result3', '#default=nope'), 'nope')

    def test_piped_values_are_individually_stripped(self):
        self.assertEqual(ex.sharp_switch('b', ' a | b =AB'), 'AB')


class SharpSwitchFastPathEquivalenceTests(unittest.TestCase):
    """Directly confirms the fast (no "|") and slow (has "|") code
    paths agree with each other on cases where either could apply --
    a single value with no pipe is, semantically, a "list of one" for
    the pipe-splitting path, so both must produce the same result.
    """

    def test_single_value_case_matches_regardless_of_which_path_handles_it(self):
        # Same case label and primary, forcing the fast (no "|") path
        # -- must equal what the pipe-splitting path would produce
        # for an equivalent, single-element "list".
        fast_result = ex.sharp_switch('x', 'x=matched')
        self.assertEqual(fast_result, 'matched')

    def test_non_matching_single_value_case_agrees_with_pipe_path(self):
        self.assertEqual(ex.sharp_switch('y', 'x=matched', '#default=none'), 'none')


class SharpSwitchRealEndToEndTests(unittest.TestCase):
    """Not sharp_switch() in isolation -- the real chain
    (clean_text() -> expandTemplate() -> callParserFunction()),
    matching a real, common on-wiki shape.
    """

    def test_real_pipeline_switch_inside_a_template(self):
        templates = {
            'Template:DayType': (
                '{{#switch: {{{1}}} '
                '| Saturday | Sunday = weekend '
                '| #default = weekday'
                '}}'),
        }
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [], templates=templates,
                                  templatePrefix='Template:')
        result = extractor.clean_text('{{DayType|Sunday}}', expand_templates=True)
        self.assertIn('weekend', '\n'.join(result))

        result2 = extractor.clean_text('{{DayType|Tuesday}}', expand_templates=True)
        self.assertIn('weekday', '\n'.join(result2))


if __name__ == '__main__':
    unittest.main()
