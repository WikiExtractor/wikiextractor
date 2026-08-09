"""
Tests for padleft/padright -- note no '#' prefix, matching real
MediaWiki's own syntax for these (part of the "string functions"
extension, like formatnum, lc, uc, urlencode -- not the '#'-prefixed
ParserFunctions-family branching functions like #if/#switch/#expr).

padleft was previously "implemented" but silently, completely broken:
its lambda referenced a variable (`pad`) that was never defined
anywhere in its own scope, so every call raised NameError, which
callParserFunction()'s own catch-all swallowed into an empty result
with no warning logged at all -- confirmed directly before this fix:
{{padleft:7|3|0}} returned '' every time. padright didn't exist even
as a stub.

Both are now implemented via a single shared core (_sharp_pad),
matching PHP's own str_pad() semantics that real MediaWiki's
implementation is built on: the padding string is repeated as many
times as needed to cover the gap, then truncated to exactly that many
characters (not repeated a whole number of times and left over-long)
before being attached to the original string.

Run with:
    python -m unittest tests.test_padleft_padright -v
or, from the tests/ directory:
    python -m unittest test_padleft_padright -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class PadleftTests(unittest.TestCase):

    def test_default_padding_character_is_zero(self):
        self.assertEqual(ex.sharp_padleft('7', '3'), '007')

    def test_multi_character_padding_repeats_then_truncates_not_whole_repetitions(self):
        # Needs 3 characters of padding (width 4 - len("1")). "xy"
        # repeated is "xyxyxy..."; truncated to 3 chars is "xyx" --
        # not "xy1" (short by one) and not "xyxy1" (one whole extra
        # repetition, four characters instead of three).
        self.assertEqual(ex.sharp_padleft('1', '4', 'xy'), 'xyx1')

    def test_string_already_at_or_past_target_width_is_unchanged(self):
        self.assertEqual(ex.sharp_padleft('12345', '3'), '12345')

    def test_string_exactly_at_target_width_is_unchanged(self):
        self.assertEqual(ex.sharp_padleft('123', '3'), '123')

    def test_non_numeric_width_leaves_string_unchanged(self):
        self.assertEqual(ex.sharp_padleft('7', 'notanumber'), '7')

    def test_negative_width_leaves_string_unchanged(self):
        self.assertEqual(ex.sharp_padleft('7', '-1'), '7')

    def test_empty_padding_string_leaves_value_unchanged(self):
        # Nothing to repeat -- must not loop forever or divide by zero.
        self.assertEqual(ex.sharp_padleft('7', '5', ''), '7')

    def test_whitespace_around_width_is_tolerated(self):
        self.assertEqual(ex.sharp_padleft('7', ' 3 '), '007')


class PadrightTests(unittest.TestCase):

    def test_default_padding_character_is_zero(self):
        self.assertEqual(ex.sharp_padright('7', '3'), '700')

    def test_multi_character_padding_repeats_then_truncates(self):
        self.assertEqual(ex.sharp_padright('1', '4', 'xy'), '1xyx')

    def test_string_already_at_or_past_target_width_is_unchanged(self):
        self.assertEqual(ex.sharp_padright('12345', '3'), '12345')


class PadRealEndToEndTests(unittest.TestCase):
    """Not sharp_padleft()/sharp_padright() in isolation -- the real
    chain (clean_text() -> expandTemplate() -> callParserFunction()),
    matching a real, common on-wiki shape: zero-padding a date or ID
    number inside a template.
    """

    def test_real_pipeline_zero_pads_a_computed_day_number(self):
        templates = {
            'Template:TwoDigitDay': '{{padleft:{{{1}}}|2}}',
        }
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [], templates=templates,
                                  templatePrefix='Template:')
        result = extractor.clean_text('{{TwoDigitDay|7}}', expand_templates=True)
        self.assertIn('07', '\n'.join(result))


if __name__ == '__main__':
    unittest.main()
