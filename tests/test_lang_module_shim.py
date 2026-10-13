"""
Tests for the Module:Lang shim behind #invoke.

Templates built on Module:Lang wrap a term in its own language or
script, inline and mid-sentence: {{lang}}, {{lang-xx}},
{{transliteration}}, {{transl}}, {{wikt-lang}}. On enwiki all of them
are Lua, and a Lua module this project cannot run expands to nothing,
so every such term disappears and takes its sentence's meaning with
it. enwiki's Altruism read

    In Hinduism, selflessness (), love (), kindness (), and
    forgiveness () are considered as the highest acts of humanity
    or " ".

where {{transliteration|sa|Atmatyag}} and its siblings belong.

The rendered page wraps each term in a <span> carrying the language
tag and nothing else, so for plain text the term is the whole of the
output. The signatures differ in where that term sits, which is what
these cover.

Run with:
    python -m unittest tests.test_lang_module_shim -v
or, from the tests/ directory:
    python -m unittest test_lang_module_shim -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


TEMPLATES = {
    # The real enwiki bodies, which is where the module and function
    # names come from.
    'Template:Lang': '{{#invoke:Lang|{{{fn|lang}}}}}',
    'Template:Transliteration': '{{#invoke:lang|xlit}}',
    'Template:Transl': '{{#invoke:lang|xlit}}',
    'Template:Langx': '{{#invoke:lang|langx}}',
    'Template:Lang-grc': '{{#invoke:Lang|lang_xx_italic|code=grc}}',
    'Template:Lang-sa': '{{#invoke:Lang|lang_xx_inherit|code=sa}}',
    'Template:Wikt-lang': ('{{#invoke:Wikt-lang|wiktlang|{{{1|}}}|{{{2|}}}'
                           '|{{{3|}}}}}'),
}


def expand(wikitext):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates=TEMPLATES, templatePrefix='Template:')
    return extractor.expandTemplates(wikitext)


class LangTests(unittest.TestCase):
    """{{lang|<code>|<text>}} -- the text is the second argument."""

    def test_a_term_in_another_script(self):
        self.assertEqual(expand('{{lang|sa|आत्मत्याग}}'), 'आत्मत्याग')

    def test_a_named_argument_does_not_displace_the_text(self):
        self.assertEqual(expand('{{lang|de|Fahrvergnügen|italic=no}}'),
                         'Fahrvergnügen')

    def test_an_empty_text_gives_empty(self):
        self.assertEqual(expand('{{lang|de|}}'), '')

    def test_the_language_code_is_never_the_output(self):
        self.assertNotIn('sa', expand('{{lang|sa|आत्मत्याग}}'))


class LangxTests(unittest.TestCase):
    """{{langx|<code>|<text>|<transliteration>|<gloss>}} -- enwiki's
    newer form, which carries the romanization and a gloss alongside
    the term. Only the term is emitted."""

    def test_code_and_text(self):
        self.assertEqual(expand('{{langx|uk|Зміїний}}'), 'Зміїний')

    def test_a_romanization_does_not_displace_the_term(self):
        self.assertEqual(expand('{{langx|grc|Ἀχιλλεύς|Achilleús}}'),
                         'Ἀχιλλεύς')

    def test_a_skipped_romanization_and_a_gloss(self):
        self.assertEqual(expand('{{langx|grc|Ἄρκη||swift|label=none}}'),
                         'Ἄρκη')


class TransliterationTests(unittest.TestCase):
    """The romanization standard is optional and sits between the code
    and the text, so the text is the last positional argument."""

    def test_code_and_text(self):
        self.assertEqual(expand('{{transliteration|sa|Atmatyag}}'), 'Atmatyag')

    def test_code_standard_and_text(self):
        self.assertEqual(expand('{{transliteration|ar|DIN|al-Jazira}}'),
                         'al-Jazira')

    def test_the_transl_alias(self):
        self.assertEqual(expand('{{transl|sa|Prema}}'), 'Prema')


class LangXxTests(unittest.TestCase):
    """{{lang-xx|<text>}} -- the language is in the template name, so
    the text is the first argument."""

    def test_italic_variant(self):
        self.assertEqual(expand('{{lang-grc|ἀλήθεια}}'), 'ἀλήθεια')

    def test_inherit_variant(self):
        self.assertEqual(expand('{{lang-sa|धर्म}}'), 'धर्म')


class WiktLangTests(unittest.TestCase):

    def test_code_and_text(self):
        self.assertEqual(expand('{{wikt-lang|sa|धर्म}}'), 'धर्म')


class ModuleNameCaseTests(unittest.TestCase):
    """A title's first letter is capitalized either way, so
    {{#invoke:Lang}} and {{#invoke:lang}} name one module."""

    def test_both_spellings_resolve(self):
        for body in ('{{#invoke:Lang|lang}}', '{{#invoke:lang|lang}}'):
            with self.subTest(body=body):
                templates = {'Template:L': body}
                extractor = ex.Extractor(1, "1", "https://x", "T", [],
                                         templates=templates,
                                         templatePrefix='Template:')
                self.assertEqual(extractor.expandTemplates('{{L|sa|धर्म}}'),
                                 'धर्म')


class AltruismTests(unittest.TestCase):
    """The page, reduced."""

    def test_the_sentence_keeps_its_terms(self):
        extractor = ex.Extractor(1, "1", "https://x", "Altruism", [],
                                 templates=TEMPLATES, templatePrefix='Template:')
        result = '\n'.join(extractor.clean_text(
            'In Hinduism, selflessness ({{transliteration|sa|Atmatyag}}), '
            'love ({{transliteration|sa|Prema}}) are considered as the '
            'highest acts of humanity or "{{transliteration|sa|Manushyattva}}".',
            expand_templates=True))
        self.assertEqual(
            result,
            'In Hinduism, selflessness (Atmatyag), love (Prema) are '
            'considered as the highest acts of humanity or "Manushyattva".')


class UnshimmedModulesStillExpandToNothing(unittest.TestCase):
    """Only the functions listed in modules are covered; anything else
    is unchanged, since guessing at a module's output is worse than
    leaving it empty."""

    def test_an_unknown_module(self):
        templates = {'Template:X': '{{#invoke:Separated entries|br}}'}
        extractor = ex.Extractor(1, "1", "https://x", "T", [],
                                 templates=templates, templatePrefix='Template:')
        self.assertEqual(extractor.expandTemplates('{{X|a|b}}'), '')

    def test_an_unknown_function_of_a_known_module(self):
        templates = {'Template:X': '{{#invoke:Lang|name_from_tag}}'}
        extractor = ex.Extractor(1, "1", "https://x", "T", [],
                                 templates=templates, templatePrefix='Template:')
        self.assertEqual(extractor.expandTemplates('{{X|sa}}'), '')


if __name__ == '__main__':
    unittest.main()
