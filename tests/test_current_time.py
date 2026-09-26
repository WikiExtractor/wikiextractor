"""
Tests for the extraction time: one moment the whole run treats as
"now", which the CURRENT* variables report and which {{#time:}}
resolves an empty or relative timestamp against.

Two properties, and the second is the reason it exists:

UTC. MediaWiki reports the CURRENT* variables in the wiki's own
timezone -- UTC for Wikimedia -- and offers the LOCAL* family for a
local clock. {{#time:}} works in UTC too, so the two agree with each
other whatever timezone the extraction runs in.

One moment for the whole run. Every Extractor is handed the same
value, so page one and page ten thousand report the same second no
matter how long the run takes, and two runs of the same dump with the
same pin are byte-identical. WikiExtractor's main() resolves it once
from --current-time, then SOURCE_DATE_EPOCH, then the wall clock, and
passes it to every worker.

Run with:
    python -m unittest tests.test_current_time -v
or, from the tests/ directory:
    python -m unittest test_current_time -v
"""

import datetime
import os
import subprocess
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex
import wikiextractor.WikiExtractor as we


PINNED = datetime.datetime(2026, 9, 1, 12, 34, 56, tzinfo=datetime.timezone.utc)


def make_extractor(current_time=None, title='Test Article'):
    kwargs = {} if current_time is None else {'currentTime': current_time}
    return ex.Extractor(1, '1', 'https://x', title, [], templates={},
                        templatePrefix='Template:', **kwargs)


def expand(wikitext, current_time=None):
    extractor = make_extractor(current_time)
    return '\n'.join(extractor.clean_text(wikitext, expand_templates=True))


class ResolveCurrentTimeTests(unittest.TestCase):

    def setUp(self):
        self._saved_epoch = os.environ.pop('SOURCE_DATE_EPOCH', None)

    def tearDown(self):
        os.environ.pop('SOURCE_DATE_EPOCH', None)
        if self._saved_epoch is not None:
            os.environ['SOURCE_DATE_EPOCH'] = self._saved_epoch

    def test_a_compact_date_resolves_to_midnight_utc(self):
        self.assertEqual(we.resolve_current_time('20260901'),
                         datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc))

    def test_a_hyphenated_date_resolves_to_midnight_utc(self):
        self.assertEqual(we.resolve_current_time('2026-09-01'),
                         datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc))

    def test_a_full_timestamp_resolves(self):
        self.assertEqual(we.resolve_current_time('2026-09-01T12:34:56Z'), PINNED)

    def test_a_fourteen_digit_timestamp_resolves(self):
        self.assertEqual(we.resolve_current_time('20260901123456'), PINNED)

    def test_source_date_epoch_is_honored(self):
        os.environ['SOURCE_DATE_EPOCH'] = str(int(PINNED.timestamp()))
        self.assertEqual(we.resolve_current_time(), PINNED)

    def test_an_explicit_spec_beats_source_date_epoch(self):
        os.environ['SOURCE_DATE_EPOCH'] = '0'
        self.assertEqual(we.resolve_current_time('2026-09-01T12:34:56Z'), PINNED)

    def test_the_wall_clock_is_the_last_resort_and_is_aware_utc(self):
        resolved = we.resolve_current_time()
        self.assertIsNotNone(resolved.tzinfo)
        self.assertEqual(resolved.utcoffset(), datetime.timedelta(0))

    def test_an_unreadable_spec_is_rejected(self):
        with self.assertRaises(ValueError):
            we.resolve_current_time('not a time')

    def test_an_impossible_date_is_rejected(self):
        with self.assertRaises(ValueError):
            we.resolve_current_time('20260231')

    def test_an_unreadable_source_date_epoch_is_rejected(self):
        os.environ['SOURCE_DATE_EPOCH'] = 'banana'
        with self.assertRaises(ValueError):
            we.resolve_current_time()

    def test_an_empty_source_date_epoch_falls_through_to_the_clock(self):
        os.environ['SOURCE_DATE_EPOCH'] = ''
        self.assertIsNotNone(we.resolve_current_time())


