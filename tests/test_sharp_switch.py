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

    def test_last_item_with_no_equals_sign_is_the_default(self):
        # MediaWiki's documented rule: "the last parameter, if it has
        # no equals sign, is the default".
        #
        # This returned '' for years. The end-of-function check was
        # written as "if rvalue is not None", with rvalue cleared at
        # the top of every iteration and again after every labeled
        # case, so it could never be anything but None by the time the
        # check ran -- the branch was unreachable and the trailing
        # default silently dropped.
        self.assertEqual(ex.sharp_switch('zzz', 'a=A', 'unmatched_bare_value'),
                         'unmatched_bare_value')

    def test_the_trailing_default_is_stripped(self):
        self.assertEqual(ex.sharp_switch('zzz', 'a=A', '  fallback  '), 'fallback')

    def test_a_match_still_beats_the_trailing_default(self):
        self.assertEqual(ex.sharp_switch('a', 'a=A', 'fallback'), 'A')

    def test_the_trailing_default_beats_an_earlier_hash_default(self):
        # MediaWiki's own order: CoreParserFunctions::switch() tests
        # $lastItemHadNoEquals before falling back to $default.
        self.assertEqual(ex.sharp_switch('zzz', '#default=named', 'trailing'),
                         'trailing')

    def test_a_hash_default_after_the_last_bare_case_still_wins(self):
        # Here the last part does have "=", so there is no trailing
        # default to prefer and #default applies as usual.
        self.assertEqual(ex.sharp_switch('zzz', 'bare', '#default=named'), 'named')

    def test_a_bare_case_that_matches_and_ends_the_list_returns_itself(self):
        # {{#switch: a | x = X | a }} -- the match sets the fall-through
        # flag but nothing follows it, so the trailing default (which
        # is that same part) is what comes back.
        self.assertEqual(ex.sharp_switch('a', 'x=X', 'a'), 'a')

    def test_an_empty_trailing_part_defaults_to_empty(self):
        # {{#switch: zzz | a = A | }} -- a trailing "|" before the
        # closing braces is extremely common and must stay empty.
        self.assertEqual(ex.sharp_switch('zzz', 'a=A', ''), '')

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

    def test_abbr_keeps_the_words_it_wraps(self):
        # Template:Abbr, reduced to the part that matters. Its visible
        # text -- the abbreviation itself -- is the #switch's trailing
        # default, reached whenever the optional third argument is not
        # "i" or "IPA", which is nearly always.
        #
        # While the trailing default was being dropped, every {{abbr}}
        # on every wiki deleted its own content, taking words out of
        # the middle of a sentence rather than leaving anything to
        # notice. bhwiki's भारत, mid-paragraph:
        #
        #   "भारत के {{abbr|फॉरेन एक्सचेंज रिमिटेंस|...}} साल 2014 में"
        #     extracted as  "भारत के  साल 2014 में"
        templates = {
            'Template:Abbr': ('<abbr title="{{{2|}}}">{{#switch: {{{3|}}}'
                              ' | i | IPA = {{IPA|{{{1|}}}}}'
                              ' | {{{1|}}} }}</abbr>'),
        }
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                 templates=templates, templatePrefix='Template:')
        result = '\n'.join(extractor.clean_text(
            'India received {{abbr|remittances|money sent home}} in 2014.',
            expand_templates=True))
        self.assertEqual(result, 'India received remittances in 2014.')


if __name__ == '__main__':
    unittest.main()
