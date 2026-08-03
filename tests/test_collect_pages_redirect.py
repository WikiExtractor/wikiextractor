"""
Tests for collect_pages()'s page-filtering condition, which had a
Python operator-precedence bug: 'and' binds tighter than 'or', so

    if (colon < 0 or (title[:colon] in acceptedNamespaces) and id != last_id and
            not redirect and not title.startswith(templateNamespace)):

actually parsed as

    if (colon < 0) or ((title[:colon] in acceptedNamespaces) and id != last_id
            and not redirect and not title.startswith(templateNamespace)):

meaning a title with no namespace colon at all (colon < 0) made the
whole condition True unconditionally, short-circuiting past the
id != last_id, not redirect, and not-a-template checks entirely.
Since ordinary article titles -- including ordinary, main-namespace
redirect pages, which are the vast majority of redirects on any real
wiki -- typically have no colon in their own title, this meant
redirect pages were incorrectly yielded for extraction despite the
not-redirect check existing right there in the code. Confirmed
directly, reproducibly, before this fix: a synthetic redirect page
("USA" -> "United States", no colon in its own title) was yielded by
collect_pages() alongside a genuine article, when it should have been
excluded.

Fixed by parenthesizing the two logical halves explicitly:

    if (colon < 0 or (title[:colon] in acceptedNamespaces)) and (id != last_id and
            not redirect and not title.startswith(templateNamespace)):

so the namespace check and the redirect/duplicate/template checks are
both always evaluated, exactly as originally intended.

Run with:
    python -m unittest tests.test_collect_pages_redirect -v
or, from the tests/ directory:
    python -m unittest test_collect_pages_redirect -v
"""

import os
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we


class CollectPagesRedirectFilterTests(unittest.TestCase):

    def setUp(self):
        # collect_pages() relies on these module-level globals, normally
        # populated from the dump's own <siteinfo> section -- set them
        # explicitly here, matching what a real dump would provide,
        # rather than relying on whatever a prior test left behind.
        self._orig_namespace = we.templateNamespace
        self._orig_accepted = we.acceptedNamespaces
        we.templateNamespace = 'Template'
        we.acceptedNamespaces = set(['w'])
        self.tmpdir = os.path.dirname(os.path.abspath(__file__))
        self._paths = []

    def tearDown(self):
        we.templateNamespace = self._orig_namespace
        we.acceptedNamespaces = self._orig_accepted
        for path in self._paths:
            if os.path.exists(path):
                os.remove(path)

    def collect(self, xml, name='_cp_test.xml'):
        path = os.path.join(self.tmpdir, name)
        self._paths.append(path)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(xml)
        with we.decode_open(path) as f:
            return list(we.collect_pages(f))

    def test_redirect_page_with_no_colon_in_title_is_excluded(self):
        # The exact, real-world shape of the bug: an ordinary,
        # main-namespace redirect, whose own title has no colon at all.
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>USA</title>
    <ns>0</ns>
    <id>5</id>
    <redirect title="United States" />
    <revision>
      <id>50</id>
      <text bytes="30">#REDIRECT [[United States]]</text>
    </revision>
  </page>
  <page>
    <title>Real Article</title>
    <ns>0</ns>
    <id>6</id>
    <revision>
      <id>51</id>
      <text bytes="30">Some real content here.</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        titles = [r[2] for r in results]
        self.assertNotIn('USA', titles)
        self.assertIn('Real Article', titles)
        self.assertEqual(len(results), 1)

    def test_redirect_page_with_colon_in_title_is_also_excluded(self):
        # Sanity check: a redirect whose title DOES have a colon (e.g.
        # under a non-default accepted namespace) must also still be
        # excluded -- the fix must not just be "fixing" the no-colon
        # case by accident while leaving the with-colon case broken
        # some other way.
        we.acceptedNamespaces = set(['w', 'wiktionary'])
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>wiktionary:USA</title>
    <ns>0</ns>
    <id>7</id>
    <redirect title="wiktionary:United States" />
    <revision>
      <id>52</id>
      <text bytes="30">#REDIRECT [[wiktionary:United States]]</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        self.assertEqual(len(results), 0)

    def test_ordinary_non_redirect_article_still_included(self):
        # Must not overcorrect: a completely ordinary article (no
        # colon, no redirect tag) must still be yielded normally.
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>Ordinary Article</title>
    <ns>0</ns>
    <id>8</id>
    <revision>
      <id>53</id>
      <text bytes="20">Just a normal page.</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2], 'Ordinary Article')

    def test_template_namespace_page_still_excluded(self):
        # Sanity check for the other half of the AND: a page under the
        # template namespace (title has a colon, prefix matches
        # templateNamespace) must still be excluded, same as before
        # this fix -- the parenthesization must not have disturbed this.
        xml = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>Template:Infobox</title>
    <ns>10</ns>
    <id>9</id>
    <revision>
      <id>54</id>
      <text bytes="20">Some template body.</text>
    </revision>
  </page>
</mediawiki>
'''
        results = self.collect(xml)
        self.assertEqual(len(results), 0)


if __name__ == '__main__':
    unittest.main()
