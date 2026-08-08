"""
Regression test for GitHub issue #254:
"Missing the articles that include a colon in the title"
https://github.com/WikiExtractor/wikiextractor/issues/254

The reporter's example: an ordinary, main-namespace (ns=0) article
titled "Super Mario Advance 4: Super Mario Bros 3" was silently
dropped by extraction.

Root cause, as it existed before this fix, in collect_pages():

    colon = title.find(':')
    if ((colon < 0 or (title[:colon] in acceptedNamespaces)) and
        (id != last_id and not redirect and not title.startswith(templateNamespace))):
        yield (id, revid, title, page)

The namespace check was done purely by looking for a ':' in the title
string and testing the text before it against acceptedNamespaces
(default ['w', 'wiktionary', 'wikt'] -- a list of interwiki project
prefixes, used elsewhere for deciding whether an in-body link like
[[wiktionary:foo]] is kept or stripped -- not a list of MediaWiki
namespaces at all). collect_pages() never looked at the page's actual
<ns>...</ns> element -- it wasn't parsed anywhere in the function. So
any ns=0 article whose title merely *contained* a colon -- a common
pattern for media titles ("Super Mario Advance 4: Super Mario Bros 3",
"3001: The Final Odyssey", "Kill Bill: Volume 1") -- was misread as
belonging to a foreign namespace ("Super Mario Advance 4", "3001",
"Kill Bill") and dropped, even though the dump's own <ns>0</ns> said
plainly that it was an ordinary article.

Fixed by having collect_pages() parse <ns> directly and require it to
be exactly '0' (Main/Article) -- the one namespace WikiExtractor
actually wants pages from. acceptedNamespaces and -ns/--namespaces are
untouched: that flag still controls only which interwiki-project
prefixes survive link-stripping in extract.py, exactly as before this
fix (see makeInternalLink()) -- it has no bearing on which pages
collect_pages() yields.

These tests assert the correct behavior (an ns=0 article is extracted
regardless of what its title contains, and non-ns=0 pages are still
excluded) and should now pass. If they ever start failing again, issue
#254 (or an equivalent regression) has resurfaced.

Run with:
    python -m unittest tests.test_colon_in_title -v
or, from the tests/ directory:
    python -m unittest test_colon_in_title -v
"""

import os
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we


class ColonInTitleTests(unittest.TestCase):

    def setUp(self):
        # Same reasoning as test_collect_pages_redirect.py: collect_pages()
        # relies on this module-level global, normally populated from the
        # dump's own <siteinfo> section -- set it explicitly so the test
        # doesn't depend on whatever a prior test left behind.
        self._orig_namespace = we.templateNamespace
        we.templateNamespace = 'Template'
        self.tmpdir = os.path.dirname(os.path.abspath(__file__))
        self._paths = []

    def tearDown(self):
        we.templateNamespace = self._orig_namespace
        for path in self._paths:
            if os.path.exists(path):
                os.remove(path)

    def collect(self, xml, name='_colon_test.xml'):
        path = os.path.join(self.tmpdir, name)
        self._paths.append(path)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(xml)
        with we.decode_open(path) as f:
            return list(we.collect_pages(f))

    def test_issue_254_exact_example(self):
        # The exact title from the bug report, on a plain ns=0 page with
        # no redirect and no other complication -- as minimal a
        # reproduction as possible.
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>Super Mario Advance 4: Super Mario Bros 3</title>
    <ns>0</ns>
    <id>100</id>
    <revision>
      <id>1000</id>
      <text bytes="40">A video game article with a colon.</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        titles = [r[2] for r in results]
        self.assertIn(
            'Super Mario Advance 4: Super Mario Bros 3', titles,
            "issue #254 reproduced: an ns=0 article whose title contains "
            "a colon was dropped by collect_pages()")

    def test_ns0_article_with_colon_alongside_ordinary_article(self):
        # Mirrors how the bug actually surfaces in practice: a normal
        # dump where most titles have no colon and extract fine, but
        # colon-bearing ns=0 titles quietly vanish from the output
        # while everything else looks normal.
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>3001: The Final Odyssey</title>
    <ns>0</ns>
    <id>101</id>
    <revision>
      <id>1001</id>
      <text bytes="20">A novel by Arthur C. Clarke.</text>
    </revision>
  </page>
  <page>
    <title>Ordinary Article</title>
    <ns>0</ns>
    <id>102</id>
    <revision>
      <id>1002</id>
      <text bytes="20">Just a normal page.</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        titles = [r[2] for r in results]
        self.assertIn('Ordinary Article', titles)
        self.assertIn(
            '3001: The Final Odyssey', titles,
            "issue #254 reproduced: an ns=0 article whose title contains "
            "a colon was dropped by collect_pages(), even though a "
            "sibling article with no colon in its title extracted fine")

    def test_ns0_article_whose_title_looks_like_a_namespace_prefix_still_included(self):
        # Sanity check: a plain ns=0 article whose title happens to
        # start with text that looks like a namespace prefix (but isn't
        # one -- the page's own <ns> says 0) must still be included.
        # acceptedNamespaces is deliberately NOT touched here: it has no
        # bearing on collect_pages()'s page-inclusion decision, only on
        # link-stripping in extract.py.
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>wiktionary:some entry</title>
    <ns>0</ns>
    <id>103</id>
    <revision>
      <id>1003</id>
      <text bytes="20">An ns=0 article that merely looks namespaced.</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        titles = [r[2] for r in results]
        self.assertIn('wiktionary:some entry', titles)

    def test_non_article_namespace_title_with_colon_still_excluded(self):
        # Sanity check for the other direction: a page that's genuinely
        # in a different namespace (e.g. Talk) should still be excluded.
        # A correct fix for #254 needs to tell this case apart from the
        # "3001: The Final Odyssey" case above -- doing so requires
        # actually consulting <ns>, since both cases look identical from
        # the title text alone.
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>Talk:Some Article</title>
    <ns>1</ns>
    <id>104</id>
    <revision>
      <id>1004</id>
      <text bytes="20">Discussion page content.</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        titles = [r[2] for r in results]
        self.assertNotIn('Talk:Some Article', titles)

    def test_ns_flag_still_controls_link_stripping_only(self):
        # -ns/--namespaces (built into extractor_kwargs['acceptedNamespaces']
        # in main(), then passed into each real Extractor) only affects
        # whether an in-body interwiki link is kept or stripped in
        # extract.py. It has no effect on which pages collect_pages()
        # yields -- confirm a non-'0' namespace stays excluded regardless.
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>Category:Films</title>
    <ns>14</ns>
    <id>105</id>
    <revision>
      <id>1005</id>
      <text bytes="20">Category description.</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        titles = [r[2] for r in results]
        self.assertNotIn(
            'Category:Films', titles,
            "-ns/--namespaces should not affect page selection, only "
            "link-stripping in extract.py")


if __name__ == '__main__':
    unittest.main()