class CurrentVariableTests(unittest.TestCase):

    def test_the_variables_report_the_extraction_time(self):
        extractor = make_extractor(PINNED)
        extractor.clean_text('', expand_templates=True)
        self.assertEqual(extractor.magicWords['CURRENTYEAR'], '2026')
        self.assertEqual(extractor.magicWords['CURRENTMONTH'], '09')
        self.assertEqual(extractor.magicWords['CURRENTDAY'], '1')
        self.assertEqual(extractor.magicWords['CURRENTHOUR'], '12')
        self.assertEqual(extractor.magicWords['CURRENTTIME'], '12:34')

    def test_the_variables_expand_in_wikitext(self):
        self.assertEqual(
            expand('{{CURRENTYEAR}}-{{CURRENTMONTH}}-{{CURRENTDAY2}}', PINNED),
            '2026-09-01')

    def test_they_are_utc_not_the_local_clock(self):
        # A pinned UTC instant renders the same wherever the machine
        # thinks it is; the LOCAL* family is the local-clock one.
        for tz in ('UTC', 'America/Los_Angeles', 'Pacific/Kiritimati'):
            with self.subTest(tz=tz):
                out = subprocess.run(
                    [sys.executable, '-c',
                     'import wikiextractor.extract as ex, datetime;'
                     'p=datetime.datetime(2026,9,1,12,34,56,tzinfo=datetime.timezone.utc);'
                     'e=ex.Extractor(1,"1","x","T",[],templates={},currentTime=p);'
                     'e.clean_text("", expand_templates=True);'
                     'print(e.magicWords["CURRENTDAY2"], e.magicWords["CURRENTTIME"])'],
                    cwd='..', capture_output=True, text=True,
                    env=dict(os.environ, TZ=tz))
                self.assertEqual(out.stdout.strip(), '01 12:34')


class SharpTimeUsesTheExtractionTimeTests(unittest.TestCase):

    def test_an_empty_timestamp_is_the_extraction_time(self):
        self.assertEqual(expand('{{#time:Y-m-d H:i:s}}', PINNED), '2026-09-01 12:34:56')

    def test_a_relative_timestamp_offsets_from_the_extraction_time(self):
        self.assertEqual(expand('{{#time:Y-m-d|+24hours}}', PINNED), '2026-09-02')
        self.assertEqual(expand('{{#time:Y-m-d|-1day}}', PINNED), '2026-08-31')

    def test_timel_uses_it_too(self):
        self.assertEqual(expand('{{#timel:Y-m-d}}', PINNED), '2026-09-01')

    def test_an_absolute_timestamp_is_unaffected(self):
        self.assertEqual(expand('{{#time:Y-m-d|1999-12-31}}', PINNED), '1999-12-31')

    def test_the_variables_and_sharp_time_agree(self):
        result = expand('{{CURRENTYEAR}}-{{CURRENTMONTH}}-{{CURRENTDAY2}} '
                        'and {{#time:Y-m-d}}', PINNED)
        self.assertEqual(result, '2026-09-01 and 2026-09-01')

    def test_the_access_date_check_citations_rely_on(self):
        # Template:Accessdate/core flags an access date more than a day
        # ahead of now; pinning makes that verdict a property of the
        # dump rather than of when it was extracted.
        future = ('{{#ifexpr:{{#time:U|2026-09-30}} >= {{#time:U|+24hours}}'
                  '|future|past}}')
        past = ('{{#ifexpr:{{#time:U|2026-08-01}} >= {{#time:U|+24hours}}'
                '|future|past}}')
        self.assertEqual(expand(future, PINNED), 'future')
        self.assertEqual(expand(past, PINNED), 'past')

    def test_sharp_time_called_directly_still_works(self):
        # No extractor in play: falls back to the module-level value.
        self.assertEqual(ex.sharp_time('Y-m-d', '2026-09-01'), '2026-09-01')
        self.assertEqual(ex.sharp_time('Y', '', now=PINNED), '2026')


class OneMomentPerRunTests(unittest.TestCase):

    def test_every_extractor_given_the_same_value_reports_it(self):
        for page in range(3):
            extractor = make_extractor(PINNED, title='Page %d' % page)
            with self.subTest(page=page):
                self.assertEqual(expand('{{#time:H:i:s}}', PINNED), '12:34:56')
                extractor.clean_text('', expand_templates=True)
                self.assertEqual(extractor.magicWords['CURRENTTIME'], '12:34')

    def test_an_unconfigured_extractor_still_gets_a_stable_moment(self):
        # Two Extractors built at different instants share the
        # process-wide fallback rather than drifting apart.
        first = make_extractor()
        second = make_extractor()
        self.assertEqual(first.currentTime, second.currentTime)
        self.assertEqual(first.currentTime, ex._FALLBACK_CURRENT_TIME)

    def test_the_fallback_is_aware_utc(self):
        self.assertEqual(ex._FALLBACK_CURRENT_TIME.utcoffset(), datetime.timedelta(0))


