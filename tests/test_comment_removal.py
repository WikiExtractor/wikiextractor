"""
Tests for a real bug in clean()'s "Collect spans" ordering: the br/hr
line-break substitution (substituteLineBreakTag(), see
test_selfclosing_tags.py) mutates text's length -- a longer tag like
<br clear=all> collapses to a single space -- but it used to run
*after* comment spans had already been collected as absolute
character positions. Once the text shifted from any br/hr
substitution, every span collected before that point held stale
positions that no longer corresponded to where those comments
actually were in the (now shorter) text. dropSpans() then removed the
wrong span of characters entirely, while the real, shifted comment
survived untouched.

Found on a real Urdu Wikipedia article ("محمد علی جناح"/Muhammad Ali
Jinnah, id 1086): the article has 6 HTML comments; two of them,
located after a br/hr substitution earlier in the same article,
survived completely intact into the extracted output (HTML-escaped,
e.g. "&lt;!-- Quoting Fatima Jinnah... --&gt;"), while the other four
were correctly removed.

The fix: substituteLineBreakTag() now runs before any span collection
begins, so every span computed afterward (comments, self-closing
tags, ignored tags) is based on the final, stable text that
dropSpans() will actually operate on.

Run with:
    python -m unittest tests.test_comment_removal -v
or, from the tests/ directory:
    python -m unittest test_comment_removal -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class CommentRemovalTestCase(unittest.TestCase):

    def setUp(self):
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text])
        return extractor.clean_text(article_text, expand_templates=True)


class BasicCommentRemovalTests(CommentRemovalTestCase):
    """Sanity checks for comment removal on its own, unrelated to the
    span-ordering bug -- these should have always passed, and confirm
    the fix didn't disturb ordinary comment handling.
    """

    def test_simple_comment_removed(self):
        text = "Real text before.<!-- a comment -->Real text after."
        result = self.get_result(text)
        self.assertEqual(result, ["Real text before.Real text after."])

    def test_multiline_comment_removed(self):
        text = "Real text before.<!-- a\nmulti-line\ncomment -->Real text after."
        result = self.get_result(text)
        self.assertEqual(result, ["Real text before.Real text after."])

    def test_multiple_independent_comments_all_removed(self):
        text = "<!-- one -->A<!-- two -->B<!-- three -->"
        result = self.get_result(text)
        self.assertEqual(result, ["AB"])


class CommentAfterLineBreakSubstitutionTests(CommentRemovalTestCase):
    """The core regression: a comment positioned after a br/hr
    substitution earlier in the same text must still be correctly
    removed, not survive due to a stale collected span position.
    """

    def test_comment_after_br_still_removed(self):
        # A minimal reproduction of the real bug shape: a <br>
        # (matched and substituted with a space, shortening the text
        # relative to its own length) sits before a comment later in
        # the same text.
        text = "word<br>word<!-- a comment -->after"
        result = self.get_result(text)
        self.assertEqual(result, ["word wordafter"])
        self.assertNotIn("a comment", result[0])
        self.assertNotIn("<!--", result[0])

    def test_comment_after_old_style_br_with_attribute(self):
        # Old-style <br clear=all> (no trailing slash) collapses to a
        # single space -- a LARGER length change than a self-closed
        # <br/>, making any downstream position drift more severe.
        text = "word<br clear=all>word<!-- a comment -->after"
        result = self.get_result(text)
        self.assertEqual(result, ["word wordafter"])
        self.assertNotIn("a comment", result[0])

    def test_multiple_comments_spanning_a_br_substitution(self):
        # A comment BEFORE the br/hr substitution and a comment AFTER
        # it, in the same text -- the one before is unaffected either
        # way; the one after is exactly where the stale-position bug
        # showed up.
        text = "<!-- before -->A<br clear=all>B<!-- after -->C"
        result = self.get_result(text)
        self.assertEqual(result, ["A BC"])
        self.assertNotIn("before", result[0])
        self.assertNotIn("after", result[0])

    def test_realistic_scale_shift_from_many_br_substitutions(self):
        # Reconstruction of the real failure at realistic scale (found
        # on "محمد علی جناح"/Muhammad Ali Jinnah, id 1086 -- two of six
        # comments survived because dropSpans() removed the wrong
        # characters once the text had shifted). A single <br> only
        # shifts positions by a few characters, which can coincidentally
        # still overlap enough of a short comment to remove it anyway --
        # the real article's comments were separated from the nearest
        # br/hr by tens of thousands of characters and several
        # substitutions, so this uses many accumulated old-style
        # <br clear=all> substitutions (each a larger individual shift
        # than a bare <br/>) to reliably reproduce a large enough drift.
        padding_with_brs = "Some unrelated real prose content.<br clear=all> " * 50
        text = (padding_with_brs +
                "text before comment"
                "<!-- Quoting a source, page not specified -->"
                "text after comment.")
        result = self.get_result(text)
        full = result[0] if result else ""
        self.assertNotIn("Quoting a source", full)
        self.assertNotIn("<!--", full)
        self.assertNotIn("&lt;!--", full)


if __name__ == '__main__':
    unittest.main()
