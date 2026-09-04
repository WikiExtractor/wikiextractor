"""
Tests for sharp_time() -- {{#time: FORMAT | TIMESTAMP }}.

#time was a stub returning '' for every call. Citation and
date-validation templates lean on it heavily, in two ways that both
misfire against a stub:

  {{#switch:{{{isodate}}}|{{#time:Y-m-d|{{{isodate}}}}}=...}}

compares a date against its own reformatting to decide whether it is
well formed, and an empty case label never matches, so every date
fell to the error branch. And

  {{#ifexpr:{{#time:U|{{{isodate}}}}} < 979516800|...}}

feeds the result straight into #expr, where an empty left operand
leaves "< 979516800" to fail parsing -- 396 malformed-#expr
occurrences on the jawiki article 鳥山明 (id 194) alone, and 28 on
アンパサンド (id 5).

What is implemented here is a subset, along two axes. Numeric format
characters only: month and day names, the composite r/c formats and
the x-prefixed non-Gregorian calendars all need the source wiki's own
language data, so a format string containing any alphabetic character
outside the table is declined and returns '' rather than guessing.
And a fixed set of timestamp forms, rather than everything PHP's
strtotime accepts: ISO dates and datetimes, MediaWiki's own 14-digit
form, and a single offset from the current time.

Run with:
    python -m unittest tests.test_sharp_time -v
or, from the tests/ directory:
    python -m unittest test_sharp_time -v
"""

import datetime
import re
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def expand(wikitext, templates=None):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates=templates or {}, templatePrefix='Template:')
    return extractor.expandTemplates(wikitext)


class NumericFormatCharacterTests(unittest.TestCase):

    def test_year_four_and_two_digit(self):
        self.assertEqual(ex.sharp_time('Y', '2024-03-08'), '2024')
        self.assertEqual(ex.sharp_time('y', '2024-03-08'), '24')

    def test_month_padded_and_unpadded(self):
        self.assertEqual(ex.sharp_time('m', '2024-03-08'), '03')
        self.assertEqual(ex.sharp_time('n', '2024-03-08'), '3')

    def test_day_padded_and_unpadded(self):
        self.assertEqual(ex.sharp_time('d', '2024-03-08'), '08')
        self.assertEqual(ex.sharp_time('j', '2024-03-08'), '8')

    def test_time_of_day(self):
        self.assertEqual(ex.sharp_time('H:i:s', '2024-03-08 09:07:05'), '09:07:05')
        self.assertEqual(ex.sharp_time('G', '2024-03-08 09:07:05'), '9')

    def test_twelve_hour_clock(self):
        self.assertEqual(ex.sharp_time('g', '2024-03-08 13:00'), '1')
        self.assertEqual(ex.sharp_time('h', '2024-03-08 00:00'), '12')

    def test_unix_timestamp(self):
        self.assertEqual(ex.sharp_time('U', '1970-01-01'), '0')
        self.assertEqual(ex.sharp_time('U', '2024-03-08'), '1709856000')

    def test_leap_year_flag_and_days_in_month(self):
        self.assertEqual(ex.sharp_time('L', '2024-01-01'), '1')
        self.assertEqual(ex.sharp_time('L', '2023-01-01'), '0')
        self.assertEqual(ex.sharp_time('t', '2024-02-01'), '29')
        self.assertEqual(ex.sharp_time('t', '2023-02-01'), '28')

    def test_weekday_numbering(self):
        # 2024-03-08 was a Friday. N counts Monday as 1, w counts
        # Sunday as 0.
        self.assertEqual(ex.sharp_time('N', '2024-03-08'), '5')
        self.assertEqual(ex.sharp_time('w', '2024-03-08'), '5')
        self.assertEqual(ex.sharp_time('w', '2024-03-10'), '0')

    def test_day_of_year_is_zero_based(self):
        self.assertEqual(ex.sharp_time('z', '2024-01-01'), '0')
        self.assertEqual(ex.sharp_time('z', '2024-12-31'), '365')

    def test_combined_format_used_by_citation_templates(self):
        self.assertEqual(ex.sharp_time('Y-m-d', '2024-03-08'), '2024-03-08')
        self.assertEqual(ex.sharp_time('Y-n-j', '2024-03-08'), '2024-3-8')
        self.assertEqual(ex.sharp_time('Y-m', '2024-03-08'), '2024-03')