class DateVariableFamilyTests(unittest.TestCase):
    """The formats MediaWiki reports, which differ between siblings:
    DAY and MONTH1 are unpadded while DAY2 and MONTH are not, and TIME
    carries no seconds."""

    # 2026-03-07 is a Saturday, in ISO week 10, with a single-digit
    # day and month so that padding is visible.
    MARCH = datetime.datetime(2026, 3, 7, 5, 4, 3, tzinfo=datetime.timezone.utc)

    def test_the_whole_family(self):
        self.assertEqual(ex.dateVariables(self.MARCH), {
            'YEAR': '2026', 'MONTH': '03', 'MONTH1': '3',
            'DAY': '7', 'DAY2': '07', 'DOW': '6',
            'HOUR': '05', 'TIME': '05:04', 'WEEK': '10',
            'TIMESTAMP': '20260307050403',
        })

    def test_day_is_unpadded_and_day2_is_padded(self):
        self.assertEqual(expand('{{CURRENTDAY}}/{{CURRENTDAY2}}', self.MARCH), '7/07')

    def test_month1_is_unpadded_and_month_is_padded(self):
        self.assertEqual(expand('{{CURRENTMONTH1}}/{{CURRENTMONTH}}', self.MARCH), '3/03')

    def test_time_carries_no_seconds(self):
        self.assertEqual(expand('{{CURRENTTIME}}', self.MARCH), '05:04')

    def test_day_of_week_counts_sunday_as_zero(self):
        sunday = datetime.datetime(2026, 3, 8, tzinfo=datetime.timezone.utc)
        self.assertEqual(expand('{{CURRENTDOW}}', sunday), '0')
        self.assertEqual(expand('{{CURRENTDOW}}', self.MARCH), '6')

    def test_timestamp_is_the_fourteen_digit_form(self):
        stamp = expand('{{CURRENTTIMESTAMP}}', self.MARCH)
        self.assertEqual(stamp, '20260307050403')
        # and round-trips back through the timestamp parser
        self.assertEqual(ex._parseTimestamp(stamp), self.MARCH)

    def test_local_mirrors_current(self):
        # The wiki's configured timezone is UTC on Wikimedia wikis, so
        # the two families report the same moment.
        for suffix in ex.dateVariables(self.MARCH):
            with self.subTest(suffix=suffix):
                self.assertEqual(expand('{{LOCAL%s}}' % suffix, self.MARCH),
                                 expand('{{CURRENT%s}}' % suffix, self.MARCH))

    def test_the_name_bearing_members_stay_empty(self):
        # Naming a month needs the wiki's language data.
        for name in ('CURRENTMONTHNAME', 'CURRENTDAYNAME', 'CURRENTMONTHABBREV',
                     'LOCALMONTHNAME'):
            with self.subTest(name=name):
                self.assertEqual(expand('a{{%s}}b' % name, self.MARCH), 'ab')


class CompactDateParsingTests(unittest.TestCase):
    """The 8-digit form a Wikimedia dump filename carries."""

    def test_a_compact_date_parses(self):
        self.assertEqual(ex._parseTimestamp('20260901'),
                         datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc))

    def test_it_works_as_a_sharp_time_timestamp(self):
        self.assertEqual(ex.sharp_time('Y-m-d', '20260901'), '2026-09-01')

    def test_an_impossible_compact_date_is_rejected(self):
        self.assertIsNone(ex._parseTimestamp('20260231'))

    def test_a_seven_digit_run_is_not_a_date(self):
        self.assertIsNone(ex._parseTimestamp('2026090'))

    def test_the_fourteen_digit_form_still_parses(self):
        self.assertEqual(ex._parseTimestamp('20260901123456'), PINNED)


class CommandLineTests(unittest.TestCase):
    """argparse validates before the input file is opened, so these
    need no dump."""

    def run_cli(self, *args, **env):
        return subprocess.run(
            [sys.executable, '-m', 'wikiextractor.WikiExtractor'] + list(args)
            + ['nonexistent_input_file.xml'],
            cwd='..', capture_output=True, text=True,
            env=dict(os.environ, **env))

    def test_the_flag_is_accepted(self):
        result = self.run_cli('--current-time', '20260901')
        self.assertNotIn('unrecognized arguments', result.stderr)
        self.assertNotIn('cannot read', result.stderr)

    def test_an_unreadable_value_is_rejected_with_a_usable_message(self):
        result = self.run_cli('--current-time', 'not a time')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot read', result.stderr)

    def test_an_unreadable_source_date_epoch_is_rejected(self):
        result = self.run_cli(SOURCE_DATE_EPOCH='banana')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SOURCE_DATE_EPOCH', result.stderr)


if __name__ == '__main__':
    unittest.main()
