"""
Tests for MediaWiki variables ({{PAGENAME}}, {{SUBPAGENAME}},
{{REVISIONID}} and the rest of MagicWords.names).

Two properties:

Recognition. expandTemplate() resolves a variable before it tries any
template lookup, so a wiki page whose name matches one does not
shadow it, and a variable nothing has assigned a value to renders as
the empty string rather than being chased through the template
machinery. Most of the names carry no value, so they render empty
either way; what changes is that they no longer reach the lookup,
which is what produced entries like "Template:NAMESPACENUMBER" and
"टेम्पलेट:SUBPAGENAME" in the never-found report of the tooling that
hunts for missing templates.

Case. Variables are case sensitive and written in uppercase, so
{{PAGENAME}} is a variable and {{Pagename}} is a link to a template of
that name. Matching them loosely would let any of the 77 names shadow
a real template that happened to share a name. Across en/ja/bh/skr
dumps all 249 real invocations are canonical uppercase, so strictness
costs nothing.

Run with:
    python -m unittest tests.test_magic_words -v
or, from the tests/ directory:
    python -m unittest test_magic_words -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class RecordingTemplates(dict):
    """A templates mapping that records the titles looked up in it.

    Extractor takes any object supporting `in`/`[]` for its templates
    argument, which makes this the cheapest way to see whether a title
    reached the template machinery at all.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.looked_up = []

    def __contains__(self, key):
        self.looked_up.append(key)
        return super().__contains__(key)


def make_extractor(templates=None, title='Test Article', page_id=1, revid='1'):
    return ex.Extractor(page_id, revid, 'https://x', title, [],
                        templates=templates if templates is not None else {},
                        templatePrefix='Template:')


def expand(wikitext, templates=None, title='Test Article'):
    """Run wikitext through clean_text, which is what assigns the
    variables that do carry a value."""
    extractor = make_extractor(templates, title)
    return '\n'.join(extractor.clean_text(wikitext, expand_templates=True))


class MagicWordsContainerTests(unittest.TestCase):

    def test_every_name_has_an_uppercase_surface_form(self):
        words = ex.MagicWords()
        for name in ex.MagicWords.names:
            with self.subTest(name=name):
                self.assertIn(name.upper(), words)

    def test_known_surface_forms_are_recognized(self):
        words = ex.MagicWords()
        for name in ('PAGENAME', 'SUBPAGENAME', 'NAMESPACENUMBER',
                     'TALKPAGENAME', 'FULLPAGENAMEE', 'REVISIONID', '!'):
            with self.subTest(name=name):
                self.assertIn(name, words)

    def test_the_lowercase_id_is_not_a_surface_form(self):
        words = ex.MagicWords()
        for name in ('pagename', 'subpagename', 'Pagename', 'PageName'):
            with self.subTest(name=name):
                self.assertNotIn(name, words)

    def test_an_unlisted_name_is_not_recognized(self):
        words = ex.MagicWords()
        self.assertNotIn('NOTAVARIABLE', words)
        self.assertNotIn('INFOBOX', words)

    def test_a_recognized_name_with_no_value_gives_the_empty_string(self):
        # Never None: the result is concatenated straight into the
        # surrounding text.
        words = ex.MagicWords()
        self.assertEqual(words['SUBPAGENAME'], '')
        self.assertEqual(words['NAMESPACENUMBER'], '')

    def test_an_unrecognized_name_also_gives_the_empty_string(self):
        self.assertEqual(ex.MagicWords()['NOTAVARIABLE'], '')

    def test_the_prepopulated_pipe_keeps_its_value(self):
        self.assertEqual(ex.MagicWords()['!'], '|')

    def test_assignment_is_readable_back(self):
        words = ex.MagicWords()
        words['PAGENAME'] = 'Some Article'
        self.assertEqual(words['PAGENAME'], 'Some Article')


class VariableResolutionTests(unittest.TestCase):

    def test_assigned_variables_expand_to_their_value(self):
        self.assertEqual(expand('{{PAGENAME}}', title='Some Article'), 'Some Article')

    def test_the_pipe_variable_expands(self):
        # In context, not alone on its own line: clean() drops a line
        # made up entirely of punctuation.
        self.assertEqual(expand('a{{!}}b'), 'a|b')

    def test_unassigned_variables_expand_to_nothing(self):
        # Still carrying no value: these need the wiki's own siteinfo
        # or a live site statistic.
        for name in ('TALKPAGENAME', 'TALKSPACE', 'SITENAME', 'SERVER',
                     'CONTENTLANGUAGE', 'NUMBEROFARTICLES', 'CURRENTMONTHNAME'):
            with self.subTest(name=name):
                self.assertEqual(expand('a{{%s}}b' % name), 'ab')

    def test_surrounding_text_is_left_alone(self):
        self.assertEqual(expand('before {{SITENAME}} after'), 'before after')


