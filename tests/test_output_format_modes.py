"""
Tests covering the three mutually-exclusive output formats extract()
can produce, controlled by the Extractor.to_json / Extractor.to_text
class attributes (set from the --json / --text CLI flags; neither set
means the default <doc> format):

  default (neither flag): each article wrapped in
      <doc id="..." url="..." title="...">
      TITLE

      BODY
      </doc>
  --json: one JSON object per article, one line, with five keys --
      id, revid, url, title, text (text is the body ONLY, title is
      its own separate key, not duplicated as the text's first line).
  --text: body only, no id/url/title metadata of any kind, no <doc>
      tags -- just the article's own body text, followed by a blank
      line and a further trailing newline (three newlines total)
      separating one article's output from the next.

Also covers --discard_empty's interaction with each mode (an article
whose body is empty writes nothing at all when set, regardless of
which output mode is active -- versus each mode's own, different
"empty but not discarded" shape otherwise), and confirms --json and
--text are enforced as mutually exclusive at the CLI argument-parsing
level.

Run with:
    python -m unittest tests.test_output_format_modes -v
or, from the tests/ directory:
    python -m unittest test_output_format_modes -v
"""

import io
import json
import subprocess
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class OutputFormatModeTestCase(unittest.TestCase):

    def setUp(self):
        self.templatePrefix = 'Template:'

    def make_extractor(self, body_text, page_id=1, revid='100', title='My Title',
                        to_json=False, to_text=False, discard_empty=False):
        return ex.Extractor(page_id, revid, 'https://test.wikipedia.org/wiki',
                             title, [body_text], templatePrefix=self.templatePrefix,
                             to_json=to_json, to_text=to_text, discard_empty=discard_empty)

    def run_extract(self, body_text, to_json=False, to_text=False,
                     discard_empty=False, **kwargs):
        extractor = self.make_extractor(body_text, to_json=to_json, to_text=to_text,
                                         discard_empty=discard_empty, **kwargs)
        buf = io.StringIO()
        extractor.extract(buf)
        return buf.getvalue()


class DefaultDocFormatTests(OutputFormatModeTestCase):

    def test_wraps_body_in_doc_tags_with_title_and_metadata(self):
        result = self.run_extract('Some body text here.',
                                   page_id=42, revid='999', title='My Title')
        self.assertTrue(result.startswith(
            '<doc id="42" url="https://test.wikipedia.org/wiki?curid=42" title="My Title">\n'))
        self.assertTrue(result.endswith('</doc>\n'))

    def test_title_appears_as_first_line_of_body(self):
        result = self.run_extract('Some body text here.', title='My Title')
        # Title, then a blank line, then the real body -- matches the
        # real <doc id="..."> ... \nTITLE\n\nBODY ... shape used
        # throughout every real extraction sample in this project.
        self.assertIn('title="My Title">\nMy Title\n\nSome body text here.', result)

    def test_empty_article_without_discard_empty_still_gets_doc_wrapper(self):
        result = self.run_extract('', title='Empty Article')
        self.assertIn('<doc id="1"', result)
        self.assertIn('Empty Article', result)
        self.assertTrue(result.endswith('</doc>\n'))


class JsonFormatTests(OutputFormatModeTestCase):

    def test_produces_one_json_object_with_expected_keys(self):
        result = self.run_extract('Some body text here.', to_json=True,
                                   page_id=42, revid='999', title='My Title')
        self.assertTrue(result.endswith('\n'))
        data = json.loads(result)
        self.assertEqual(set(data.keys()), {'id', 'revid', 'url', 'title', 'text'})
        self.assertEqual(data['id'], 42)
        self.assertEqual(data['revid'], '999')
        self.assertEqual(data['title'], 'My Title')
        self.assertEqual(data['url'], 'https://test.wikipedia.org/wiki?curid=42')
        self.assertEqual(data['text'], 'Some body text here.')

    def test_title_is_a_separate_field_not_duplicated_in_text(self):
        # Unlike default mode, the title must NOT show up as the
        # text field's own first line.
        result = self.run_extract('Some body text here.', to_json=True, title='My Title')
        data = json.loads(result)
        self.assertFalse(data['text'].startswith('My Title'))
        self.assertEqual(data['text'], 'Some body text here.')

    def test_no_doc_tags_present_anywhere(self):
        result = self.run_extract('Some body text here.', to_json=True)
        self.assertNotIn('<doc', result)
        self.assertNotIn('</doc>', result)

    def test_empty_article_without_discard_empty_still_writes_json_with_empty_text(self):
        result = self.run_extract('', to_json=True, title='Empty Article')
        data = json.loads(result)
        self.assertEqual(data['text'], '')
        self.assertEqual(data['title'], 'Empty Article')


class TextFormatTests(OutputFormatModeTestCase):

    def test_body_only_no_title_no_doc_tags(self):
        result = self.run_extract('Some body text here.', to_text=True, title='My Title')
        self.assertNotIn('<doc', result)
        self.assertNotIn('My Title', result)
        self.assertTrue(result.startswith('Some body text here.'))

    def test_articles_separated_by_a_blank_line(self):
        # body + '\n' (join) + '\n\n\n' (separator) -- three newlines
        # total after the body, i.e. the body followed by two blank
        # lines, matching how consecutive --text articles are meant
        # to be told apart with no other markup available at all.
        result = self.run_extract('Some body text here.', to_text=True)
        self.assertEqual(result, 'Some body text here.\n\n\n')

    def test_empty_article_without_discard_empty_writes_only_the_separator(self):
        result = self.run_extract('', to_text=True, title='Empty Article')
        self.assertNotIn('Empty Article', result)
        self.assertEqual(result, '\n\n\n')


class DiscardEmptyTests(OutputFormatModeTestCase):
    """--discard_empty means an article with no real body content
    writes nothing at all -- in every output mode, not just the
    default one.
    """

    def test_default_mode_discards_empty_article_entirely(self):
        result = self.run_extract('', discard_empty=True, title='Empty Article')
        self.assertEqual(result, '')

    def test_json_mode_discards_empty_article_entirely(self):
        result = self.run_extract('', to_json=True, discard_empty=True,
                                   title='Empty Article')
        self.assertEqual(result, '')

    def test_text_mode_discards_empty_article_entirely(self):
        result = self.run_extract('', to_text=True, discard_empty=True,
                                   title='Empty Article')
        self.assertEqual(result, '')

    def test_discard_empty_does_not_affect_non_empty_articles(self):
        for to_json, to_text in [(False, False), (True, False), (False, True)]:
            with self.subTest(to_json=to_json, to_text=to_text):
                result = self.run_extract('Real content.', to_json=to_json,
                                           to_text=to_text, discard_empty=True)
                self.assertNotEqual(result, '')


class MutualExclusivityTests(unittest.TestCase):
    """--json and --text are declared in the same argparse
    mutually-exclusive group -- confirmed at the actual CLI level,
    not just by reading the argparse setup.
    """

    def test_json_and_text_together_is_rejected_by_the_cli(self):
        result = subprocess.run(
            [sys.executable, '-m', 'wikiextractor.WikiExtractor',
             '--json', '--text', '-o', '/tmp/_never_used', '/tmp/_nonexistent_input.xml'],
            cwd='..', capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not allowed with argument', result.stderr)


if __name__ == '__main__':
    unittest.main()
