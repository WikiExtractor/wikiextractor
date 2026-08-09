"""
Tests for the "String functions" extension family: len, pos, rpos,
sub, count, replace, explode. All '#'-prefixed, matching real
MediaWiki's own syntax for this extension -- unlike the core case/url
magic words (lc, uc, padleft, padright, urlencode, urldecode), which
aren't prefixed.

Several of these have real, documented semantics that a naive
implementation would get wrong:
  - #sub matches PHP's substr() precisely, including negative START
    (count from the end) and negative LENGTH (stop that many
    characters before the end of the *whole string*, not relative to
    START) -- not a plain Python slice translation.
  - #pos returns an empty string when the target isn't found; #rpos
    returns -1. This asymmetry is real and documented in the
    extension itself, not a bug to reconcile between the two.
  - #count, #replace (empty search), and #explode (empty delimiter)
    all diverge from what Python's own string methods would naively
    do with an empty argument, since Python's semantics there aren't
    the same as PHP's (what real MediaWiki is built on).

Run with:
    python -m unittest tests.test_string_functions -v
or, from the tests/ directory:
    python -m unittest test_string_functions -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class LenTests(unittest.TestCase):

    def test_basic_length(self):
        self.assertEqual(ex.sharp_len('hello'), '5')

    def test_empty_string(self):
        self.assertEqual(ex.sharp_len(''), '0')


class PosTests(unittest.TestCase):

    def test_found_returns_zero_based_index(self):
        self.assertEqual(ex.sharp_pos('hello world', 'world'), '6')

    def test_not_found_returns_empty_string_not_negative_one(self):
        self.assertEqual(ex.sharp_pos('hello', 'xyz'), '')

    def test_offset_skips_earlier_occurrences(self):
        self.assertEqual(ex.sharp_pos('abcabc', 'abc', '1'), '3')


class RposTests(unittest.TestCase):

    def test_found_returns_index_of_last_occurrence(self):
        self.assertEqual(ex.sharp_rpos('hello hello', 'hello'), '6')

    def test_not_found_returns_negative_one_not_empty(self):
        # Deliberately asymmetric with #pos above -- this is the real,
        # documented behavior of the actual extension.
        self.assertEqual(ex.sharp_rpos('hello', 'xyz'), '-1')


class SubTests(unittest.TestCase):

    def test_positive_start_and_length(self):
        self.assertEqual(ex.sharp_sub('Hello world', '0', '5'), 'Hello')

    def test_negative_start_counts_from_end(self):
        self.assertEqual(ex.sharp_sub('Hello world', '-5'), 'world')

    def test_length_omitted_goes_to_end(self):
        self.assertEqual(ex.sharp_sub('Hello world', '6'), 'world')

    def test_negative_length_stops_before_whole_strings_own_end(self):
        # The case a naive s[start:start+length] translation gets
        # wrong: PHP substr("Hello world", 6, -2) is "wor" (stop 2
        # short of the *whole string's* end), not computed relative
        # to start (which would give a nonsensical empty slice).
        self.assertEqual(ex.sharp_sub('Hello world', '6', '-2'), 'wor')

    def test_negative_length_matches_php_semantics_from_start_zero(self):
        self.assertEqual(ex.sharp_sub('Hello world', '0', '-6'), 'Hello')


class CountTests(unittest.TestCase):

    def test_counts_non_overlapping_occurrences(self):
        self.assertEqual(ex.sharp_count('abcabcabc', 'abc'), '3')

    def test_empty_substring_returns_zero_not_pythons_own_count_quirk(self):
        # Python's own "abc".count('') == 4 (a match between every
        # character) isn't a meaningful answer for this function.
        self.assertEqual(ex.sharp_count('abc', ''), '0')


class ReplaceTests(unittest.TestCase):

    def test_basic_replace(self):
        self.assertEqual(ex.sharp_replace('hello world', 'world', 'there'), 'hello there')

    def test_empty_search_is_a_no_op(self):
        # PHP's str_replace() with an empty search matches nothing;
        # Python's own str.replace('', x) inserts x between every
        # character, which is not the semantics to reproduce here.
        self.assertEqual(ex.sharp_replace('hello', ''), 'hello')

    def test_limit_caps_the_number_of_replacements(self):
        self.assertEqual(ex.sharp_replace('a-a-a-a', '-', '_', '2'), 'a_a_a-a')


class ExplodeTests(unittest.TestCase):

    def test_returns_the_segment_at_position(self):
        self.assertEqual(ex.sharp_explode('a-b-c', '-', '1'), 'b')

    def test_negative_position_counts_from_the_end(self):
        self.assertEqual(ex.sharp_explode('a-b-c', '-', '-1'), 'c')

    def test_position_out_of_range_returns_empty(self):
        self.assertEqual(ex.sharp_explode('a-b-c', '-', '5'), '')

    def test_empty_delimiter_returns_empty_rather_than_raising(self):
        self.assertEqual(ex.sharp_explode('abc', ''), '')

    def test_limit_caps_segment_count_last_segment_absorbs_the_rest(self):
        self.assertEqual(ex.sharp_explode('a-b-c-d', '-', '2', '3'), 'c-d')


class StringFunctionsRealEndToEndTests(unittest.TestCase):
    """Not the functions in isolation -- the real chain
    (clean_text() -> expandTemplate() -> callParserFunction()),
    matching a real, plausible on-wiki shape: extracting a file
    extension from a filename inside a template.
    """

    def test_real_pipeline_extracts_a_file_extension(self):
        templates = {
            'Template:Extension': '{{#sub:{{{1}}}|{{#pos:{{{1}}}|.}}}}',
        }
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [], templates=templates,
                                  templatePrefix='Template:')
        result = extractor.clean_text('{{Extension|photo.jpg}}', expand_templates=True)
        self.assertIn('.jpg', '\n'.join(result))


if __name__ == '__main__':
    unittest.main()