class LiteralAndEscapeTests(unittest.TestCase):

    def test_non_alphabetic_characters_pass_through(self):
        self.assertEqual(ex.sharp_time('Y/m/d', '2024-03-08'), '2024/03/08')

    def test_backslash_escapes_the_next_character(self):
        # The T separator in Y-m-d\TH:i, which would otherwise be read
        # as a format character.
        self.assertEqual(ex.sharp_time('Y-m-d\\TH:i', '2024-03-08 14:30'),
                         '2024-03-08T14:30')

    def test_double_quotes_mark_a_literal_run(self):
        self.assertEqual(ex.sharp_time('Y"年"', '2024-03-08'), '2024年')

    def test_unclosed_quote_is_itself_a_literal(self):
        self.assertEqual(ex.sharp_time('Y"', '2024-03-08'), '2024"')

    def test_trailing_backslash_is_dropped(self):
        self.assertEqual(ex.sharp_time('Y\\', '2024-03-08'), '2024')


class UnsupportedFormatTests(unittest.TestCase):
    """A format asking for something outside the table returns '',
    leaving the caller where the stub left it, rather than the error
    span -- the timestamp was fine, so nothing should claim otherwise.
    """

    def test_month_name_format_is_declined(self):
        self.assertEqual(ex.sharp_time('Y年F', '2024-03-08'), '')

    def test_day_name_format_is_declined(self):
        self.assertEqual(ex.sharp_time('l', '2024-03-08'), '')

    def test_declining_is_not_the_error_span(self):
        self.assertNotIn('error', ex.sharp_time('F', '2024-03-08'))

    def test_one_unsupported_character_declines_the_whole_format(self):
        self.assertEqual(ex.sharp_time('Y-m-d F', '2024-03-08'), '')


class TimestampParsingTests(unittest.TestCase):

    def test_iso_date(self):
        self.assertEqual(ex.sharp_time('Y-m-d', '2024-03-08'), '2024-03-08')

    def test_unpadded_iso_date(self):
        self.assertEqual(ex.sharp_time('Y-m-d', '2024-3-8'), '2024-03-08')

    def test_year_and_month_only_defaults_the_day(self):
        self.assertEqual(ex.sharp_time('Y-m-d', '2024-03'), '2024-03-01')

    def test_iso_datetime_with_t_separator(self):
        self.assertEqual(ex.sharp_time('H:i', '2024-03-08T14:30'), '14:30')

    def test_trailing_z_is_accepted(self):
        self.assertEqual(ex.sharp_time('Y-m-d', '2024-03-08T00:00Z'), '2024-03-08')

    def test_mediawiki_fourteen_digit_timestamp(self):
        self.assertEqual(ex.sharp_time('Y-m-d H:i:s', '20240308143005'),
                         '2024-03-08 14:30:05')

    def test_empty_timestamp_means_now(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertEqual(ex.sharp_time('Y', ''), '%04d' % now.year)

    def test_relative_offset_moves_from_now(self):
        later = int(ex.sharp_time('U', '+24hours'))
        now = int(ex.sharp_time('U', ''))
        self.assertAlmostEqual(later - now, 86400, delta=5)

    def test_negative_relative_offset(self):
        earlier = int(ex.sharp_time('U', '-1day'))
        now = int(ex.sharp_time('U', ''))
        self.assertAlmostEqual(now - earlier, 86400, delta=5)

    def test_relative_units_without_a_fixed_length_are_rejected(self):
        # A month is 28 to 31 days; picking one would put a silently
        # wrong date into whatever comparison asked for it.
        self.assertEqual(ex.sharp_time('Y', '+1month'), ex._SHARP_EXPR_ERROR_SPAN)


class InvalidTimestampTests(unittest.TestCase):

    def test_unparseable_text_gives_the_error_span(self):
        self.assertEqual(ex.sharp_time('Y', 'not a date'), ex._SHARP_EXPR_ERROR_SPAN)

    def test_well_formed_but_impossible_date_gives_the_error_span(self):
        self.assertEqual(ex.sharp_time('Y', '2024-02-31'), ex._SHARP_EXPR_ERROR_SPAN)
        self.assertEqual(ex.sharp_time('Y', '2024-13-01'), ex._SHARP_EXPR_ERROR_SPAN)

    def test_trailing_junk_after_a_date_gives_the_error_span(self):
        # The shape date templates produce when they concatenate: a
        # date with a stray extra component on the end.
        self.assertEqual(ex.sharp_time('Y', '2024-03-08-1'), ex._SHARP_EXPR_ERROR_SPAN)

    def test_the_error_span_is_what_iferror_detects(self):
        self.assertEqual(expand('{{#iferror:{{#time:Y|bogus}}|invalid|valid}}'), 'invalid')

    def test_a_good_date_does_not_trip_iferror(self):
        self.assertEqual(expand('{{#iferror:{{#time:Y|2024-03-08}}|invalid|valid}}'),
                         'valid')

    def test_the_error_span_leaves_no_visible_text(self):
        # span is in ignoredTags and the span has no content, so a
        # rejected date contributes nothing to the article.
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                 templates={}, templatePrefix='Template:')
        result = '\n'.join(extractor.clean_text('前{{#time:Y|bogus}}後',
                                                expand_templates=True))
        self.assertIn('前後', result)
        self.assertNotIn('error', result)


