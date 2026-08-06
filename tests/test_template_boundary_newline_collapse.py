"""
Tests for a single-newline-collapse fix in define_template()'s
<noinclude>/<onlyinclude> handling in extract.py.

Real MediaWiki collapses a single newline immediately adjacent to a
<noinclude>/<includeonly>/<onlyinclude> tag boundary, specifically so
that removing (or, for onlyinclude, selecting) content at that
boundary doesn't leave a stray blank line behind. define_template()
previously implemented none of this -- a template whose source simply
ended with "...</noinclude>\n" (the ordinary case of a template file
ending right after its documentation block) stored that trailing
newline as literal, tacked-on content, appended to every future
expansion of the template.

Confirmed on a real Urdu Wikipedia template, "سانچہ:تراش"
("Template:Trim") -- whose entire purpose is trimming whitespace from
its argument via the well-known "{{#if:1|value}}" community idiom
(#if strips whitespace from the value it returns). Its own stored
body, after define_template()'s existing (unfixed) noinclude handling,
was "{{safesubst:#if:1|{{{1|}}}}}\\n" -- a literal trailing newline
sitting outside the #if expression entirely, which #if's own
stripping can't reach. So "Trim" -- ironically -- appended a newline
to everything it wrapped. Real consequence: a citation template
("سانچہ:القرآن/ربط2") built a URL by wrapping each query parameter in
"Trim", e.g. "chapter={{تراش| {{{1}}} }}&from_verse=...". With the
extra newline embedded, MediaWiki's own "[url label]" external-link
syntax -- which uses the first whitespace to separate the URL from
the label -- broke, and the raw, unresolved query string leaked into
extracted article text as visible content on a real page ("الجحیم",
UR wiki, id 515450).

A related, pre-existing (before this fix) variant of the same
template, with three literal trailing SPACES instead of a newline,
produces a related but less severe symptom (the URL survives up to
the first space; only the tail leaks) -- deliberately NOT addressed
by this fix, and covered here as a control case: spaces are ordinary
content, and no MediaWiki rule collapses them, so stripping them would
mean guessing at authorial intent rather than replicating a real,
documented behavior. That template's actual bug needs fixing on the
wiki itself (e.g. switching to <onlyinclude>, which is immune to
anything trailing after it regardless of type).

Scope of the fix, deliberately narrow:
  - Only ONE trailing newline, only immediately after </noinclude>
    (also covering the unterminated and self-closing <noinclude>
    forms) and immediately inside <onlyinclude>'s own tag boundaries.
  - NOT extended to a newline BEFORE the opening <noinclude> tag: an
    earlier attempt also stripped that side, which seemed symmetric
    but was wrong -- for a noinclude block sandwiched between real
    content on both sides ("Before\\n<noinclude>...\\n</noinclude>\\n
    After"), stripping both sides merges "Before" and "After" onto the
    same line entirely, rather than just avoiding an extra blank line
    between them. Trailing-side-only matches the confirmed bug shape
    without that regression -- covered explicitly below.
  - NOT extended to <includeonly>: its content is always kept either
    way, and deleting just its tags in place doesn't remove any text,
    so there's no equivalent gap for a stray newline to fill.

Run with:
    python -m unittest tests.test_template_boundary_newline_collapse -v
or, from the tests/ directory:
    python -m unittest test_template_boundary_newline_collapse -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class NewlineCollapseTestCase(unittest.TestCase):

    def setUp(self):
        ex.templates.clear()
        ex.templateCache.clear()
        ex.redirects.clear()
        ex.Extractor.templatePrefix = "Template:"

    def stored_body(self, page_lines):
        ex.define_template('Template:X', page_lines)
        return ex.templates.get('Template:X')


class NoincludeTrailingNewlineTests(NewlineCollapseTestCase):
    """The core fix: a single trailing newline right after
    </noinclude>, with nothing else following, is now collapsed."""

    def test_real_trim_template_shape(self):
        # Reconstruction of the real case: an "#if"-based trim
        # template whose stored body must be exactly the #if
        # expression, with no trailing newline appended.
        body = self.stored_body([
            '<includeonly>{{safesubst:#if:1|{{{1|}}}}}</includeonly>'
            '<noinclude>\n{{documentation}}\n</noinclude>\n'
        ])
        self.assertEqual(body, '{{safesubst:#if:1|{{{1|}}}}}')

    def test_real_trim_template_used_in_a_url_no_longer_leaks(self):
        # End-to-end: the actual failure mode -- a citation template
        # building a URL by wrapping each parameter in "Trim" -- no
        # longer leaks the unresolved query string into visible text.
        ex.define_template('Template:Trim', [
            '<includeonly>{{safesubst:#if:1|{{{1|}}}}}</includeonly>'
            '<noinclude>\n{{documentation}}\n</noinclude>\n'
        ])
        ex.define_template('Template:LinkB', [
            'http://example.com?chapter={{Trim| {{{1}}} }}&verse={{Trim| {{{2}}} }}&done'
        ])
        wikitext = 'cite: [{{LinkB|2|119}} Some Label]'
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [wikitext])
        result = extractor.clean_text(wikitext, expand_templates=True)
        self.assertEqual(result, ['cite: Some Label'])

    def test_unterminated_noinclude_with_trailing_newline(self):
        body = self.stored_body(['before<noinclude>docs forever\n'])
        self.assertEqual(body, 'before')

    def test_self_closing_noinclude_with_trailing_newline(self):
        body = self.stored_body(['before<noinclude/>\n'])
        self.assertEqual(body, 'before')


class TrailingSpacesNotStrippedControlTests(NewlineCollapseTestCase):
    """Control case: trailing SPACES (as opposed to a newline) are
    deliberately left untouched -- no MediaWiki rule collapses them,
    so stripping them would be guessing at intent rather than
    replicating real, documented behavior. This is the actual,
    still-unfixed-by-us shape of the real "سانچہ:تراش" template as it
    existed in the dump that led to this investigation; fixing it
    requires an edit on the wiki itself (e.g. <onlyinclude>), not a
    change here.
    """

    def test_trailing_spaces_after_noinclude_preserved(self):
        body = self.stored_body([
            '<includeonly>{{safesubst:#if:1|{{{1|}}}}}</includeonly>'
            '<noinclude>\n{{documentation}}\n</noinclude>   '
        ])
        self.assertEqual(body, '{{safesubst:#if:1|{{{1|}}}}}   ')


class NoRegressionOnSandwichedContentTests(NewlineCollapseTestCase):
    """The regression caught before shipping this fix: stripping a
    newline on BOTH sides of <noinclude> (not just the trailing side)
    merges unrelated "before"/"after" content onto a single line.
    Trailing-side-only avoids this entirely.
    """

    def test_noinclude_sandwiched_between_real_content(self):
        body = self.stored_body(['Before text\n<noinclude>\ndocs here\n</noinclude>\nAfter text'])
        self.assertEqual(body, 'Before text\nAfter text')
        self.assertNotIn('Before textAfter text', body)

    def test_two_separate_noinclude_blocks(self):
        body = self.stored_body(['A\n<noinclude>\ndoc1\n</noinclude>\nB\n<noinclude>\ndoc2\n</noinclude>\nC'])
        self.assertEqual(body, 'A\nB\nC')


class OnlyincludeNewlineTests(NewlineCollapseTestCase):
    """<onlyinclude> gets the analogous treatment: a newline just
    inside its own opening/closing tags is collapsed, same reasoning
    as noinclude (content is being discarded around the boundary,
    creating the same kind of gap)."""

    def test_onlyinclude_with_newlines_just_inside_tags(self):
        body = self.stored_body([
            '<onlyinclude>\nReal content\n</onlyinclude><noinclude>\ndocs\n</noinclude>'
        ])
        self.assertEqual(body, 'Real content')


class UnaffectedBaselineTests(NewlineCollapseTestCase):
    """Sanity checks: ordinary templates with no noinclude/onlyinclude
    at all, or with the tags directly touching content (nothing to
    collapse), are completely unaffected by this fix."""

    def test_plain_template_no_special_tags(self):
        body = self.stored_body(['Just plain {{{1|}}} template text\nwith a real blank line\n\nand more.'])
        self.assertEqual(body, 'Just plain {{{1|}}} template text\nwith a real blank line\n\nand more.')

    def test_noinclude_directly_touching_content_nothing_to_collapse(self):
        body = self.stored_body(['before<noinclude>doc</noinclude>after'])
        self.assertEqual(body, 'beforeafter')

    def test_includeonly_not_given_this_treatment(self):
        # includeonly's content is always kept either way -- deleting
        # just its tags in place doesn't remove any text, so there's
        # no gap for a stray newline to fill, and this fix doesn't
        # touch it at all.
        body = self.stored_body(['before<includeonly>\nkept\n</includeonly>after'])
        self.assertEqual(body, 'before\nkept\nafter')


if __name__ == '__main__':
    unittest.main()
