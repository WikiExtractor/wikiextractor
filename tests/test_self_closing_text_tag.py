"""
Tests for self-closing <text/> handling in WikiExtractor.py's
collect_pages() and load_templates().

Real MediaWiki dumps write a revision with zero bytes of content as a
self-closing tag with no separate closing tag at all:
    <text bytes="0" sha1="..." />
rather than the normal <text ...>content</text> pair. Confirmed
directly against a real Sindhi Wikipedia dump: neither function
recognized this form at all -- both only ever handled "no matching
</text> arrives on this line" as the ordinary, still-open case
(inText = True), with no check for whether the tag that was just
opened is actually self-closing. Since a self-closing tag's matching
"</text>" never arrives at all, inText stayed stuck True forever,
silently merging every SUBSEQUENT line -- including the next page's
own <ns>/<id>/<revision>/<contributor> metadata and its real body text
-- into what was supposed to be the empty page's own (nonexistent)
text.

Concretely, this meant: a page immediately following an empty one got
the EMPTY page's id, while showing the FOLLOWING page's own title, and
its extracted text contained the following page's own XML metadata as
literal, HTML-escaped junk (confirmed as the exact, real-world source
of a "leaked metadata" pattern that surfaced independently while
investigating an unrelated redirect-handling change).

Fixed by checking whether the character immediately before the tag's
closing '>' is '/' -- the same distinguishing signal already used
elsewhere in this project for the identical self-closing-vs-open
question (see extract.py's discardElements fix). When it is, the tag
contributes nothing to the page's text and inText is never set at all,
rather than being set and left with no way to ever turn back off.

Run with:
    python -m unittest tests.test_self_closing_text_tag -v
or, from the tests/ directory:
    python -m unittest test_self_closing_text_tag -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we


class SelfClosingTextTagTestCase(unittest.TestCase):

    def collect(self, xml_text):
        lines = xml_text.splitlines(keepends=True)
        return list(we.collect_pages(lines))


class EmptyPageDoesNotAbsorbFollowingPageTests(SelfClosingTextTagTestCase):

    def test_page_after_empty_page_keeps_its_own_id_and_title(self):
        xml = '''<mediawiki>
  <page>
    <title>Empty Page</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <id>100</id>
      <text bytes="0" sha1="abc" />
    </revision>
  </page>
  <page>
    <title>Real Article</title>
    <ns>0</ns>
    <id>2</id>
    <revision>
      <id>101</id>
      <text>Real article content.</text>
    </revision>
  </page>
</mediawiki>'''
        pages = self.collect(xml)
        titles = {p[2]: p for p in pages}
        self.assertIn('Real Article', titles)
        page_id, revid, title, page = titles['Real Article']
        self.assertEqual(page_id, '2')
        self.assertEqual(''.join(page), 'Real article content.')

    def test_following_page_text_contains_no_leaked_metadata(self):
        xml = '''<mediawiki>
  <page>
    <title>Empty Page</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <id>100</id>
      <text bytes="0" sha1="abc" />
    </revision>
  </page>
  <page>
    <title>Real Article</title>
    <ns>0</ns>
    <id>2</id>
    <revision>
      <id>101</id>
      <contributor>
        <username>SomeEditor</username>
      </contributor>
      <text>Real article content.</text>
    </revision>
  </page>
</mediawiki>'''
        pages = self.collect(xml)
        titles = {p[2]: p for p in pages}
        page_id, revid, title, page = titles['Real Article']
        text = ''.join(page)
        self.assertNotIn('<contributor>', text)
        self.assertNotIn('SomeEditor', text)
        self.assertEqual(text, 'Real article content.')

    def test_multiple_consecutive_empty_pages(self):
        xml = '''<mediawiki>
  <page>
    <title>Empty One</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <id>100</id>
      <text bytes="0" sha1="a" />
    </revision>
  </page>
  <page>
    <title>Empty Two</title>
    <ns>0</ns>
    <id>2</id>
    <revision>
      <id>101</id>
      <text bytes="0" sha1="b" />
    </revision>
  </page>
  <page>
    <title>Real Article After Two Empties</title>
    <ns>0</ns>
    <id>3</id>
    <revision>
      <id>102</id>
      <text>Content after two empty pages.</text>
    </revision>
  </page>
</mediawiki>'''
        pages = self.collect(xml)
        titles = {p[2]: p for p in pages}
        page_id, revid, title, page = titles['Real Article After Two Empties']
        self.assertEqual(page_id, '3')
        self.assertEqual(''.join(page), 'Content after two empty pages.')


class OrdinaryFormsStillWorkTests(SelfClosingTextTagTestCase):
    """Sanity check: the ordinary, already-working forms (multi-line
    text, and open+close on the same line) are unaffected.
    """

    def test_multiline_text(self):
        xml = '''<mediawiki>
  <page>
    <title>Multiline Article</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <id>100</id>
      <text>Line one.
Line two.
Line three.</text>
    </revision>
  </page>
</mediawiki>'''
        pages = self.collect(xml)
        self.assertEqual(len(pages), 1)
        page_id, revid, title, page = pages[0]
        self.assertEqual(''.join(page), 'Line one.\nLine two.\nLine three.')

    def test_same_line_open_close(self):
        xml = '''<mediawiki>
  <page>
    <title>Single Line Article</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <id>100</id>
      <text>All content on one line.</text>
    </revision>
  </page>
</mediawiki>'''
        pages = self.collect(xml)
        self.assertEqual(len(pages), 1)
        page_id, revid, title, page = pages[0]
        self.assertEqual(''.join(page), 'All content on one line.')


class TemplateContaminationTests(unittest.TestCase):
    """load_templates() has an identical copy of the same bug --
    worse in consequence than the article case, since a template that
    ends up contaminated this way gets re-expanded into every article
    that calls it, not just the one page immediately following the
    empty one in the dump.
    """

    def setUp(self):
        self.templates = {}
        self.redirects = {}
        import wikiextractor.extract as ex
        import wikiextractor.WikiExtractor as we
        ex.TemplateArg._parse_template.cache_clear()
        ex.Extractor._parse_template.cache_clear()
        # load() below relies on load_templates()'s self-bootstrap
        # namespace detection, which only ever fires once per process
        # (while we.templateNamespace is still falsy) -- reset here so
        # this doesn't silently pick up a stale namespace left by some
        # other, unrelated test file's own dump data.
        we.templateNamespace = ''

    def load(self, xml_text):
        import io
        import wikiextractor.WikiExtractor as we
        we.load_templates(io.StringIO(xml_text), templates=self.templates, redirects=self.redirects)

    def test_template_after_blanked_template_is_not_contaminated(self):
        xml = '''<mediawiki>
  <page>
    <title>Template:Blanked</title>
    <ns>10</ns>
    <id>500</id>
    <revision>
      <id>900</id>
      <text bytes="0" sha1="abc" />
    </revision>
  </page>
  <page>
    <title>Template:Infobox person</title>
    <ns>10</ns>
    <id>501</id>
    <revision>
      <id>901</id>
      <contributor>
        <username>SomeEditor</username>
      </contributor>
      <text>The real infobox content for {{{name}}}.</text>
    </revision>
  </page>
</mediawiki>'''
        self.load(xml)
        self.assertNotIn('Template:Blanked', self.templates)
        self.assertIn('Template:Infobox person', self.templates)
        content = self.templates['Template:Infobox person']
        self.assertNotIn('<contributor>', content)
        self.assertNotIn('SomeEditor', content)
        self.assertEqual(content, 'The real infobox content for {{{name}}}.')

    def test_a_genuinely_empty_template_does_not_crash_define_template(self):
        # Exposed by the fix above: once collect_pages()/
        # load_templates() correctly recognize a self-closing <text/>
        # instead of silently merging in the next page's content,
        # define_template() can now genuinely be called with an empty
        # page list -- which it previously never needed to handle,
        # since the merge bug always made page non-empty by the time
        # it got there.
        import wikiextractor.extract as ex
        try:
            ex.define_template('Template:Blanked', [], self.templates, self.redirects)
        except IndexError:
            self.fail("define_template() crashed on a genuinely empty page list")
        self.assertNotIn('Template:Blanked', self.templates)
        self.assertNotIn('Template:Blanked', self.redirects)


if __name__ == '__main__':
    unittest.main()
