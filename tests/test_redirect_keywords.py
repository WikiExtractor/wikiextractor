"""
Tests for define_template()'s redirect detection in extract.py.

Real MediaWiki's redirect magic word is localized per-wiki
(each wiki's own content language has its own translation,
e.g. Sindhi uses "چوريو" and Urdu uses "رجوع_مکرر", rather than
"REDIRECT"), separate from the interface language.
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

Confirmed separately on a real Urdu Wikipedia dump: a template
("سانچہ:ص.م/فتح", used by the film infobox chain) starting with
"#رجوع_مکرر [[سانچہ:خانہ معلومات/آغاز]]" was never resolved as a
redirect either, which meant the target template supplying an
infobox's opening "{|" wikitable syntax was never reached -- leaving
a sibling template's "! scope=col ..." header-row fragment with no
"{|" to be paired with, so table-stripping had nothing to match and
the raw row leaked into extracted text. Real case: page id 1078623
("سنڈریلا 3: اے ٹوسٹ ان ٹائم", 2026-07-01 UR dump).

Fixed via an extensible redirectKeywords list (starting with
'REDIRECT', 'چوريو', and now 'رجوع_مکرر'), rather than a single
hardcoded English pattern -- more can be added as they're confirmed
on other wikis, without guessing translations for languages not yet
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
        self.templates = {}
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        ex.redirects.clear()


class EnglishRedirectStillWorksTests(RedirectKeywordsTestCase):
    """Regression coverage for the original, pre-existing behavior."""

    def test_uppercase_redirect(self):
        ex.define_template('Template:Foo', ['#REDIRECT [[Template:Bar]]'], self.templates)
        self.assertEqual(ex.redirects.get('Template:Foo'), 'Template:Bar')
        self.assertNotIn('Template:Foo', self.templates)

    def test_lowercase_redirect_case_insensitive(self):
        ex.define_template('Template:Foo', ['#redirect [[Template:Bar]]'], self.templates)
        self.assertEqual(ex.redirects.get('Template:Foo'), 'Template:Bar')

    def test_mixed_case_redirect(self):
        ex.define_template('Template:Foo', ['#Redirect [[Template:Bar]]'], self.templates)
        self.assertEqual(ex.redirects.get('Template:Foo'), 'Template:Bar')

    def test_normal_non_redirect_template_unaffected(self):
        ex.define_template('Template:Normal', ['Some ordinary template text.'], self.templates)
        self.assertIsNone(ex.redirects.get('Template:Normal'))
        self.assertIn('Template:Normal', self.templates)


class SindhiRedirectTests(RedirectKeywordsTestCase):
    """The new behavior this fix adds."""

    def test_sindhi_redirect_keyword(self):
        ex.define_template('سانچو:حوالا', ['#چوريو [[سانچو:حوالو]]'], self.templates)
        self.assertEqual(ex.redirects.get('سانچو:حوالا'), 'سانچو:حوالو')
        self.assertNotIn('سانچو:حوالا', self.templates)

    def test_real_world_case_content_leak_is_gone(self):
        # The exact real case this fix was found on: once recognized
        # as a redirect, the template's stale, leftover body (the
        # "list-style-type" CSS construct) never gets treated as its
        # content in the first place.
        ex.define_template('سانچو:حوالا', [
            '#چوريو [[سانچو:حوالو]]\n'
            '<div class="reflist" style="list-style-type: decimal;">\n'
            '{{#tag:references}}</div>'
        ], self.templates)
        self.assertEqual(ex.redirects.get('سانچو:حوالا'), 'سانچو:حوالو')
        self.assertNotIn('سانچو:حوالا', self.templates)


class UrduRedirectTests(RedirectKeywordsTestCase):
    """Urdu's own redirect keyword, "رجوع_مکرر" -- distinct from
    Sindhi's "چوريو" despite both being South Asian languages sharing
    the whole conversation's UR test corpus.

    Confirmed on a real Urdu Wikipedia dump: "سانچہ:ص.م/فتح"
    (Template:Infobox-open), used by the film infobox template chain,
    is a redirect to "سانچہ:خانہ معلومات/آغاز" via
    "#رجوع_مکرر [[سانچہ:خانہ معلومات/آغاز]]". Before this fix, that
    redirect was never recognized -- the literal redirect-arrow text
    got treated as "ص.م/فتح"'s own template content instead of being
    resolved to its target, so the target's opening "{|" wikitable
    syntax was never reached. A sibling template further down the
    chain still successfully emitted its own "! scope=col ..." wikitext
    header-row fragment, but with no "{|" ever generated to pair it
    with, dropNested()'s table-stripping had nothing to match, and the
    raw table row leaked verbatim into extracted article text --
    visible on real output for id 1078623 ("سنڈریلا 3: اے ٹوسٹ ان
    ٹائم", 2026-07-01 UR dump).

    Recognizing the redirect keyword is necessary but not alone
    sufficient to fix that specific leak -- the redirect's target
    template also has to actually be present in whatever templates
    file is loaded, which is a separate, likely related gap: template-
    dependency discovery presumably also needs to follow this same
    redirect chain, and would have hit the same unrecognized-keyword
    problem while doing so.
    """

    def test_urdu_redirect_keyword(self):
        ex.define_template('سانچہ:ص.م/فتح', ['#رجوع_مکرر [[سانچہ:خانہ معلومات/آغاز]]'], self.templates)
        self.assertEqual(ex.redirects.get('سانچہ:ص.م/فتح'), 'سانچہ:خانہ معلومات/آغاز')
        self.assertNotIn('سانچہ:ص.م/فتح', self.templates)

    def test_real_world_case_table_leak_is_gone(self):
        # With the redirect keyword recognized AND the target template
        # actually available, the infobox's "{|" opener is reached and
        # the header row it should have wrapped gets correctly dropped
        # -- rather than leaking verbatim as in the real case above.
        ex.define_template('سانچہ:ص.م/فتح', ['#رجوع_مکرر [[سانچہ:خانہ معلومات/آغاز]]'], self.templates)
        ex.define_template('سانچہ:خانہ معلومات/آغاز', ['{| class="infobox"'], self.templates)
        ex.define_template('سانچہ:خانہ معلومات/اختتام', ['|}'], self.templates)
        ex.define_template('سانچہ:خ۔م/عنوان', ['! scope=col | {{{1}}}'], self.templates)

        wikitext = ('{{ص.م/فتح}}\n{{خ۔م/عنوان|Test}}\n{{خانہ معلومات/اختتام}}\n'
                    'Ordinary article prose follows.')
        extractor = ex.Extractor('1', '1', 'https://x', 'Test', [wikitext], templates=self.templates)
        result = extractor.clean_text(wikitext)
        joined = '\n'.join(result)
        self.assertNotIn('scope=col', joined)
        self.assertIn('Ordinary article prose follows.', joined)


class FalsePositiveBoundaryTests(RedirectKeywordsTestCase):
    """The narrow, deliberately-tested false-positive surface: only
    matches at the very start of a template's first line, only as a
    whole word, only when a wikilink actually follows on that line.
    """

    def test_word_continuation_does_not_match(self):
        # "REDIRECTED" -- no word boundary right after "REDIRECT".
        ex.define_template('Template:Foo', ['#REDIRECTED to a new place [[Template:Bar]]'], self.templates)
        self.assertIsNone(ex.redirects.get('Template:Foo'))
        self.assertIn('Template:Foo', self.templates)

    def test_keyword_not_at_start_of_first_line_does_not_match(self):
        ex.define_template('Template:Foo', ['Some text. #REDIRECT mentioned here [[Template:Bar]]'], self.templates)
        self.assertIsNone(ex.redirects.get('Template:Foo'))
        self.assertIn('Template:Foo', self.templates)

    def test_keyword_with_no_wikilink_does_not_match(self):
        ex.define_template('Template:Foo', ['#REDIRECT to somewhere, no link here'], self.templates)
        self.assertIsNone(ex.redirects.get('Template:Foo'))
        self.assertIn('Template:Foo', self.templates)

    def test_keyword_appearing_only_in_a_later_line_does_not_match(self):
        # Only the template's first line is ever checked.
        ex.define_template('Template:Foo', [
            'This is the real, first line of content.\n'
            '#REDIRECT [[Template:Bar]]\n'
        ], self.templates)
        self.assertIsNone(ex.redirects.get('Template:Foo'))
        self.assertIn('Template:Foo', self.templates)


if __name__ == '__main__':
    unittest.main()
