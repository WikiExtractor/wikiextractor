"""
Tests for three tags added to extract.py's discardElements/ignoredTags/
selfClosingTags after being confirmed against real data:

- inputbox (discardElements): a MediaWiki extension tag for create-page
  widgets. Confirmed via a real Saraiki (SKR) article -- its own text
  never mentions "inputbox" at all, but a template it calls
  (سانچہ:نیا ورقہ, "Template:New Page") has it baked directly into its
  definition. Its content (form field directives like "type=create",
  "width=40") isn't natural-language prose at all, so it's discarded
  along with the tag, same category as table/gallery/form.

- bdi (ignoredTags): bidirectional text isolation, the standard way
  editors mark an embedded LTR term (e.g. an English name) inside RTL
  prose. Confirmed occurring 91 times in real Urdu (UR) data. The
  wrapped text is real, meaningful content, so only the tag is
  stripped, not the content -- same category as poem/abbr/span.

- section (BOTH selfClosingTags AND ignoredTags -- confirmed, via
  MediaWiki's own LabeledSectionTransclusion documentation, that this
  tag genuinely has two different, incompatible usages sharing one tag
  name):
    1. <section begin="x" />...<section end="x" /> -- the standard,
       documented form. begin and end are each independently
       self-closing markers, "not normal XML open/close tags" per
       MediaWiki's own docs -- this explains a real, confirmed
       observation directly: 1439 occurrences of "<section" with zero
       matching "</section>" in real Urdu data, which is completely
       expected behavior for this form, not malformed source data.
    2. <section>content</section> -- a rarer, content-wrapping form
       (confirmed 8 times in real Saraiki data) that MediaWiki's own
       docs describe as invalid for this specific extension, but which
       real wikitext evidently still contains sometimes regardless.
  Since both forms share the tag name "section", it needs to be
  registered in both selfClosingTags (to correctly discard each
  begin=/end= marker as its own, contentless unit) AND ignoredTags (to
  correctly preserve real, wrapped content in the rarer form) -- the
  same "dual registration" pattern already used elsewhere in this
  codebase for tags with more than one legitimate shape (e.g. ref in
  both selfClosingTags and discardElements; div in both ignoredTags
  and discardElements).

Run with:
    python -m unittest tests.test_new_tags -v
or, from the tests/ directory:
    python -m unittest test_new_tags -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class NewTagsTestCase(unittest.TestCase):

    def setUp(self):
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        ex.redirects.clear()

    def get_result(self, text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1", "Test", [text])
        return extractor.clean_text(text, expand_templates=True)


class InputboxTests(NewTagsTestCase):

    def test_content_discarded(self):
        text = 'before<inputbox>type=create\nwidth=40\nbgcolor=#F0F8FF</inputbox>after'
        result = self.get_result(text)
        self.assertEqual(result, ['beforeafter'])

    def test_real_world_case(self):
        # The exact real case this was found on: a template's own
        # definition contains this directly.
        text = ('word<inputbox> type=create\n width=40\n bgcolor=#F0F8FF\n'
                ' preload=Template:Standard content for new page\n'
                ' buttonlabel= ورقہ بݨاؤ\n</inputbox>word')
        result = self.get_result(text)
        self.assertEqual(result, ['wordword'])


class BdiTests(NewTagsTestCase):

    def test_content_preserved(self):
        text = 'RTL prose <bdi>English Term</bdi> more RTL prose'
        result = self.get_result(text)
        self.assertEqual(result, ['RTL prose English Term more RTL prose'])

    def test_tag_itself_not_visible_in_output(self):
        text = '<bdi>content</bdi>'
        result = self.get_result(text)
        self.assertNotIn('bdi', ''.join(result))


class SectionTests(NewTagsTestCase):
    """Both forms need to work correctly, since they share one tag name."""

    def test_begin_end_markers_the_documented_mediawiki_form(self):
        # The exact example from MediaWiki's own
        # Extension:Labeled_Section_Transclusion documentation.
        text = '<section begin="chapter1" />this is chapter 1<section end="chapter1" />'
        result = self.get_result(text)
        self.assertEqual(result, ['this is chapter 1'])

    def test_begin_end_markers_do_not_require_pairing_in_one_call(self):
        # Real content between two markers that belong to DIFFERENT,
        # unrelated sections should still be preserved untouched --
        # each self-closing marker is independent, not a pair that
        # needs to "match" the other by name.
        text = ('<section begin="a" />first section text<section end="a" />'
                'middle prose, not inside any section'
                '<section begin="b" />second section text<section end="b" />')
        result = self.get_result(text)
        self.assertEqual(result, ['first section textmiddle prose, not inside any sectionsecond section text'])

    def test_wrapping_form_content_preserved(self):
        text = 'word<section>real content here</section>word'
        result = self.get_result(text)
        self.assertEqual(result, ['wordreal content hereword'])

    def test_unnamed_bare_wrapping_form(self):
        text = '<section>bare content, no attributes</section>'
        result = self.get_result(text)
        self.assertEqual(result, ['bare content, no attributes'])


if __name__ == '__main__':
    unittest.main()