class CaseSensitivityTests(unittest.TestCase):
    """{{PAGENAME}} is a variable; {{Pagename}} and {{pagename}} name a
    template."""

    def test_an_off_case_spelling_does_not_resolve_as_a_variable(self):
        for spelling in ('{{pagename}}', '{{Pagename}}', '{{PageName}}'):
            with self.subTest(spelling=spelling):
                self.assertNotIn('Some Article', expand(spelling, title='Some Article'))

    def test_an_off_case_spelling_reaches_the_template_lookup(self):
        templates = RecordingTemplates()
        make_extractor(templates).clean_text('{{Pagename}}', expand_templates=True)
        self.assertIn('Template:Pagename', templates.looked_up)

    def test_a_template_sharing_a_variables_name_expands_when_written_off_case(self):
        # The shadowing this strictness exists to prevent.
        result = expand('{{Pagename}}', {'Template:Pagename': 'template body'},
                        title='Some Article')
        self.assertIn('template body', result)

    def test_an_off_case_unassigned_name_is_not_treated_as_a_variable(self):
        result = expand('{{Subpagename}}', {'Template:Subpagename': 'template body'})
        self.assertIn('template body', result)


class NoTemplateLookupTests(unittest.TestCase):
    """The point of recognizing them: a variable never reaches the
    template machinery."""

    def test_an_unassigned_variable_is_not_looked_up(self):
        templates = RecordingTemplates()
        make_extractor(templates).clean_text('{{SUBPAGENAME}}', expand_templates=True)
        self.assertEqual(templates.looked_up, [])

    def test_an_assigned_variable_is_not_looked_up(self):
        templates = RecordingTemplates()
        make_extractor(templates).clean_text('{{PAGENAME}}', expand_templates=True)
        self.assertEqual(templates.looked_up, [])

    def test_an_ordinary_template_is_still_looked_up(self):
        templates = RecordingTemplates({'Template:Real': 'real body'})
        result = make_extractor(templates).clean_text('{{Real}}', expand_templates=True)
        self.assertIn('Template:Real', templates.looked_up)
        self.assertIn('real body', '\n'.join(result))

    def test_an_undefined_template_is_still_looked_up_and_yields_nothing(self):
        templates = RecordingTemplates()
        result = make_extractor(templates).clean_text('a{{Missing}}b', expand_templates=True)
        self.assertIn('Template:Missing', templates.looked_up)
        self.assertIn('ab', '\n'.join(result))


class PrecedenceTests(unittest.TestCase):
    """A page named after a variable does not shadow it, matching
    MediaWiki."""

    def test_a_template_named_after_an_assigned_variable_is_ignored(self):
        result = expand('{{PAGENAME}}', {'Template:PAGENAME': 'template body'},
                        title='Some Article')
        self.assertIn('Some Article', result)
        self.assertNotIn('template body', result)

    def test_a_template_named_after_an_unassigned_variable_is_ignored(self):
        result = expand('a{{SITENAME}}b', {'Template:SITENAME': 'template body'})
        self.assertIn('ab', result)
        self.assertNotIn('template body', result)

    def test_a_template_whose_name_merely_resembles_one_still_expands(self):
        # PAGENAMEBASE is not a variable; BASEPAGENAME is. A real
        # template by that name has to keep working.
        result = expand('{{PAGENAMEBASE}}', {'Template:PAGENAMEBASE': 'template body'})
        self.assertIn('template body', result)


