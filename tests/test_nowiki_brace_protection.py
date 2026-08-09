"""
Tests for _mask_nowiki()/_unmask_nowiki() and their use inside
expandTemplates() -- protecting <nowiki>...</nowiki> regions from
being misread as real template/link syntax during brace-matching.

Root cause this fixes: findMatchingBraces() (used by expandTemplates()
to find {{...}} template-call boundaries, and by splitParts() to split
a call's own argument list on |) scans raw text for brace/bracket
characters with no awareness of <nowiki> tags. A template using
<nowiki>}}</nowiki> to *display* the literal characters "}}" -- a
real, unremarkable pattern, not a contrived edge case -- can
prematurely terminate an enclosing template call, silently truncating
everything after that point into unprocessed, literal leftover text.

Confirmed against real, live ur.wikipedia.org data:
Template:Metadata population AT-1 (reached via Template:Infobox
settlement, one of the most widely-used templates on the whole
project) does exactly this in its own error-message branch, triggered
by an easy-to-hit, unremarkable case -- calling it with an argument it
doesn't recognize.

The fix has to live inside expandTemplates() itself, not run once on
the original article text, because a <nowiki> sequence can arrive
*mid-expansion* -- introduced by template substitution, exactly what
happens in the real Metadata population AT-1 case -- not just by
being present in the wikitext being scanned at the top of the call
stack.

Run with:
    python -m unittest tests.test_nowiki_brace_protection -v
or, from the tests/ directory:
    python -m unittest test_nowiki_brace_protection -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class MaskUnmaskRoundTripTests(unittest.TestCase):
    """_mask_nowiki()/_unmask_nowiki() directly, no template
    expansion involved -- confirms the core primitive is correct
    before relying on it inside expandTemplates().
    """

    def test_no_nowiki_present_is_a_cheap_no_op(self):
        text = 'plain text, no nowiki here at all'
        masked, placeholders = ex._mask_nowiki(text)
        self.assertIs(masked, text)  # not just equal -- the exact same object, confirming the fast path
        self.assertIsNone(placeholders)

    def test_single_span_masked_and_restored_exactly(self):
        text = 'before <nowiki>}}{{[[]]</nowiki> after'
        masked, placeholders = ex._mask_nowiki(text)
        self.assertNotIn('{', masked)
        self.assertNotIn('}', masked)
        self.assertNotIn('[', masked)
        self.assertEqual(ex._unmask_nowiki(masked, placeholders), text)

    def test_multiple_spans_each_masked_and_restored_exactly(self):
        text = '<nowiki>}}</nowiki> middle <nowiki>{{</nowiki> end'
        masked, placeholders = ex._mask_nowiki(text)
        self.assertNotIn('{', masked)
        self.assertNotIn('}', masked)
        self.assertEqual(ex._unmask_nowiki(masked, placeholders), text)

    def test_unmask_with_none_placeholders_is_a_no_op(self):
        self.assertEqual(ex._unmask_nowiki('unchanged', None), 'unchanged')

    def test_masked_placeholder_contains_no_pipe_either(self):
        # Not just braces/brackets -- a literal "|" inside <nowiki>
        # must not be readable as an argument separator by splitParts().
        text = '<nowiki>a|b|c</nowiki>'
        masked, placeholders = ex._mask_nowiki(text)
        self.assertNotIn('|', masked)


class ExpandTemplatesNowikiProtectionTests(unittest.TestCase):
    """Through the real expandTemplates() -> findMatchingBraces() ->
    expandTemplate() -> splitParts() chain -- confirming the
    protection actually reaches every layer that needs it, not just
    the masking primitive in isolation.
    """

    def make_extractor(self, templates):
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [])
        extractor.templates = templates
        extractor.templatePrefix = 'Template:'
        return extractor

    def test_nowiki_escaped_closing_braces_do_not_truncate_the_call(self):
        # The exact shape of the original bug: a nowiki-escaped "}}"
        # must not be read as the real closing brace of the enclosing
        # template call.
        extractor = self.make_extractor({})  # "Wrapper" undefined -> whole call resolves empty
        result = extractor.expandTemplates(
            '{{Wrapper|rowclass1=A|data1=<nowiki>}}</nowiki>|rowclass2=B|data2=C}}')
        self.assertNotIn('rowclass2', result)
        self.assertNotIn('data2', result)

    def test_nowiki_escaped_pipe_is_not_read_as_an_argument_separator(self):
        extractor = self.make_extractor({'Template:Foo': 'a=[[[{{{a}}}]]] b=[[[{{{b}}}]]]'})
        result = extractor.expandTemplates('{{Foo|a=<nowiki>x|y</nowiki>|b=real}}')
        # If the escaped "|" were wrongly treated as a real separator,
        # parameter a would end up as just "x" and "y" would become a
        # spurious third positional argument, leaving b unfilled.
        self.assertIn('x|y', result)
        self.assertIn('real', result)

    def test_nowiki_containing_a_wikilink_does_not_confuse_brace_matching(self):
        extractor = self.make_extractor({'Template:Foo': 'got: {{{a}}}'})
        result = extractor.expandTemplates(
            '{{Foo|a=<nowiki>[[not a real link]]</nowiki>}}')
        self.assertIn('[[not a real link]]', result)

    def test_multiple_nowiki_spans_in_one_call_all_protected(self):
        extractor = self.make_extractor(
            {'Template:Bar': 'A={{{a}}} B={{{b}}} C={{{c}}}'})
        result = extractor.expandTemplates(
            '{{Bar|a=<nowiki>}}</nowiki>|b=<nowiki>{{</nowiki>|c=3}}')
        self.assertIn('A=<nowiki>}}</nowiki>', result)
        self.assertIn('B=<nowiki>{{</nowiki>', result)
        self.assertIn('C=3', result)

    def test_unclosed_nowiki_tag_does_not_crash_or_regress(self):
        # No closing tag at all -- _NOWIKI_RE simply won't match it
        # (matches prior behavior for this malformed-wikitext case;
        # this fix targets well-formed <nowiki>...</nowiki> pairs, not
        # a general wikitext-repair mechanism).
        extractor = self.make_extractor({'Template:Foo': 'x'})
        result = extractor.expandTemplates('{{Foo}} <nowiki>unclosed forever')
        self.assertIn('x', result)
        self.assertIn('<nowiki>unclosed forever', result)

    def test_real_world_shape_nowiki_arriving_mid_expansion_via_substitution(self):
        # The specific mechanism that made the original bug hard to
        # spot: the <nowiki> sequence isn't in the original article
        # text at all -- it arrives only after a template's own body
        # gets substituted in and re-scanned for further nested
        # template calls. This is exactly why the fix has to run
        # inside expandTemplates() on every call, not once at the top
        # of the whole expansion.
        templates = {
            'Template:ErrorMessage': 'call it as {{Foo<nowiki>}}</nowiki> not like that',
            'Template:Outer': ('before {{ErrorMessage}} '
                                '| rowclass1 = should_survive | data1 = yes'),
        }
        extractor = self.make_extractor(templates)
        result = extractor.expandTemplates('{{Outer}}')
        self.assertIn('rowclass1', result)
        self.assertIn('should_survive', result)


class NowikiRealEndToEndTests(unittest.TestCase):
    """Not expandTemplates() directly -- the real chain
    (clean_text() -> expandTemplate() -> ...), confirming the fix
    holds up through the full pipeline a real extraction run uses.
    """

    def test_real_pipeline_does_not_leak_raw_template_syntax(self):
        templates = {
            'Template:Metadata': (
                '{{#switch: {{{2|}}}'
                '| date = 2020-01-01'
                '| #default = <nowiki>}}</nowiki> unrecognized keyword'
                '}}'),
            # Framework: a stand-in for real MediaWiki's own generic
            # infobox-building templates -- takes rowclassN/dataN
            # arguments and (in the real case) hands them to a Lua
            # module; here it just echoes them back, which is enough
            # to prove they survived as real, separate arguments
            # rather than being swallowed by a premature "}}".
            'Template:Framework': 'seen: {{{rowclass1}}} / {{{data1}}}',
            'Template:Infobox': (
                '{{Framework'
                '| rowclass1 = {{Metadata|date}}'
                '| data1 = 42'
                '}}'),
        }
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                  templates=templates, templatePrefix='Template:')
        result = extractor.clean_text('{{Infobox}}', expand_templates=True)
        joined = '\n'.join(result)
        self.assertIn('42', joined)
        self.assertNotIn('rowclass1', joined)
        self.assertNotIn('{{Framework', joined)


if __name__ == '__main__':
    unittest.main()
