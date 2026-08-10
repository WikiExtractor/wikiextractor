"""
Tests for a real, common on-wiki template pattern: core content that
is unconditional, with decorative/supplementary content gated behind
a check this codebase doesn't support (#ifexist specifically, though
the same shape applies to any unsupported feature). Confirms the
unconditional part survives correctly even though the gated part
degrades gracefully instead.

Motivated by https://github.com/WikiExtractor/wikiextractor/issues/269
-- "its founding president was ." (the name silently dropped) on the
Touring Club Italiano article. Re-tested directly against a fresh
Special:Export of the current article: the name now extracts
correctly. Traced the real mechanism precisely (not just inferred):
this is NOT #ifexist having been implemented -- it still isn't, and
sharp_ifexist-equivalent support doesn't exist in this codebase. It's
that {{ill|...}} (a redirect to Template:Interlanguage link, "high-
risk template with 4000+ transclusions" per its own edit history) has
a template body whose structure keeps the actual link target --
[[{{{1}}}...]], i.e. the person's real name -- entirely outside the
part gated by #ifexist. That gated part only controls a supplementary
"[...]" interwiki/Wikidata fallback marker, shown only when the
target page doesn't exist locally. Since #ifexist always returns
empty here (unsupported), that whole conditional block takes its
else-branch -- just a category tag, no visible text lost -- while the
name was never conditional on it at all.

Confirmed directly with the real, exact template text (Template:Ill,
Template:Interlanguage link, Template:Main other, Template:Trim,
pulled from a real Special:Export of the article and reduced to just
the four templates this specific mechanism actually depends on) that
this reproduces exactly: extractor.expandTemplates(
'{{ill|Luigi Vittorio Bertarelli|de|Luigi Vittorio Bertarelli|it}}')
produces '[[Luigi Vittorio Bertarelli]][[Category:...]]<nowiki />' --
the name intact, not truncated.

Run with:
    python -m unittest tests.test_ifexist_fallback_robustness -v
or, from the tests/ directory:
    python -m unittest test_ifexist_fallback_robustness -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class MinimalIfexistFallbackPatternTests(unittest.TestCase):
    """A small, hand-built template with the same shape as the real
    one below, isolating just the mechanism itself: core content
    unconditional, decoration gated behind #ifexist. Easier to read
    at a glance than the real template's own considerable complexity
    -- the real-template test further below is what actually ties
    this to the reported issue.
    """

    def make_extractor(self, templates):
        return ex.Extractor(1, "1", "https://x", "Test Article", [], templates=templates,
                             templatePrefix='Template:')

    def test_core_content_survives_when_ifexist_is_unsupported(self):
        templates = {
            'Template:Link': '[[{{{1}}}]]{{#ifexist:{{{1}}}|| (no article yet)}}',
        }
        extractor = self.make_extractor(templates)
        result = extractor.expandTemplates('{{Link|Some Person}}')
        # The core link must survive regardless of what #ifexist does.
        self.assertIn('[[Some Person]]', result)

    def test_ifexist_call_itself_always_resolves_to_empty(self):
        # #ifexist is an unsupported stub (lambda *args: '') -- it
        # returns empty directly, without evaluating either branch, so
        # a bare {{#ifexist:...|X|Y}} call resolves to '' rather than
        # selecting X or Y. Real templates that want a usable fallback
        # for this wrap it in an outer #if that reacts to that empty
        # result -- see RealInterlanguageLinkTemplateTests below for
        # the actual, real-world version of that pattern.
        templates = {
            'Template:Link': '[[{{{1}}}]]{{#ifexist:{{{1}}}|X|Y}}',
        }
        extractor = self.make_extractor(templates)
        result = extractor.expandTemplates('{{Link|Some Person}}')
        self.assertEqual(result, '[[Some Person]]')

    def test_outer_if_reacting_to_ifexists_empty_result_is_the_real_pattern(self):
        # This is the actual mechanism Template:Interlanguage link
        # uses (see the real-template tests below): wrap #ifexist in
        # an outer #if, which correctly takes its "false" branch when
        # #ifexist resolves to empty.
        templates = {
            'Template:Link': '[[{{{1}}}]]{{#if:{{#ifexist:{{{1}}}|1|}}|X|Y}}',
        }
        extractor = self.make_extractor(templates)
        result = extractor.expandTemplates('{{Link|Some Person}}')
        self.assertEqual(result, '[[Some Person]]Y')


class RealInterlanguageLinkTemplateTests(unittest.TestCase):
    """The real templates from the actual reported issue, reduced to
    just the four this specific mechanism depends on -- confirmed
    directly against a real Special:Export of the article that this
    is the exact, complete set (Template:Ill is a redirect;
    Template:Interlanguage link needs Template:Main other and
    Template:Trim; nothing else).
    """

    TEMPLATES = {
        'Template:Interlanguage link': (
            '{{safesubst:#if:{{{quote|}}}{{{quotes|}}}|"}}'
            "{{safesubst:#if:{{{italic|}}}{{{italics|}}}|''}}"
            '[[{{{1}}}{{safesubst:#if:{{{lt|}}}|{{safesubst:!}}{{{lt}}}}}]]'
            "{{safesubst:#if:{{{italic|}}}{{{italics|}}}|&#8202;''}}"
            '{{safesubst:#if:{{{quote|}}}{{{quotes|}}}|"}}'
            '{{safesubst:#ifeq:{{subst:Substcheck}}|SUBST||'
            '{{#if:{{#if:{{{preserve|{{{display|}}}}}}|1|'
            '{{#ifexist:{{{1|}}}|{{#invoke:redirect|isRedirect|{{{1|}}}}}|1}}}}\n'
            ' |<{{#switch:{{{vertical-align|{{{valign|{{{v|}}}}}}}}}|sup|super=sup|sub=sub|span}}'
            ' class="noprint" style="{{#switch:{{{vertical-align|{{{valign|{{{v|}}}}}}}}}'
            '|ib=font-size:100%;|sup|super|sub=|font-size:85%;}} font-style: normal;'
            ' {{#if:{{{nobold|}}}|font-weight: normal;}}">&nbsp;&#91;{{#if:{{{qid|}}}\n'
            '  | [[d:Special:EntityPage/{{{qid|}}}#sitelinks-wikipedia|'
            '<span title="Wikidata list: &quot;{{{1}}}&quot; articles in other languages">'
            '{{#if:{{{short|{{{s|}}}}}}|wd|Wikidata}}</span>]]\n'
            '  | {{#invoke:Separated entries|main|frameOnly=true|separator=;&#32;\n'
            '    | {{#if:{{{2|}}}|[[:{{{2}}}:{{#if:{{{3|}}}|{{{3}}}|{{{1}}}}}|{{trim|1={{{2}}}}}]]}}\n'
            '    | {{#if:{{{4|}}}|[[:{{{4}}}:{{#if:{{{5|}}}|{{{5}}}|{{{1}}}}}|{{trim|1={{{4}}}}}]]}}\n'
            '    }}}}&#93;</{{#switch:{{{vertical-align|{{{valign|{{{v|}}}}}}}}}|sup|super=sup|sub=sub|span}}>\n'
            ' | [[Category:Interlanguage link template existing link]]<nowiki />\n'
            ' }}}}{{main other|'
            '{{#if:{{{preserve|{{{display|}}}}}}|[[Category:Interlanguage link template forcing interwiki links]]}}'
            '{{#if:{{{2|}}}{{{qid|}}}||[[Category:Pages using interlanguage link with no language parameter]]}}'
            '}}'
        ),
        'Template:Main other': (
            '{{safesubst:#switch:\n'
            '    {{safesubst:#if:{{{demospace|}}} \n'
            '  | {{safesubst:lc: {{{demospace}}} }}      | {{safesubst:#ifeq:{{safesubst:NAMESPACE}}|{{safesubst:ns:0}}\n'
            '    | main\n'
            '    | other\n'
            '    }} \n'
            '  }}\n'
            '| main     = {{{1|}}}\n'
            '| other\n'
            '| #default = {{{2|}}}\n'
            '}}'
        ),
        'Template:Trim': "{{safesubst:#if:1|{{{1|}}}}}",
    }
    REDIRECTS = {'Template:Ill': 'Template:Interlanguage link'}

    def make_extractor(self):
        return ex.Extractor(1, "1", "https://x", "Touring Club Italiano", [],
                             templates=self.TEMPLATES, redirects=self.REDIRECTS,
                             templatePrefix='Template:')

    def test_founding_presidents_name_is_not_dropped(self):
        # The exact call from the real article, the exact regression
        # this issue reported: the name must not be truncated away.
        extractor = self.make_extractor()
        result = extractor.expandTemplates(
            '{{ill|Luigi Vittorio Bertarelli|de|Luigi Vittorio Bertarelli|it}}')
        self.assertIn('[[Luigi Vittorio Bertarelli]]', result)

    def test_ill_redirect_resolves_to_interlanguage_link(self):
        # Confirms the redirect itself is followed correctly -- not
        # just that calling the real template name directly happens
        # to work.
        extractor = self.make_extractor()
        via_redirect = extractor.expandTemplates('{{ill|Test Name|fr|Test Name|fr}}')
        direct = extractor.expandTemplates('{{Interlanguage link|Test Name|fr|Test Name|fr}}')
        self.assertEqual(via_redirect, direct)

    def test_real_pipeline_end_to_end_through_clean_text(self):
        # Not expandTemplates() directly -- the real chain
        # (clean_text() -> expandTemplate() -> ...), matching how a
        # real extraction run actually processes this article.
        extractor = self.make_extractor()
        wikitext = ("Among the founding members was "
                    "{{ill|Luigi Vittorio Bertarelli|de|Luigi Vittorio Bertarelli|it}}, "
                    "who became president in 1919.")
        result = extractor.clean_text(wikitext, expand_templates=True)
        self.assertIn('Luigi Vittorio Bertarelli', '\n'.join(result))


if __name__ == '__main__':
    unittest.main()
