"""
Tests for splitNamedPart(): finding the "=" that separates a name from
its value without being fooled by one belonging to a nested call.

splitParts() has always been careful not to split at a "|" inside
{{...}}, {{{...}}} or [[...]] -- a nested call's own argument
separators are none of the outer call's business. The same has to hold
for "=", and it did not: sharp_switch() used a plain
str.split('=', 1).

bhwiki's राहुल सांकृत्यायन is what that cost. Template:MONTHNUMBER
ends its #switch with a positional default case that is itself a
parser-function call taking a named argument:

    | {{#ifexpr: {{अंक परिवर्तन|{{{1}}}|प्रकार=अरबी}} < 0 | ... | ... }}

Splitting that at its first "=" cut the part in half in the middle of
the nested call, giving

    "{{#ifexpr:{{अंक परिवर्तन|4|प्रकार"    as the case label
    "अरबी}}<0|...|...}}"                    as its result

and since the mangled label still contained a bare "|4|",
sharp_switch()'s pipe-separated-label check matched it against the
primary "4" and returned the second half verbatim.

That wrecked the page twice over. The returned fragment carried
unbalanced "}}" into the enclosing {{Infobox writer}} argument stream,
so brace matching lost its place and the infobox's raw source spilled
into the article from "| 3=" onwards:

    &lt;0||}}-09)9 1893
     | 3= पंदहा गाँव, आजमगढ़ जिला, उत्तर प्रदेश, ब्रिटिश भारत

Run with:
    python -m unittest tests.test_split_named_part -v
or, from the tests/ directory:
    python -m unittest test_split_named_part -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class SplitNamedPartTests(unittest.TestCase):
    """The helper in isolation."""

    def test_a_positional_part_is_returned_whole(self):
        self.assertEqual(ex.splitNamedPart('just a value'), ['just a value'])

    def test_a_named_part_splits_at_its_equals(self):
        self.assertEqual(ex.splitNamedPart('name=value'), ['name', 'value'])

    def test_only_the_first_equals_separates(self):
        self.assertEqual(ex.splitNamedPart('a=b=c'), ['a', 'b=c'])

    def test_surrounding_space_is_left_for_the_caller_to_strip(self):
        self.assertEqual(ex.splitNamedPart(' n = v '), [' n ', ' v '])

    def test_an_empty_part(self):
        self.assertEqual(ex.splitNamedPart(''), [''])

    def test_an_equals_inside_a_template_call_does_not_separate(self):
        self.assertEqual(ex.splitNamedPart('{{t|k=v}}'), ['{{t|k=v}}'])

    def test_an_equals_inside_a_tplarg_default_does_not_separate(self):
        self.assertEqual(ex.splitNamedPart('{{{1|k=v}}}'), ['{{{1|k=v}}}'])

    def test_an_equals_inside_a_link_does_not_separate(self):
        self.assertEqual(ex.splitNamedPart('[[a|b=c]]'), ['[[a|b=c]]'])

    def test_an_equals_before_a_nested_call_still_separates(self):
        self.assertEqual(ex.splitNamedPart('n={{t|k=v}}'), ['n', '{{t|k=v}}'])

    def test_an_equals_after_a_nested_call_still_separates(self):
        self.assertEqual(ex.splitNamedPart('{{t|k=v}}=tail'),
                         ['{{t|k=v}}', 'tail'])

    def test_an_equals_between_two_nested_calls_still_separates(self):
        self.assertEqual(ex.splitNamedPart('{{a|x=1}}={{b|y=2}}'),
                         ['{{a|x=1}}', '{{b|y=2}}'])

    def test_nesting_two_deep(self):
        self.assertEqual(ex.splitNamedPart('{{a|{{b|c=d}}|e=f}}'),
                         ['{{a|{{b|c=d}}|e=f}}'])

    def test_the_monthnumber_default_case_survives_intact(self):
        part = ('{{#ifexpr:{{अंक परिवर्तन|{{{1}}}|प्रकार=अरबी}}<0'
                '|{{#expr:1}}|{{#expr:2}}}}')
        self.assertEqual(ex.splitNamedPart(part), [part])

    def test_unbalanced_braces_do_not_hide_a_real_equals(self):
        # Nothing pairs up here, so there is no nested span to skip and
        # the "=" is the caller's after all.
        self.assertEqual(ex.splitNamedPart('{{oops|n=v'), ['{{oops|n', 'v'])


class SharpSwitchNestedEqualsTests(unittest.TestCase):
    """The regression the helper exists for."""

    def test_a_case_whose_result_contains_a_nested_named_argument(self):
        self.assertEqual(
            ex.sharp_switch('b', 'a=A', 'b={{t|k=v}}'), '{{t|k=v}}')

    def test_a_positional_default_that_is_a_call_with_a_named_argument(self):
        # Before the fix this split into a bogus label and result, and
        # returned the result half.
        self.assertEqual(
            ex.sharp_switch('zzz', 'a=A', '{{t|k=v}}'), '{{t|k=v}}')

    def test_the_bhwiki_case_does_not_match_on_a_mangled_label(self):
        # "4" appears inside the nested call's arguments. Split
        # correctly, the part is one positional default and no label
        # can match it by accident.
        default = '{{#ifexpr:{{f|4|p=q}}<0|neg|pos}}'
        self.assertEqual(ex.sharp_switch('4', 'january=1', default), default)


class EndToEndTests(unittest.TestCase):
    """Through a real Extractor, since the damage was to brace
    matching in the enclosing call rather than to #switch's own
    output."""

    @staticmethod
    def clean(wikitext, templates):
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                                 templates=templates, templatePrefix='Template:')
        return '\n'.join(extractor.clean_text(wikitext, expand_templates=True))

    def test_an_enclosing_call_is_not_derailed_by_the_fragment(self):
        # Template:Wrap stands in for {{Infobox writer}}: a call whose
        # later arguments followed the one holding the broken #switch.
        # The fragment that #switch used to return carried unbalanced
        # "}}", which ended the enclosing call early and spilled the
        # rest of its source into the article.
        templates = {
            'Template:Pick': ('{{#switch:{{{1|}}}'
                              '|january=1'
                              '|{{#ifexpr:{{{2|}}}<0|neg|pos}}'
                              '}}'),
            'Template:Wrap': 'seen:{{{1|}}}/{{{2|}}}',
        }
        result = self.clean(
            'before {{Wrap|1={{Pick|4|x=y}}|2=second argument}} after',
            templates)
        self.assertNotIn('}}', result)
        self.assertNotIn('| 2=', result)
        self.assertIn('second argument', result)
        self.assertIn('before', result)
        self.assertIn('after', result)


if __name__ == '__main__':
    unittest.main()