class PageNameValueTests(unittest.TestCase):
    """Every page an Extractor sees is main-namespace, since
    collect_pages() selects on the page's own <ns>. That fixes the
    namespace as empty and the page name as the whole title, and
    Wikimedia's main namespace has no subpages, so the sub, base and
    root names are the page name too."""

    TITLE = 'Kill Bill: Volume 1'

    def values(self, title=None):
        extractor = make_extractor(title=title or self.TITLE)
        extractor.clean_text('', expand_templates=True)
        return extractor.magicWords

    def test_the_page_name_family_is_the_title(self):
        words = self.values()
        for name in ('PAGENAME', 'FULLPAGENAME', 'BASEPAGENAME',
                     'SUBPAGENAME', 'ROOTPAGENAME', 'SUBJECTPAGENAME'):
            with self.subTest(name=name):
                self.assertEqual(words[name], self.TITLE)

    def test_the_namespace_is_empty_even_when_the_title_has_a_colon(self):
        # The colon in "Kill Bill: Volume 1" is part of the title, not
        # a namespace prefix.
        words = self.values()
        self.assertEqual(words['NAMESPACE'], '')
        self.assertEqual(words['NAMESPACEE'], '')
        self.assertEqual(words['SUBJECTSPACE'], '')
        self.assertEqual(words['NAMESPACENUMBER'], '0')

    def test_a_title_with_a_slash_keeps_its_whole_name(self):
        # Main-namespace subpages are disabled, so the slash is
        # ordinary punctuation rather than a subpage separator.
        words = self.values('AC/DC')
        self.assertEqual(words['SUBPAGENAME'], 'AC/DC')
        self.assertEqual(words['BASEPAGENAME'], 'AC/DC')
        self.assertEqual(words['ROOTPAGENAME'], 'AC/DC')

    def test_the_ids_come_from_the_page(self):
        extractor = ex.Extractor('18964', '77', 'https://x', 'T', [],
                                 templates={}, templatePrefix='Template:')
        extractor.clean_text('', expand_templates=True)
        self.assertEqual(extractor.magicWords['PAGEID'], '18964')
        self.assertEqual(extractor.magicWords['REVISIONID'], '77')

    def test_an_integer_id_is_rendered_as_text(self):
        extractor = ex.Extractor(18964, 77, 'https://x', 'T', [],
                                 templates={}, templatePrefix='Template:')
        extractor.clean_text('', expand_templates=True)
        self.assertEqual(extractor.magicWords['PAGEID'], '18964')

    def test_they_expand_in_wikitext(self):
        self.assertEqual(expand('{{PAGENAME}} in ns {{NAMESPACENUMBER}}',
                                title='Some Article'),
                         'Some Article in ns 0')


class UrlEncodedVariableTests(unittest.TestCase):
    """The E-suffixed variables: the same value encoded for a URL."""

    def test_spaces_become_underscores(self):
        self.assertEqual(ex.wikiUrlencode('Some Article'), 'Some_Article')

    def test_a_title_colon_and_slash_survive(self):
        self.assertEqual(ex.wikiUrlencode('Kill Bill: Volume 1'),
                         'Kill_Bill:_Volume_1')
        self.assertEqual(ex.wikiUrlencode('AC/DC'), 'AC/DC')

    def test_other_punctuation_is_percent_encoded(self):
        self.assertEqual(ex.wikiUrlencode('AT&T'), 'AT%26T')
        self.assertEqual(ex.wikiUrlencode('"Heroes"'), '%22Heroes%22')

    def test_the_e_variables_carry_the_encoded_title(self):
        extractor = make_extractor(title='Kill Bill: Volume 1')
        extractor.clean_text('', expand_templates=True)
        for name in ('PAGENAMEE', 'FULLPAGENAMEE', 'BASEPAGENAMEE',
                     'SUBPAGENAMEE', 'ROOTPAGENAMEE', 'SUBJECTPAGENAMEE'):
            with self.subTest(name=name):
                self.assertEqual(extractor.magicWords[name], 'Kill_Bill:_Volume_1')

    def test_it_expands_in_wikitext(self):
        self.assertEqual(expand('{{PAGENAMEE}}', title='Some Article'),
                         'Some_Article')


class ParserFunctionsUnaffectedTests(unittest.TestCase):
    """Parser functions are lowercase and carry a colon, so they are
    dispatched separately and none of this reaches them."""

    def test_lowercase_string_function_still_works(self):
        self.assertEqual(expand('{{lc:ABC}}'), 'abc')

    def test_branching_function_still_works(self):
        self.assertEqual(expand('{{#if:x|yes|no}}'), 'yes')

    def test_padleft_still_works(self):
        self.assertEqual(expand('{{padleft:7|3}}'), '007')


class VariablesInsideTemplatesTests(unittest.TestCase):
    """Variables mostly turn up inside a template body rather than in
    article text."""

    def test_a_variable_in_a_template_body_resolves(self):
        templates = {'Template:Header': "'''{{PAGENAME}}'''"}
        self.assertIn('Some Article', expand('{{Header}}', templates, title='Some Article'))

    def test_an_empty_variable_as_a_parser_function_argument(self):
        templates = {'Template:Site': '{{#if:{{SITENAME}}|named|unnamed}}'}
        self.assertIn('unnamed', expand('{{Site}}', templates))

    def test_a_variable_as_a_template_argument(self):
        templates = {'Template:Echo': '{{{1|}}}'}
        self.assertIn('Some Article',
                      expand('{{Echo|{{PAGENAME}}}}', templates, title='Some Article'))


if __name__ == '__main__':
    unittest.main()
