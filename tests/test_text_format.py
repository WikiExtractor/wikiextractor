"""
Tests for the --text output format, --discard_empty flag, and the
--json/--text mutual exclusivity added in the "Add options for a bare
text format & removing empty documents" change.

Background
----------
wikiextractor's default output wraps each article in a
<doc id="..." url="..." title="...">...</doc> XML-style header/footer.
--json instead emits one JSON object per line. --text is a third,
simpler option: just the body text (the same `text` list used by the
other two formats), with no header/footer/metadata at all -- intended
for corpus-building use cases (e.g. char-LM training data) where the
wrapper is pure noise. --discard_empty skips writing anything at all
for articles whose body extracts to nothing (redirects, category-only
stubs, etc.), and applies uniformly across all three output formats
(the check happens before the format branch, not inside it).

Run with:
    python -m unittest tests.test_text_format -v
or, from the tests/ directory:
    python -m unittest test_text_format -v
"""

import json
import subprocess
import sys
import unittest
from io import StringIO

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class TextFormatTestCase(unittest.TestCase):
    """Base class that resets wikiextractor's module-level template state
    and output-format flags before each test, so tests don't leak state
    into one another.
    """

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"
        # Reset all three output-format flags to their defaults; each
        # test sets only the ones it needs.
        ex.Extractor.to_json = False
        ex.Extractor.to_text = False
        ex.Extractor.discard_empty = False

    def make_extractor(self, article_text, article_id=1, title="Test Article"):
        return Extractor(article_id, str(article_id),
                          f"https://test.wikipedia.org/wiki?curid={article_id}",
                          title, [article_text])

    def extract_output(self, article_text, **flags):
        for name, value in flags.items():
            setattr(ex.Extractor, name, value)
        extractor = self.make_extractor(article_text)
        out = StringIO()
        extractor.extract(out, html_safe=True)
        return out.getvalue()


class TextFormatOutputTests(TextFormatTestCase):
    """--text should contain the article body and nothing else -- no
    <doc> wrapper, no XML/JSON metadata.
    """

    def test_text_format_has_no_doc_tags(self):
        article_text = "This is a plain paragraph of article text."
        output = self.extract_output(article_text, to_text=True)

        self.assertNotIn("<doc", output)
        self.assertNotIn("</doc>", output)
        self.assertIn("This is a plain paragraph of article text.", output)

    def test_doc_format_still_has_tags_by_default(self):
        # Regression check: adding the --text branch must not disturb the
        # default (no flags set) <doc> format.
        article_text = "This is a plain paragraph of article text."
        output = self.extract_output(article_text)  # all flags default False

        self.assertIn("<doc", output)
        self.assertIn("</doc>", output)
        self.assertIn("This is a plain paragraph of article text.", output)

    def test_json_format_unaffected_by_text_option(self):
        # Regression check: --json must still produce valid JSON with no
        # <doc> tags, unchanged by the new --text branch existing.
        article_text = "This is a plain paragraph of article text."
        output = self.extract_output(article_text, to_json=True)

        self.assertNotIn("<doc", output)
        data = json.loads(output)
        self.assertIn("text", data)
        self.assertIn("This is a plain paragraph of article text.", data["text"])


class DiscardEmptyTests(TextFormatTestCase):
    """--discard_empty should skip writing anything for articles whose
    body has no real (non-whitespace) content -- redirects and
    category-only stubs correctly produced clean_text() == [] from the
    start, but a whitespace-only body (e.g. clean_text() returning
    [' ', ' ']) was NOT caught by the original 'not text' check, since a
    non-empty list of blank strings is still truthy. The fix checks
    `not any(t.strip() for t in text)` instead, which covers both cases
    while still preserving articles that have real content alongside
    some blank paragraphs. This must apply the same way regardless of
    output format.
    """

    REDIRECT_BODY = "#REDIRECT [[Other Page]]"
    WHITESPACE_ONLY_BODY = "   \n\n  "
    MIXED_BODY = "\n\n\nActual content here.\n\n\n"
    NORMAL_BODY = "A real article with actual content in it."

    def test_discard_empty_skips_redirect_in_text_format(self):
        output = self.extract_output(self.REDIRECT_BODY,
                                      to_text=True, discard_empty=True)
        self.assertEqual(output, "")

    def test_discard_empty_skips_redirect_in_doc_format(self):
        output = self.extract_output(self.REDIRECT_BODY,
                                      discard_empty=True)  # default doc format
        self.assertEqual(output, "")

    def test_discard_empty_skips_redirect_in_json_format(self):
        output = self.extract_output(self.REDIRECT_BODY,
                                      to_json=True, discard_empty=True)
        self.assertEqual(output, "")

    def test_discard_empty_skips_whitespace_only_body(self):
        # clean_text() returns a non-empty list of blank strings here
        # (e.g. [' ', ' ']), not [] -- this is the case the original
        # 'not text' check missed.
        output = self.extract_output(self.WHITESPACE_ONLY_BODY,
                                      to_text=True, discard_empty=True)
        self.assertEqual(output, "")

    def test_discard_empty_preserves_body_with_some_blank_paragraphs(self):
        # A body with blank paragraphs AND real content must NOT be
        # discarded -- only bodies with zero real content anywhere.
        output = self.extract_output(self.MIXED_BODY,
                                      to_text=True, discard_empty=True)
        self.assertIn("Actual content here.", output)

    def test_discard_empty_does_not_affect_normal_articles(self):
        output = self.extract_output(self.NORMAL_BODY,
                                      to_text=True, discard_empty=True)
        self.assertIn("A real article with actual content in it.", output)

    def test_without_discard_empty_flag_redirect_still_writes_something(self):
        # discard_empty is opt-in: without it, a redirect/empty article
        # should still produce the usual (near-empty-bodied) output
        # rather than being silently skipped.
        output = self.extract_output(self.REDIRECT_BODY,
                                      to_text=True, discard_empty=False)
        self.assertNotEqual(output, "")


class MutuallyExclusiveFlagsTests(unittest.TestCase):
    """--json and --text are declared as a mutually exclusive argparse
    group; passing both should be rejected at the CLI level before any
    input file is even opened.
    """

    def test_json_and_text_together_rejected_by_cli(self):
        # argparse validates mutually-exclusive groups during
        # parse_args(), which happens before the (nonexistent) input
        # file is ever opened -- so this doesn't need a real dump file.
        result = subprocess.run(
            [sys.executable, "-m", "wikiextractor.WikiExtractor",
             "--json", "--text", "nonexistent_input_file.xml"],
            cwd="..", capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with argument", result.stderr)


if __name__ == '__main__':
    unittest.main()
