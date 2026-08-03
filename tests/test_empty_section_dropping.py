"""
Tests confirming compact()'s pre-existing "empty sections are discarded"
behavior (see its own docstring: "Deal with headers, lists, empty
sections, residuals of tables") interacts correctly with the
__NOEDITSECTION__ fix.

Background
----------
A section header's text is not written to output immediately -- it's
held in compact()'s `headers` dict and only flushed once a real content
line is found under it (see the `elif len(headers): ... page.append(v)`
branch). If nothing real ever follows a header before the article ends,
the header is simply never flushed -- it's discarded, by design.

Before __NOEDITSECTION__ was added to MagicWords.switches, it survived
in the text as literal, non-blank content, which was enough to fool
compact() into treating an otherwise-genuinely-empty section as
non-empty, flushing its (otherwise pointless) header. After the fix,
__NOEDITSECTION__ correctly reduces to nothing, and a section that has
truly nothing else under it is now correctly recognized as empty and
dropped -- confirmed against a real article ("ٻجھارتاں" / "Riddles" on
Saraiki Wikipedia, id 3675) whose References section contains only a
<references /> marker (nothing to compile, since no <ref> tags appear
earlier in the article), two category links, __FORCETOC__, and
__NOEDITSECTION__ -- i.e. genuinely zero real content.

Run with:
    python -m unittest tests.test_empty_section_dropping -v
or, from the tests/ directory:
    python -m unittest test_empty_section_dropping -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class EmptySectionDroppingTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class RealArticleReproductionTests(EmptySectionDroppingTestCase):
    """The exact structure found on the real article ٻجھارتاں (id 3675,
    Saraiki Wikipedia): a References section containing only a
    <references /> marker, two category links, __FORCETOC__, and
    __NOEDITSECTION__ -- genuinely zero real content underneath.
    """

    def test_references_section_with_only_markers_is_dropped(self):
        article_text = (
            "Some real article content here.\n\n"
            "== حوالے ==\n"
            "<references />\n"
            "[[ونکی:لوک ادب]]\n"
            "[[ونکی:سرائیکی لوک ادب]]\n"
            "__FORCETOC__\n"
            "__NOEDITSECTION__"
        )
        result = self.get_result(article_text)

        self.assertIn("Some real article content here.", result)
        # The empty References header must not appear anywhere in the
        # output -- neither the Arabic-script heading text nor a
        # markdown-style "## " version of it.
        full_text = "\n".join(result)
        self.assertNotIn("حوالے", full_text)


class NonEmptySectionPreservedTests(EmptySectionDroppingTestCase):
    """Contrast cases: a section with genuine content underneath must
    have both its header AND its content preserved.
    """

    def test_section_with_real_prose_is_preserved(self):
        article_text = (
            "Intro paragraph.\n\n"
            "== حوالے ==\n"
            "This is a real citation or note, not just markup.\n"
        )
        result = self.get_result(article_text)
        full_text = "\n".join(result)

        self.assertIn("حوالے", full_text)
        self.assertIn("This is a real citation or note, not just markup.", full_text)

    def test_section_becomes_non_empty_if_real_content_follows_magic_word(self):
        # Same magic word as the dropped case, but with real content
        # after it -- the header must now survive, since the section
        # is genuinely non-empty. This isolates that the behavior is
        # about total emptiness, not merely "a magic word is present".
        article_text = (
            "Intro paragraph.\n\n"
            "== حوالے ==\n"
            "__NOEDITSECTION__\n"
            "A real reference note appears here.\n"
        )
        result = self.get_result(article_text)
        full_text = "\n".join(result)

        self.assertIn("حوالے", full_text)
        self.assertIn("A real reference note appears here.", full_text)
        # The magic word itself must still be gone.
        self.assertNotIn("__NOEDITSECTION__", full_text)

    def test_multiple_sections_only_truly_empty_one_is_dropped(self):
        # A more realistic multi-section article: a real middle section
        # must survive even though the trailing section is empty.
        article_text = (
            "Intro.\n\n"
            "== Middle Section ==\n"
            "Real content in the middle section.\n\n"
            "== حوالے ==\n"
            "__NOEDITSECTION__"
        )
        result = self.get_result(article_text)
        full_text = "\n".join(result)

        self.assertIn("Middle Section", full_text)
        self.assertIn("Real content in the middle section.", full_text)
        self.assertNotIn("حوالے", full_text)


class CategoryLinksAloneTests(EmptySectionDroppingTestCase):
    """Category links are metadata and never count as real section
    content on their own, independent of any magic word.
    """

    def test_section_with_only_category_links_is_dropped(self):
        article_text = (
            "Intro paragraph.\n\n"
            "== حوالے ==\n"
            "[[ونکی:لوک ادب]]\n"
            "[[ونکی:سرائیکی لوک ادب]]"
        )
        result = self.get_result(article_text)
        full_text = "\n".join(result)

        self.assertIn("Intro paragraph.", full_text)
        self.assertNotIn("حوالے", full_text)


if __name__ == '__main__':
    unittest.main()