class TimelTests(unittest.TestCase):

    def test_timel_renders_the_same_as_time(self):
        # Local time would need the source wiki's configured timezone;
        # both work in UTC, which is what MediaWiki stores.
        self.assertEqual(expand('{{#timel:Y-m-d|2024-03-08}}'), '2024-03-08')


class DateValidationChainTests(unittest.TestCase):
    """The two shapes citation templates actually use #time for."""

    def test_round_trip_switch_recognizes_a_well_formed_date(self):
        # Template:ISO date/ymd compares a date against its own
        # reformatting to decide whether it parsed.
        self.assertEqual(
            expand('{{#switch:2024-03-08|{{#time:Y-m-d|2024-03-08}}=ok|#default=bad}}'),
            'ok')

    def test_round_trip_switch_rejects_a_malformed_date(self):
        self.assertEqual(
            expand('{{#switch:2024-99-99|{{#time:Y-m-d|2024-99-99}}=ok|#default=bad}}'),
            'bad')

    def test_unix_timestamp_reaches_ifexpr_as_a_number(self):
        # 979516800 is 2001-01-15, the cutoff these templates use to
        # reject an access date from before Wikipedia existed.
        self.assertEqual(
            expand('{{#ifexpr:{{#time:U|1999-01-01}} < 979516800|too early|fine}}'),
            'too early')
        self.assertEqual(
            expand('{{#ifexpr:{{#time:U|2024-03-08}} < 979516800|too early|fine}}'),
            'fine')

    def test_future_date_check_against_a_relative_offset(self):
        self.assertEqual(
            expand('{{#ifexpr:{{#time:U|2024-03-08}} >= {{#time:U|+24hours}}'
                   '|future|past}}'),
            'past')


class RegistrationTests(unittest.TestCase):

    def test_time_and_timel_are_dispatched_to_the_implementation(self):
        self.assertIs(ex.parserFunctions['#time'], ex.sharp_time)
        self.assertIs(ex.parserFunctions['#timel'], ex.sharp_time)

    def test_time_is_not_a_lazy_parser_function(self):
        # Its arguments are data it reads, so they arrive expanded.
        self.assertNotIn('#time', ex.lazyParserFunctions)

    def test_timestamp_argument_is_expanded_before_parsing(self):
        self.assertEqual(expand('{{#time:Y|{{Date}}}}', {'Template:Date': '2024-03-08'}),
                         '2024')


if __name__ == '__main__':
    unittest.main()
