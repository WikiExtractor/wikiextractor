"""
Tests for how a template call's arguments are split into names and
values.

MediaWiki decides which part of an argument is a name and which is a
value on the text as written, and only then expands the two halves.
Splitting after expansion instead lets a nested call's own output
name a parameter that the editor never wrote.

slwiki's Sekunda (page id 164) quotes the 1956 definition of the
second as

    {{navedek|delež {{frac|31.556.925,9747}} tropskega leta}}

and {{frac}} expands to markup starting
'<templatestyles src="Fraction/styles.css" />'. Splitting that
expansion at its first "=" -- inside an HTML attribute that does not
appear in the wikitext at all -- turned the quotation into a
parameter named '<templatestyles src', leaving {{{1}}} undefined and
the quotation absent from the article.

An "=" the editor did type still names a parameter, even inside an
HTML attribute or a URL query string. MediaWiki behaves that way too,
which is why its own documentation tells editors to write "1="
around such values.

The end-to-end case at the bottom also needs #ifeq's empty-operand
comparison, which Template:Main other depends on and which
test_sharp_ifeq_empty_operand covers.

Run with:
    python -m unittest tests.test_template_argument_names -v
or, from the tests/ directory:
    python -m unittest test_template_argument_names -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def expand(wikitext, templates):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates=templates, templatePrefix='Template:')
    return extractor.expandTemplates(wikitext)


class ArgumentSplittingTests(unittest.TestCase):

    templates = {
        'Template:Frac': '<span class="frac">1&frasl;{{{1}}}</span>',
        'Template:Show': '[{{{1|MISSING}}}]',
        'Template:Both': 'n={{{n|MISSING}}} 1={{{1|MISSING}}}',
    }

    def test_a_plain_positional_argument(self):
        self.assertEqual(expand('{{Show|plain}}', self.templates), '[plain]')

    def test_an_equals_produced_by_a_nested_call_does_not_name_anything(self):
        # The heart of it: {{Frac}}'s output carries class="frac".
        self.assertEqual(
            expand('{{Show|a {{Frac|7}} b}}', self.templates),
            '[a <span class="frac">1&frasl;7</span> b]')

    def test_a_nested_call_alone_stays_positional(self):
        self.assertEqual(
            expand('{{Both|{{Frac|7}}}}', self.templates),
            'n=MISSING 1=<span class="frac">1&frasl;7</span>')

    def test_a_literal_equals_still_names_a_parameter(self):
        self.assertEqual(expand('{{Both|n=v}}', self.templates),
                         'n=v 1=MISSING')

    def test_a_named_value_containing_a_nested_call_is_expanded(self):
        self.assertEqual(
            expand('{{Both|n={{Frac|7}}}}', self.templates),
            'n=<span class="frac">1&frasl;7</span> 1=MISSING')

    def test_an_explicit_number_lets_a_value_keep_its_equals(self):
        self.assertEqual(expand('{{Show|1=a=b}}', self.templates), '[a=b]')

    def test_an_equals_inside_a_nested_call_does_not_split_either(self):
        self.assertEqual(
            expand('{{Both|{{Frac|{{Show|1=x=y}}}}}}', self.templates),
            'n=MISSING 1=<span class="frac">1&frasl;[x=y]</span>')

    def test_a_link_argument_survives(self):
        self.assertEqual(expand('{{Show|[[A|B]]}}', self.templates), '[[[A|B]]]')

    def test_a_typed_equals_does_name_a_parameter(self):
        # MediaWiki behaves this way too, which is why its documentation
        # tells editors to write "1=" around a URL or an HTML attribute.
        self.assertEqual(
            expand('{{Show|http://x/?a=1}}', self.templates), '[MISSING]')


class SekundaTests(unittest.TestCase):
    """The page, reduced: both faults together."""

    templates = {
        'Template:Main other': ('{{#switch: {{#ifeq:{{NAMESPACE}}|{{ns:0}}'
                                '|main|other}}'
                                '|main = {{{1|}}}'
                                '|other'
                                '|#default = {{{2|}}}'
                                '}}'),
        'Template:Frac': '<span class="frac">1&frasl;{{{1}}}</span>',
        'Template:Quote': 'QUOTE: {{{text|}}}',
        'Template:Navedek': ('{{Main other'
                             '|1={{Quote|text={{{text|{{{1|}}}}}}}}'
                             '|2=<table style="width: 20px; '
                             'vertical-align: bottom; border: none;">'
                             '{{{1|}}}</table>'
                             '}}'),
    }

    def clean(self, wikitext):
        extractor = ex.Extractor(164, "164", "https://x", "Sekunda", [],
                                 templates=self.templates,
                                 templatePrefix='Template:')
        return '\n'.join(extractor.clean_text(wikitext, expand_templates=True))

    def test_no_css_reaches_the_article(self):
        result = self.clean('{{navedek|delež {{frac|31.556.925,9747}} '
                            'tropskega leta}}')
        self.assertNotIn('vertical-align', result)
        self.assertNotIn('border: none', result)

    def test_the_quotation_reaches_the_article(self):
        result = self.clean('{{navedek|delež {{frac|31.556.925,9747}} '
                            'tropskega leta}}')
        self.assertIn('delež', result)
        self.assertIn('31.556.925,9747', result)
        self.assertIn('tropskega leta', result)


if __name__ == '__main__':
    unittest.main()
