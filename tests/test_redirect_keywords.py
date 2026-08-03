"""
Tests for define_template()'s redirect detection in extract.py.

Real MediaWiki's redirect magic word is localized per-wiki
(each wiki's own content language has its own translation,
e.g. Sindhi uses "چوريو" rather than "REDIRECT"), separate
from the interface language.
define_template() previously matched only the literal English keyword
-- a redirect page on a non-English wiki was never recognized as a
redirect at all, and its entire (often stale, pre-redirect leftover)
body text got treated as the template's real content instead.

Confirmed on a real Sindhi Wikipedia dump: a template ("سانچو:حوالا",
Template:References) starting with "#چوريو [[سانچو:حوالو]]" -- was
never resolved as a redirect, and its leftover body (a "reflist" CSS
construct) leaked a "list-style-type: decimal;" fragment into
extracted article text wherever the template was called.
Confirmed directly that treating it as a redirect eliminates this.
Two example pages that led to this were in the 2026-07-01 dump,
pages id 1869 and 1907.

Fixed via an extensible redirectKeywords list (starting with
'REDIRECT' and the confirmed 'چوريو'), rather than a single hardcoded
English pattern -- more can be added as they're confirmed on other
wikis, without guessing translations for languages not yet
encountered.

False-positive surface, deliberately narrow and tested explicitly
below: matched via re.match() (position 0 of the template's first
line only, never later in a template's body), requires a real word
boundary after the keyword (so "REDIRECTED" etc. don't match), and
requires an actual wikilink following on that same line.

Run with:
    python -m unittest tests.test_redirect_keywords -v
or, from the tests/ directory:
    python -m unittest test_redirect_keywords -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class RedirectKeywordsTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()


class EnglishRedirectStillWorksTests(RedirectKeywordsTestCase):
    """Regression coverage for the original, pre-existing behavior."""

    def test_uppercase_redirect(self):
        ex.define_template('Template:Foo', ['#REDIRECT [[Template:Bar]]'])
        self.assertEqual(ex.redirects.get('Template:Foo'), 'Template:Bar')
        self.assertNotIn('Template:Foo', ex.templates)

    def test_lowercase_redirect_case_insensitive(self):
        ex.define_template('Template:Foo', ['#redirect [[Template:Bar]]'])
        self.assertEqual(ex.redirects.get('Template:Foo'), 'Template:Bar')

    def test_mixed_case_redirect(self):
        ex.define_template('Template:Foo', ['#Redirect [[Template:Bar]]'])
        self.assertEqual(ex.redirects.get('Template:Foo'), 'Template:Bar')

    def test_normal_non_redirect_template_unaffected(self):
        ex.define_template('Template:Normal', ['Some ordinary template text.'])
        self.assertIsNone(ex.redirects.get('Template:Normal'))
        self.assertIn('Template:Normal', ex.templates)


class SindhiRedirectTests(RedirectKeywordsTestCase):
    """The new behavior this fix adds."""

    def test_sindhi_redirect_keyword(self):
        ex.define_template('سانچو:حوالا', ['#چوريو [[سانچو:حوالو]]'])
        self.assertEqual(ex.redirects.get('سانچو:حوالا'), 'سانچو:حوالو')
        self.assertNotIn('سانچو:حوالا', ex.templates)

    def test_real_world_case_content_leak_is_gone(self):
        # The exact real case this fix was found on: once recognized
        # as a redirect, the template's stale, leftover body (the
        # "list-style-type" CSS construct) never gets treated as its
        # content in the first place.
        ex.define_template('سانچو:حوالا', [
            '#چوريو [[سانچو:حوالو]]\n'
            '<div class="reflist" style="list-style-type: decimal;">\n'
            '{{#tag:references}}</div>'
        ])
        self.assertEqual(ex.redirects.get('سانچو:حوالا'), 'سانچو:حوالو')
        self.assertNotIn('سانچو:حوالا', ex.templates)


class FalsePositiveBoundaryTests(RedirectKeywordsTestCase):
    """The narrow, deliberately-tested false-positive surface: only
    matches at the very start of a template's first line, only as a
    whole word, only when a wikilink actually follows on that line.
    """

    def test_word_continuation_does_not_match(self):
        # "REDIRECTED" -- no word boundary right after "REDIRECT".
        ex.define_template('Template:Foo', ['#REDIRECTED to a new place [[Template:Bar]]'])
        self.assertIsNone(ex.redirects.get('Template:Foo'))
        self.assertIn('Template:Foo', ex.templates)

    def test_keyword_not_at_start_of_first_line_does_not_match(self):
        ex.define_template('Template:Foo', ['Some text. #REDIRECT mentioned here [[Template:Bar]]'])
        self.assertIsNone(ex.redirects.get('Template:Foo'))
        self.assertIn('Template:Foo', ex.templates)

    def test_keyword_with_no_wikilink_does_not_match(self):
        ex.define_template('Template:Foo', ['#REDIRECT to somewhere, no link here'])
        self.assertIsNone(ex.redirects.get('Template:Foo'))
        self.assertIn('Template:Foo', ex.templates)

    def test_keyword_appearing_only_in_a_later_line_does_not_match(self):
        # Only the template's first line is ever checked.
        ex.define_template('Template:Foo', [
            'This is the real, first line of content.\n'
            '#REDIRECT [[Template:Bar]]\n'
        ])
        self.assertIsNone(ex.redirects.get('Template:Foo'))
        self.assertIn('Template:Foo', ex.templates)


if __name__ == '__main__':
    unittest.main()
