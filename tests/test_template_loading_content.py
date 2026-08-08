"""
A dedicated, broader test of load_templates() itself: given a
realistic, multi-template dump -- mixing plain parameter substitution,
default-valued parameters, a redirect and its target, includeonly/
noinclude documentation separation, and non-ASCII (Urdu) content, all
in one file -- verifies that every loaded template's stored content is
EXACTLY right, not just that loading doesn't crash or that some single
feature in isolation works.

Most of the existing test suite exercises one specific mechanism at a
time (test_redirect_keywords.py for redirect detection specifically,
test_template_boundary_newline_collapse.py for one particular
whitespace fix, and so on). This file is deliberately different in
shape: one realistic-looking templates file, loaded once through the
real load_templates() pipeline (not define_template() called directly
piece by piece), with every single entry's exact final content
asserted together -- closer to what a real templates file actually
looks like, and a regression net for the pipeline as a whole rather
than any one fix within it.

Fixture note: every <page>'s <text> tag opens cleanly on its own line
with nothing else crammed onto that same line, and closes on its own
line too -- the one-tag-per-line shape every real MediaWiki dump
actually uses, and the same shape this project's own tooling assumes
throughout. A tag sharing a line with other content can silently
change which lastindex group tagRE's match falls into (confirmed
directly, more than once, while building fixtures for other tests in
this project) -- worth stating plainly here since it's an easy mistake
to reintroduce in a new fixture.

Run with:
    python -m unittest tests.test_template_loading_content -v
or, from the tests/ directory:
    python -m unittest test_template_loading_content -v
"""

import io
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we
import wikiextractor.extract as ex


# A realistic multi-template dump: normal substitution, a
# default-valued parameter, a redirect chain, includeonly/noinclude
# documentation separation, and non-ASCII content -- one file,
# mirroring what a real --templates file actually contains rather
# than one template per test.
_TEMPLATES_XML = '''<mediawiki>
<siteinfo>
<namespaces>
<namespace key="0" case="first-letter" />
<namespace key="10" case="first-letter">Template</namespace>
</namespaces>
</siteinfo>
<page>
<title>Template:Greeting</title>
<ns>10</ns>
<id>100</id>
<revision>
<id>1000</id>
<text>Hello, {{{1}}}!</text>
</revision>
</page>
<page>
<title>Template:Infobox person</title>
<ns>10</ns>
<id>101</id>
<revision>
<id>1001</id>
<text>Born: {{{birth|unknown}}}, Died: {{{death|unknown}}}</text>
</revision>
</page>
<page>
<title>Template:Old name</title>
<ns>10</ns>
<id>102</id>
<revision>
<id>1002</id>
<text>#REDIRECT [[Template:New name]]</text>
</revision>
</page>
<page>
<title>Template:New name</title>
<ns>10</ns>
<id>103</id>
<revision>
<id>1003</id>
<text>the real, current content</text>
</revision>
</page>
<page>
<title>Template:With docs</title>
<ns>10</ns>
<id>104</id>
<revision>
<id>1004</id>
<text>
<includeonly>real transcluded content</includeonly>
<noinclude>
This documentation should never appear in extracted output.
</noinclude>
</text>
</revision>
</page>
<page>
<title>Template:جغرافیہ</title>
<ns>10</ns>
<id>105</id>
<revision>
<id>1005</id>
<text>یہ ایک ٹیسٹ سانچہ ہے</text>
</revision>
</page>
</mediawiki>
'''


class TemplateLoadingContentTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Loaded once for the whole class -- every test below only
        # reads the result, none of them mutate it, so there's no
        # cross-test isolation risk in sharing this single load.
        we.templateNamespace = ''
        cls.templatePrefix = ''
        with io.StringIO(_TEMPLATES_XML) as f:
            for line in f:
                m = we.tagRE.search(line)
                if not m:
                    continue
                tag = m.group(2)
                if tag == 'namespace' and 'key="10"' in line:
                    we.templateNamespace = m.group(3)
                    cls.templatePrefix = we.templateNamespace + ':'
                elif tag == '/siteinfo':
                    break
        cls.templates = {}
        cls.redirects = {}
        with io.StringIO(_TEMPLATES_XML) as f:
            cls.count, cls.templatePrefix = we.load_templates(
                f, templates=cls.templates, redirects=cls.redirects,
                template_prefix=cls.templatePrefix)

    def test_reported_count_matches_every_page_including_the_redirect(self):
        # 6 <page> elements total: 5 real templates + 1 redirect --
        # load_templates()'s own count includes the redirect page too
        # (it's still a processed Template-namespace page, just one
        # that ends up in redirects instead of templates).
        self.assertEqual(self.count, 6)

    def test_plain_parameter_substitution_template_stored_verbatim(self):
        # Templates are stored as their raw, unexpanded wikitext --
        # {{{1}}} is not resolved at load time at all, only later,
        # per-call, during expandTemplate().
        self.assertEqual(self.templates['Template:Greeting'], 'Hello, {{{1}}}!')

    def test_default_valued_parameter_template_stored_verbatim(self):
        self.assertEqual(
            self.templates['Template:Infobox person'],
            'Born: {{{birth|unknown}}}, Died: {{{death|unknown}}}')

    def test_redirect_page_recorded_as_a_redirect_not_a_template(self):
        self.assertEqual(self.redirects.get('Template:Old name'), 'Template:New name')
        self.assertNotIn('Template:Old name', self.templates)

    def test_redirect_target_itself_is_an_ordinary_stored_template(self):
        self.assertEqual(self.templates['Template:New name'], 'the real, current content')

    def test_includeonly_content_kept_noinclude_docs_stripped(self):
        # The documentation text must never appear at all; the
        # includeonly-wrapped real content must survive with its own
        # tags removed. Surrounding newlines from the fixture's own
        # one-tag-per-line layout survive too -- this fix only ever
        # collapsed a newline immediately after </noinclude>, not
        # every newline adjacent to includeonly/noinclude generally
        # (see test_template_boundary_newline_collapse.py for that
        # fix's own, narrower test coverage).
        content = self.templates['Template:With docs']
        self.assertIn('real transcluded content', content)
        self.assertNotIn('documentation', content)
        self.assertNotIn('<includeonly>', content)
        self.assertNotIn('<noinclude>', content)

    def test_non_ascii_template_content_stored_correctly(self):
        self.assertEqual(self.templates['Template:جغرافیہ'], 'یہ ایک ٹیسٹ سانچہ ہے')

    def test_exact_full_template_set_no_extras_no_omissions(self):
        # Belt-and-suspenders on top of the individual assertions
        # above: confirms the loaded set is EXACTLY these five titles,
        # not five-plus-something-unexpected.
        self.assertEqual(set(self.templates.keys()), {
            'Template:Greeting',
            'Template:Infobox person',
            'Template:New name',
            'Template:With docs',
            'Template:جغرافیہ',
        })


if __name__ == '__main__':
    unittest.main()
