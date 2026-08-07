"""
A dedicated, broader test of full article extraction: given one
realistic article that mixes plain prose, a section heading, a
wikilink, several different kinds of template call (plain
substitution, default-valued parameters, a call resolved through a
redirect, includeonly/noinclude content, and a genuinely undefined
template), runs it through the real collect_pages() ->
Extractor.clean_text() pipeline and asserts the exact resulting lines
-- once with templates loaded, once without.

Most of the existing test suite exercises one specific mechanism in
isolation (one tag, one fix, one edge case per test file). This file
is deliberately the other shape: one realistic article, extracted
whole, with the complete expected output asserted line-for-line in
both configurations -- a broader regression net for how these pieces
actually combine, not a replacement for the narrower, single-mechanism
tests elsewhere.

The with/without-templates split is itself worth testing directly, not
just as a variation: a template call that's the ONLY content on its
own line collapses that whole line out of the output when the
template doesn't expand to anything (undefined template, or
templates not loaded at all) -- but a call that shares its line with
other prose survives, just missing whatever the template would have
contributed. Confirmed directly against real output before writing
these assertions, not predicted by hand -- see
test_using_the_redirect_line_survives_but_its_contribution_disappears
and test_greeting_line_disappears_entirely below for the two sides of
that distinction.

Fixture note: same one-tag-per-line shape as
test_template_loading_content.py, for the same reason -- see that
file's own docstring.

Run with:
    python -m unittest tests.test_article_extraction_content -v
or, from the tests/ directory:
    python -m unittest test_article_extraction_content -v
"""

import io
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we
import wikiextractor.extract as ex
from wikiextractor.extract import Extractor


# One article, several different template-call shapes, plus a
# wikilink and a section heading -- all the templates it calls are
# defined in the same dump, standing in for a realistic single-file
# extraction the way a real dump actually mixes articles and
# templates together.
_ARTICLE_XML = """<mediawiki>
<siteinfo>
<namespaces>
<namespace key="0" case="first-letter" />
<namespace key="10" case="first-letter">Template</namespace>
</namespaces>
</siteinfo>
<page>
<title>Geography</title>
<ns>0</ns>
<id>1</id>
<revision>
<id>1</id>
<text>'''Geography''' is a broad field of study.

{{Greeting|World}}

== Notable figure ==
{{Infobox person|birth=1900|death=1950}}

See also the [[History of geography|related article]].

Using the redirect: {{Old name}}

{{With docs}}

An undefined reference: {{Does not exist}}

Final paragraph of the article.</text>
</revision>
</page>
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
</mediawiki>
"""


class ArticleExtractionContentTestCase(unittest.TestCase):

    def setUp(self):
        we.templateNamespace = ''
        ex.Extractor.templatePrefix = ''
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        with io.StringIO(_ARTICLE_XML) as f:
            for line in f:
                m = we.tagRE.search(line)
                if not m:
                    continue
                tag = m.group(2)
                if tag == 'namespace' and 'key="10"' in line:
                    we.templateNamespace = m.group(3)
                    ex.Extractor.templatePrefix = we.templateNamespace + ':'
                elif tag == '/siteinfo':
                    break

    def extract_article(self, with_templates):
        templates = {}
        redirects = {}
        if with_templates:
            with io.StringIO(_ARTICLE_XML) as f:
                we.load_templates(f, templates=templates, redirects=redirects)

        with io.StringIO(_ARTICLE_XML) as f:
            pages = list(we.collect_pages(f))
        [(page_id, revid, title, page)] = [p for p in pages if p[2] == 'Geography']

        extractor = Extractor(page_id, revid, 'https://test.wikipedia.org/wiki?curid=1',
                               title, page, templates=templates, redirects=redirects)
        return extractor.clean_text(''.join(page), expand_templates=True)


class WithTemplatesLoadedTests(ArticleExtractionContentTestCase):
    """Every template call in the article resolves to real content,
    except the deliberately-undefined one -- confirmed against the
    real pipeline's actual output, not predicted by hand.
    """

    def test_exact_extracted_lines(self):
        result = self.extract_article(with_templates=True)
        self.assertEqual(result, [
            'Geography is a broad field of study.',
            'Hello, World!',
            'Notable figure.',
            'Born: 1900, Died: 1950',
            'See also the related article.',
            'Using the redirect: the real, current content',
            'real transcluded content',
            'An undefined reference: ',
            'Final paragraph of the article.',
        ])

    def test_redirect_chain_resolves_to_the_targets_real_content(self):
        result = self.extract_article(with_templates=True)
        joined = '\n'.join(result)
        self.assertIn('Using the redirect: the real, current content', joined)

    def test_includeonly_content_appears_noinclude_docs_do_not(self):
        result = self.extract_article(with_templates=True)
        joined = '\n'.join(result)
        self.assertIn('real transcluded content', joined)
        self.assertNotIn('documentation', joined)

    def test_genuinely_undefined_template_still_leaves_surrounding_text(self):
        # "Does not exist" is never defined even in the with-templates
        # case -- distinct from the without-templates case below,
        # where NOTHING is defined. Confirms undefined-template
        # handling specifically, not just "no templates were loaded
        # at all".
        result = self.extract_article(with_templates=True)
        self.assertIn('An undefined reference: ', result)


class WithoutTemplatesLoadedTests(ArticleExtractionContentTestCase):
    """No templates loaded at all -- every {{...}} call resolves to
    empty, but the article's own plain wikitext must still extract
    correctly and completely.
    """

    def test_exact_extracted_lines(self):
        result = self.extract_article(with_templates=False)
        self.assertEqual(result, [
            'Geography is a broad field of study.',
            'Notable figure.',
            'See also the related article.',
            'Using the redirect: ',
            'An undefined reference: ',
            'Final paragraph of the article.',
        ])

    def test_greeting_line_disappears_entirely(self):
        # {{Greeting|World}} was the ONLY content on its own line --
        # once it resolves to empty, that whole line is gone from the
        # output, not merely blank.
        result = self.extract_article(with_templates=False)
        joined = '\n'.join(result)
        self.assertNotIn('Hello', joined)
        self.assertNotIn('World', joined)

    def test_using_the_redirect_line_survives_but_its_contribution_disappears(self):
        # By contrast, "Using the redirect: {{Old name}}" shares its
        # line with real prose -- that prose must survive even though
        # the template itself contributes nothing.
        result = self.extract_article(with_templates=False)
        self.assertIn('Using the redirect: ', result)
        joined = '\n'.join(result)
        self.assertNotIn('current content', joined)

    def test_surrounding_plain_content_entirely_unaffected(self):
        # The parts of the article with no template involvement at
        # all must come through identically regardless of whether
        # templates were loaded.
        result = self.extract_article(with_templates=False)
        joined = '\n'.join(result)
        self.assertIn('Geography is a broad field of study.', joined)
        self.assertIn('Notable figure.', joined)
        self.assertIn('See also the related article.', joined)
        self.assertIn('Final paragraph of the article.', joined)


if __name__ == '__main__':
    unittest.main()
