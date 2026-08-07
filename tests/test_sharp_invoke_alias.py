"""
Tests for sharp_invoke()'s #invoke frame-lookup fix in extract.py.

Two separate, stacked bugs previously made {{#invoke: module | function }}
calls fail:

  1. sharp_invoke() referenced a global `modules` dict that was only
     ever defined in WikiExtractor.py, a different module -- Python
     resolves global lookups against the DEFINING module's own
     namespace, not the caller's, so this always raised NameError,
     silently swallowed by callParserFunction()'s bare except. Fixed
     by moving `modules` into extract.py, where sharp_invoke() itself
     lives.

  2. Once that NameError was gone, sharp_invoke() still failed for
     the ordinary case of an alias-style template: it guessed the
     calling template's title FROM THE INVOKED FUNCTION'S OWN NAME
     (e.g. "convert" -> "Template:Convert"), then searched the frame
     stack for an entry matching that guess. This only ever worked by
     coincidence, when a template's own name happened to match the
     function it invokes. It silently failed the moment a
     differently-named template invoked the same function -- e.g.
     {{cvt|...}}, a template literally named "Cvt", invoking the
     identical "convert" function that {{convert|...}} also invokes
     under its own, different name. This is confirmed to be the
     ordinary case, not an edge case: real Wikipedia's actual
     Template:Convert is invoked under several different short-name
     aliases this way.

     Fixed by using frame[-1] directly -- frame is a genuine stack
     (appended right before expanding a template's body, popped right
     after; see expandTemplate()), so frame[-1] is always exactly the
     template invocation that directly encloses whatever #invoke call
     is currently running, regardless of what that template happens
     to be named. No name-matching involved at all.

This file demonstrates fix #2 specifically -- the alias-name case --
since that's the one a naive "does #invoke work at all" check would
miss entirely (a same-named template invocation, like
{{convert|...}} invoking "convert", passes even against the old,
broken lookup logic; only a differently-named one exposes it).

Run with:
    python -m unittest tests.test_sharp_invoke_alias -v
or, from the tests/ directory:
    python -m unittest test_sharp_invoke_alias -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

from wikiextractor.extract import Extractor
import wikiextractor.extract as ex


class SharpInvokeAliasTestCase(unittest.TestCase):

    def setUp(self):
        self.templates = {}
        self.redirects = {}
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        ex.Extractor.templatePrefix = "Template:"
        # a minimal, deliberately-"no conversion" stand-in matching
        # the one actually shipped in extract.py's own `modules` dict
        # -- kept separate here so this test doesn't depend on that
        # dict's own exact contents remaining unchanged elsewhere.
        ex.modules['convert'] = {
            'convert': lambda x, u, *rest: x + ' ' + u,
        }

    def get_result(self, article_text):
        extractor = Extractor(1, "1", "https://test.wikipedia.org/wiki?curid=1",
                               "Test Article", [article_text], templates=self.templates, redirects=self.redirects)
        return extractor.clean_text(article_text, expand_templates=True)


class AliasTemplateNameTests(SharpInvokeAliasTestCase):
    """The core regression: a template invoking #invoke under a name
    OTHER than the function it calls.
    """

    def test_same_named_template_works(self):
        # Template:Convert invoking "convert" -- names match. This
        # passed even under the old, broken lookup logic, since the
        # guessed title ("Template:Convert", derived from the
        # function name "convert") happened to coincide with the
        # template's actual name. Included as a baseline, not as the
        # regression test itself.
        ex.define_template('Template:Convert', ['{{#invoke:convert|convert}}'], self.templates, self.redirects)
        result = self.get_result('Value: {{convert|5|km}}')
        self.assertEqual(result, ['Value: 5 km'])

    def test_differently_named_alias_template_works(self):
        # Template:Cvt invoking the SAME "convert" function under a
        # different name -- this is the actual bug. The old lookup
        # guessed "Template:Convert" (from the function name) and
        # never found a match against the frame stack's real entry,
        # "Template:Cvt" -- silently failing to empty output every
        # time, regardless of the underlying function being identical
        # and correctly registered.
        ex.define_template('Template:Cvt', ['{{#invoke:convert|convert}}'], self.templates, self.redirects)
        result = self.get_result('Value: {{cvt|5|km}}')
        self.assertEqual(result, ['Value: 5 km'])

    def test_original_buffalo_sentence(self):
        # The exact real-world sentence that surfaced this whole
        # investigation.
        ex.define_template('Template:Cvt', ['{{#invoke:convert|convert}}'], self.templates, self.redirects)
        text = ("Buffalo's lowest recorded temperature was {{cvt|−20|°F|0}}, "
                 "which occurred twice: on February 9, 1934, and February 2, 1961.")
        result = self.get_result(text)
        self.assertEqual(
            result,
            ["Buffalo's lowest recorded temperature was −20 °F, which occurred "
             "twice: on February 9, 1934, and February 2, 1961."])

    def test_multiple_differently_named_aliases_of_the_same_function(self):
        # Not special-cased to "cvt" specifically -- any number of
        # differently-named templates invoking the same function
        # should all work identically.
        ex.define_template('Template:Convert', ['{{#invoke:convert|convert}}'], self.templates, self.redirects)
        ex.define_template('Template:Cvt', ['{{#invoke:convert|convert}}'], self.templates, self.redirects)
        ex.define_template('Template:Convert2', ['{{#invoke:convert|convert}}'], self.templates, self.redirects)
        for name in ('convert', 'cvt', 'convert2'):
            with self.subTest(template=name):
                result = self.get_result(f'Value: {{{{{name}|5|km}}}}')
                self.assertEqual(result, ['Value: 5 km'])


class FrameStackOrderingTests(SharpInvokeAliasTestCase):
    """Confirms the fix genuinely uses stack position (innermost,
    currently-expanding invocation), not just "any name at all" --
    distinguishing between two different templates' own parameters
    when one invokes #invoke and the other doesn't.
    """

    def test_uses_the_directly_enclosing_invocation_not_an_outer_one(self):
        # Outer template has its own, different params and does NOT
        # itself call #invoke -- it calls Cvt, which does. The
        # #invoke call must pick up Cvt's own params (5, km), not
        # Wrapper's (999, mi).
        ex.define_template('Template:Cvt', ['{{#invoke:convert|convert}}'], self.templates, self.redirects)
        ex.define_template('Template:Wrapper', ['See: {{cvt|{{{1}}}|{{{2}}}}}'], self.templates, self.redirects)
        result = self.get_result('Value: {{wrapper|5|km}}')
        self.assertEqual(result, ['Value: See: 5 km'])


if __name__ == '__main__':
    unittest.main()
