"""
Tests for the findMatchingBraces() fix for perfectly-balanced,
ambiguous brace runs of more than 3 consecutive '{' or '}'.

findMatchingBraces()'s own comments document the correct MediaWiki
precedence rule for these cases:
    {{{{ }}}}   -> { {{{ }}} }      (4 braces: 1 outer + 3 inner)
    {{{{{ }}}}} -> {{ {{{ }}} }}    (5 braces: 2 outer + 3 inner)
But the rule was only actually applied when opening/closing counts
DIDN'T match exactly at some point during scanning (the "ambiguous"
elif branch, len(stack) == 1 and 0 < stack[0] < ldelim). When a run of
N>3 opening braces is matched by an EXACTLY equal run of N closing
braces -- the common, simple case -- the greedy regex consumes the
whole run at once with zero remainder, so the stack empties completely
and the code takes the plain "exact match" branch instead, yielding
one single, undifferentiated span for the whole run rather than
correctly splitting outer-template-around-inner-tplarg.

Confirmed this is a real, reproducible bug via a genuine, real-world
case: {{{{{1}}}}} is a standard MediaWiki idiom meaning "call whatever
template parameter 1 names" (used by "multiple issues"/stub-banner
wrapper templates). Misparsed as one, whole span, this becomes a
direct call to a template literally named "1" -- which happens to be
a real, well-known MediaWiki diagnostic template that exists
specifically to catch this exact mistake ({{1}} typed instead of
{{{1}}}). That template's own body then re-triggers the same call
recursively, tripping the loop-detection safeguard mid-expansion and
corrupting nearby includeonly handling as a side effect -- confirmed
directly against the real article and templates this was found on.

Fixed by capping the CONSUMED width of the very first (outermost)
brace-run match at 3 when it exceeds 3, offsetting the reported start
position inward accordingly, per the documented "last three belong to
a tplarg" rule -- deliberately NOT touching brace-runs discovered
mid-scan (an earlier attempt at a symmetric fix there caused a real
regression: reducing the scan position for a mid-scan match makes the
same excess braces get rediscovered and rematched as a spurious,
separate event on the next iteration). The mid-scan case doesn't need
special handling -- confirmed directly that the existing, original
stack-based algorithm already resolves it correctly on its own once
the initial match is no longer swallowing the whole run.

Run with:
    python -m unittest tests.test_brace_matching -v
or, from the tests/ directory:
    python -m unittest test_brace_matching -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor, findMatchingBraces, Template, TemplateArg
import wikiextractor.extract as ex


class FindMatchingBracesDirectTests(unittest.TestCase):
    """Direct tests against findMatchingBraces() itself, covering
    every case explicitly documented in its own comments.
    """

    def test_five_brace_case_ldelim_3(self):
        # {{{{{1}}}}} -> {{ {{{1}}} }}: the inner tplarg is what
        # Template.parse() (which always uses ldelim=3) needs to find.
        text = '{{{{{1}}}}}'
        spans = list(findMatchingBraces(text, 3))
        self.assertEqual([text[s:e] for s, e in spans], ['{{{1}}}'])

    def test_four_brace_case_ldelim_2(self):
        # {{{{ }}}} -> { {{{ }}} }: one outer plain '{', 3-brace tplarg inside.
        text = '{{{{ }}}}'
        spans = list(findMatchingBraces(text, 2))
        self.assertEqual([text[s:e] for s, e in spans], ['{{{ }}}'])

    def test_four_brace_case_ldelim_3(self):
        text = '{{{{ }}}}'
        spans = list(findMatchingBraces(text, 3))
        self.assertEqual([text[s:e] for s, e in spans], ['{{{ }}}'])

    def test_ordinary_two_brace_template_unaffected(self):
        text = '{{template|param}}'
        spans = list(findMatchingBraces(text, 2))
        self.assertEqual([text[s:e] for s, e in spans], ['{{template|param}}'])

    def test_ordinary_three_brace_tplarg_unaffected(self):
        text = '{{{param|default}}}'
        spans = list(findMatchingBraces(text, 3))
        self.assertEqual([text[s:e] for s, e in spans], ['{{{param|default}}}'])

    def test_five_brace_nested_within_a_larger_call_still_finds_outer_span(self):
        # The case that caused a real regression during development:
        # a 5-brace run appearing mid-scan, nested inside an outer,
        # ordinary 2-brace call, must not prevent the outer span from
        # being found at all.
        text = '{{#if:{{{1|}}}|{{{{{1}}}}}|no param}}'
        spans = list(findMatchingBraces(text, 2))
        self.assertEqual([text[s:e] for s, e in spans], [text])


class RecursiveNestingDirectTests(unittest.TestCase):
    """findMatchingBraces() alone only resolves ONE level of the
    more-than-3-braces ambiguity. Real MediaWiki syntax allows a
    parameter whose name is itself a parameter, indefinitely deep
    (confirmed via MediaWiki's own documentation, Manual:Advanced_templates,
    whose own examples go to 6 braces -- "the parameter name is itself
    a parameter" -- and 12 braces, "fourth level indirection"; also
    confirmed this occurs in real template data, not just a
    theoretical case). Template.parse() widens a tplarg span to
    include any further, immediately-adjacent brace layers, so that
    each additional level gets discovered as its own, properly nested
    TemplateArg via the ordinary recursive Template.parse() call on
    that wider content.
    """

    def test_six_braces_produces_two_levels_of_nested_templatearg(self):
        tree = Template.parse('{{{{{{p}}}}}}')
        self.assertEqual(len(tree), 3)  # leading/trailing TemplateText + one TemplateArg
        outer_arg = tree[1]
        self.assertIsInstance(outer_arg, TemplateArg)
        # the outer arg's own name must itself resolve to a nested tplarg,
        # not a flat, opaque "{{{p}}}" string
        inner_items = list(outer_arg.name)
        self.assertEqual(len(inner_items), 3)
        self.assertIsInstance(inner_items[1], TemplateArg)
        self.assertEqual(str(inner_items[1].name), 'p')

    def test_twelve_braces_produces_four_levels(self):
        tree = Template.parse('{{{{{{{{{{{{p}}}}}}}}}}}}')
        outer = tree[1]
        depth = 0
        current = outer
        while isinstance(current, TemplateArg):
            depth += 1
            items = list(current.name)
            current = items[1] if len(items) >= 2 else None
        self.assertEqual(depth, 4)

    def test_ordinary_single_level_tplarg_unaffected(self):
        # A plain, non-ambiguous {{{param}}} must still parse as exactly
        # one TemplateArg, not get incorrectly widened.
        tree = Template.parse('{{{param}}}')
        self.assertEqual(len(tree), 3)
        self.assertIsInstance(tree[1], TemplateArg)
        self.assertEqual(str(tree[1].name), 'param')

    def test_five_brace_case_still_only_one_level_not_incorrectly_widened(self):
        # {{{{{1}}}}} is 2 (template) + 3 (tplarg) -- NOT two nested
        # tplargs. The widening must not misfire here: the 2 leftover
        # outer braces are a template delimiter, not another "{{{"
        # layer, so they should NOT get folded into the TemplateArg.
        tree = Template.parse('{{{{{1}}}}}')
        # leading/trailing text should carry the un-widened, leftover
        # 2-brace template delimiters, not be absorbed into the tplarg
        self.assertEqual(str(tree[0]) + str(tree[1]) + str(tree[2]), '{{{{{1}}}}}')
        self.assertIsInstance(tree[1], TemplateArg)
        self.assertEqual(str(tree[1].name), '1')


class RecursiveNestingEndToEndTests(unittest.TestCase):
    """The real, documented MediaWiki examples (Manual:Advanced_templates),
    resolved through the actual define_template()/expandTemplate()
    machinery.
    """

    def setUp(self):
        self.templates = {}
        self.redirects = {}
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text], templates=self.templates, redirects=self.redirects)
        return extractor.clean_text(article_text, expand_templates=True)

    def test_six_brace_indirection_the_real_documented_example(self):
        # Template:ppp contains {{{{{{p}}}}}}; {{ppp|p=foo|foo=bar}}
        # sets the parameter named {{{foo}}} to "bar", then sets the
        # parameter named {{{p}}} to the VALUE of foo -- i.e. after
        # inner expansion the variable is {{{foo}}}, which resolves to
        # "bar". MediaWiki namespaces auto-capitalize the first letter
        # of a title, so the template must be defined as "Template:Ppp"
        # to correctly match a call written as {{ppp|...}}.
        ex.define_template('Template:Ppp', ['{{{{{{p}}}}}}'], self.templates, self.redirects)
        result = self.get_result('{{ppp|p=foo|foo=bar}}')
        self.assertEqual(result, ['bar'])

    def test_twelve_brace_fourth_level_indirection_the_real_documented_example(self):
        ex.define_template('Template:Tvvvv', ['{{{{{{{{{{{{p}}}}}}}}}}}}'], self.templates, self.redirects)
        result = self.get_result('{{tvvvv|p=alpha|alpha=beta|beta=gamma|gamma=delta}}')
        self.assertEqual(result, ['delta'])


    def test_comprehensive_range_four_through_fifteen(self):
        # Every count in this range should follow the same rule: N // 3
        # nested tplarg levels, with a leftover of exactly N % 3 stray,
        # literal characters on each side (0, 1, or 2 -- never anything
        # else, and the widening loop must never overshoot into
        # negative indices for small leftover amounts).
        for n in range(4, 16):
            with self.subTest(n=n):
                text = '{' * n + 'X' + '}' * n
                tree = Template.parse(text)
                leading, trailing = str(tree[0]), str(tree[-1])
                depth = 0
                current = tree[1] if isinstance(tree[1], TemplateArg) else None
                while isinstance(current, TemplateArg):
                    depth += 1
                    items = list(current.name)
                    current = items[1] if len(items) >= 2 and isinstance(items[1], TemplateArg) else None
                self.assertEqual(depth, n // 3, f'n={n}: wrong nesting depth')
                self.assertEqual(len(leading), n % 3, f'n={n}: wrong leading leftover')
                self.assertEqual(len(trailing), n % 3, f'n={n}: wrong trailing leftover')


class RecursiveNestingWeirdCountsEndToEndTests(unittest.TestCase):
    """The two representative 'weird' (non-multiple-of-3) shapes,
    resolved through the real, end-to-end pipeline: a leftover single
    stray literal brace (n=7, remainder 1) and a leftover real, outer
    2-brace template wrapper (n=8, remainder 2).
    """

    def setUp(self):
        self.templates = {}
        self.redirects = {}
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text], templates=self.templates, redirects=self.redirects)
        return extractor.clean_text(article_text, expand_templates=True)

    def test_seven_braces_stray_literal_brace_survives_around_resolved_value(self):
        # 7 = 2*3 + 1: two nested tplarg levels, wrapped in one leftover,
        # literal stray '{'/'}' on each side (not a valid delimiter of
        # any kind, so it just survives as plain text).
        ex.define_template('Template:Seven', ['X{{{{{{{p}}}}}}}X'], self.templates, self.redirects)
        result = self.get_result('{{seven|p=foo|foo=bar}}')
        self.assertEqual(result, ['X{bar}X'])

    def test_eight_braces_outer_two_brace_template_wrapper_fires(self):
        # 8 = 2*3 + 2: two nested tplarg levels resolve to a value that
        # itself becomes the NAME of a real, outer, dynamically-named
        # 2-brace template call.
        ex.define_template('Template:Eight', ['{{{{{{{{p}}}}}}}}'], self.templates, self.redirects)
        ex.define_template('Template:Bar', ['outer template content'], self.templates, self.redirects)
        result = self.get_result('{{eight|p=foo|foo=Bar}}')
        self.assertEqual(result, ['outer template content'])


class DynamicTemplateNameEndToEndTests(unittest.TestCase):
    """The real, end-to-end scenario this bug was found on: a
    {{{{{N}}}}}-style dynamic template-name call, resolved through the
    actual define_template()/expandTemplate() machinery, not just
    findMatchingBraces() in isolation.
    """

    def setUp(self):
        self.templates = {}
        self.redirects = {}
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        ex.Extractor.templatePrefix = "Template:"

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text], templates=self.templates, redirects=self.redirects)
        return extractor.clean_text(article_text, expand_templates=True)

    def test_dynamic_template_name_call_resolves_correctly(self):
        ex.define_template('Template:Wrapper', ['{{{{{1}}}}}'], self.templates, self.redirects)
        ex.define_template('Template:Target', ['the real target content'], self.templates, self.redirects)
        text = 'before {{Wrapper|Target}} after'
        result = self.get_result(text)
        self.assertEqual(result, ['before the real target content after'])

    def test_real_world_shape_with_conditional_and_fallback(self):
        # Matches the real case this was found on: a conditional
        # wrapper that dynamically calls whatever template name was
        # passed, or falls back to plain text if no parameter was given.
        ex.define_template('Template:Wrapper', ['{{#if:{{{1|}}}|{{{{{1}}}}}|no param given}}'], self.templates, self.redirects)
        ex.define_template('Template:StubNotice', ['This article is a stub.'], self.templates, self.redirects)

        with_param = self.get_result('x {{Wrapper|StubNotice}} y')
        self.assertEqual(with_param, ['x This article is a stub. y'])

        without_param = self.get_result('x {{Wrapper}} y')
        self.assertEqual(without_param, ['x no param given y'])

    def test_does_not_incorrectly_call_a_template_literally_named_1(self):
        # The specific, confirmed failure mode: before the fix, this
        # dynamic call was misinterpreted as a direct call to a
        # template literally named "1".
        ex.define_template('Template:1', ['WRONG -- this should never be reached'], self.templates, self.redirects)
        ex.define_template('Template:Wrapper', ['{{{{{1}}}}}'], self.templates, self.redirects)
        ex.define_template('Template:Target', ['correct content'], self.templates, self.redirects)
        text = '{{Wrapper|Target}}'
        result = self.get_result(text)
        self.assertEqual(result, ['correct content'])


if __name__ == '__main__':
    unittest.main()
