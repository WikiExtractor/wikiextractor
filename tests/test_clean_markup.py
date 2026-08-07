"""
Tests for wikiextractor/clean.py's clean_markup() -- a small,
standalone utility for cleaning a single string of wikimarkup to
plaintext, without going through the full XML-dump pipeline.

Run with:
    python -m unittest tests.test_clean_markup -v
or, from the tests/ directory:
    python -m unittest test_clean_markup -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex
from wikiextractor.clean import clean_markup


class CleanMarkupTests(unittest.TestCase):

    def setUp(self):
        ex.Extractor.templatePrefix = 'Template:'
        # Reset to the real default set before every test, restore
        # after -- don't depend on what state some other test file
        # (e.g. test_article_mode.py) left in this shared list.
        self._orig_ignored_tag_patterns = list(ex.ignored_tag_patterns)

    def tearDown(self):
        ex.ignored_tag_patterns = self._orig_ignored_tag_patterns

    def test_basic_prose_and_bold_markup(self):
        result = list(clean_markup("'''Geography''' is a broad field of study."))
        self.assertEqual(result, ['Geography is a broad field of study.'])

    def test_wikilink_reduced_to_display_text_regardless_of_keep_links(self):
        # keep_links only affects literal HTML <a> tags -- [[wikilinks]]
        # are controlled separately, by Extractor.keepLinks.
        markup = "See [[History of geography|related topics]] for more."
        without = list(clean_markup(markup, keep_links=False))
        with_links = list(clean_markup(markup, keep_links=True))
        self.assertEqual(without, ['See related topics for more.'])
        self.assertEqual(without, with_links)

    def test_keep_links_false_strips_literal_html_anchor_tag(self):
        markup = 'Some text with a literal <a href="http://example.com">HTML link</a> embedded in it.'
        result = list(clean_markup(markup, keep_links=False))
        self.assertEqual(result, ['Some text with a literal HTML link embedded in it.'])

    def test_keep_links_true_preserves_html_anchor_tag_escaped(self):
        # Preserved, but still HTML-escaped (html_safe=True), so it
        # survives as visible text, not a live anchor.
        markup = 'Some text with a literal <a href="http://example.com">HTML link</a> embedded in it.'
        result = list(clean_markup(markup, keep_links=True))
        self.assertEqual(len(result), 1)
        self.assertIn('&lt;a href="http://example.com"&gt;HTML link&lt;/a&gt;', result[0])

    def test_ignore_headers_true_drops_header_lines_by_default(self):
        markup = "== Overview ==\nSome body text."
        result = list(clean_markup(markup))
        self.assertEqual(len(result), 1)
        self.assertNotIn('Overview', result[0])
        self.assertIn('Some body text.', result[0])

    def test_ignore_headers_false_keeps_marked_header_lines(self):
        markup = "== Overview ==\nSome body text."
        result = list(clean_markup(markup, ignore_headers=False))
        headers = [p for p in result if p.startswith('## ')]
        self.assertEqual(len(headers), 1)
        self.assertIn('Overview', headers[0])

    def test_templates_are_never_expanded(self):
        # clean_markup() always calls clean_text() with
        # expand_templates=False -- a template call should survive
        # unexpanded (or vanish if it's the only content on its own
        # line, per test_article_extraction_content.py).
        ex.define_template('Template:Greeting', ['Hello, {{{1}}}!'], {}, {})
        markup = "Intro text. {{Greeting|World}} Trailing text."
        result = list(clean_markup(markup))
        joined = ' '.join(result)
        self.assertNotIn('Hello, World!', joined)
        self.assertIn('Intro text.', joined)
        self.assertIn('Trailing text.', joined)


if __name__ == '__main__':
    unittest.main()
