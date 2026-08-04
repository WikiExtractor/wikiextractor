"""
Tests for adding 'includeonly' and 'categorytree' to discardElements.

Two independent gaps, both found on real pnb.wikipedia.org pages once
the <ns> == '0' fix (see test_colon_in_title.py) started correctly
including ns=0 pages whose title merely looks like it belongs to
another namespace, surfacing pages whose content nobody had previously
seen extracted at all:

1. includeonly (id 38683, "سچہ:·"): the entire visible body was just
   "<includeonly> · </includeonly>". includeonly is the direct
   counterpart of noinclude -- its content is only ever meant to be
   visible when the page is TRANSCLUDED elsewhere, the opposite of
   noinclude, and never on a direct/standalone view, which is the
   only context a regular article is ever extracted in. noinclude was
   already in discardElements (correctly discarding tag and content
   together for a regular article); includeonly had no equivalent
   entry at all, so its tags fell through to generic HTML-escaping
   while its content survived untouched.

2. categorytree (id 40471, "بوآ: چترال"): raw
   "<categorytree>ضلع چترال</categorytree>" markup leaked through
   verbatim (also HTML-escaped, for the same reason). categorytree is
   a MediaWiki extension tag that dynamically renders a live category
   hierarchy from the wiki's own database at view time -- nothing
   meaningful for a static extractor to produce from it, the same
   category as gallery/timeline, which were already handled.

Both are straightforward discardElements additions -- ordinary matched
open/close pairs, not the unmatched-tag edge case test_noinclude_removal.py
covers for noinclude specifically.

Run with:
    python -m unittest tests.test_discard_elements -v
or, from the tests/ directory:
    python -m unittest test_discard_elements -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class DiscardElementsTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class IncludeonlyTests(DiscardElementsTestCase):

    def test_real_pnb_article_shape(self):
        # Reconstruction of the real case: an article whose entire
        # body is just includeonly-wrapped content. Should extract to
        # nothing, not leak the tag text with the content intact.
        text = "<includeonly> · </includeonly>"
        result = self.get_result(text)
        self.assertEqual(result, [])

    def test_content_discarded_alongside_real_prose(self):
        text = "Real content before.<includeonly>hidden on direct view</includeonly>Real content after."
        result = self.get_result(text)
        self.assertEqual(result, ["Real content before.Real content after."])
        self.assertNotIn("hidden on direct view", result[0] if result else "")

    def test_case_insensitive(self):
        text = "Before.<IncludeOnly>discard me</IncludeOnly>After."
        result = self.get_result(text)
        self.assertEqual(result, ["Before.After."])


class CategorytreeTests(DiscardElementsTestCase):

    def test_real_pnb_article_shape(self):
        # Reconstruction of the real case: the categorytree tag and
        # its content are discarded, but the OTHER (unrelated, already
        # malformed in the source) fragments on neighboring lines are
        # left alone -- this fix only addresses the categorytree leak
        # specifically, not other pre-existing wikitext issues.
        text = ("[ تازہ انتخابات ویکھو]\n"
                "<categorytree>ضلع چترال</categorytree>\n"
                " کیہ نيں؟ | |")
        result = self.get_result(text)
        full = '\n'.join(result)
        self.assertNotIn("categorytree", full)
        self.assertNotIn("ضلع چترال", full)
        self.assertIn("تازہ انتخابات ویکھو", full)
        self.assertIn("کیہ نيں", full)

    def test_case_insensitive(self):
        text = "Before.<CategoryTree>Some Category</CategoryTree>After."
        result = self.get_result(text)
        self.assertEqual(result, ["Before.After."])


if __name__ == '__main__':
    unittest.main()
