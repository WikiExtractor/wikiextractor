"""
Tests for -a/--article mode's use of decode_open() instead of bare
open() when reading --templates and the main input file.

Before this fix, this was the one place in WikiExtractor.py that
bypassed decode_open() -- the helper used everywhere else in this
file, which both (a) sets encoding='utf-8' explicitly rather than
relying on the platform's locale-preferred default (relevant on
Windows, where open()'s default encoding is typically NOT UTF-8), and
(b) transparently dispatches to gzip.open()/bz2.open() based on file
extension. Bare open() had neither.

Confirmed directly this was a real, currently-reproducible bug, not
just a theoretical Windows concern: plain open() on an actual, real
.bz2 dump crashes immediately on ANY platform (tested on Linux) with
UnicodeDecodeError, since it tries to decode compressed binary data
as UTF-8 text. Since Wikipedia dumps are normally distributed
compressed, this made --article mode's --templates/input arguments
unusable with the standard file format whenever they were compressed
-- not a Windows-only issue at all, just a more severe, more visible
symptom of the same missing-decode_open() gap that also affects
Windows encoding specifically.

Fixed by replacing both bare open() calls in the --article branch of
main() with decode_open(), matching the convention already used
everywhere else in this file.

Run with:
    python -m unittest tests.test_article_mode -v
or, from the tests/ directory:
    python -m unittest test_article_mode -v
"""

import bz2
import io
import os
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we

# A minimal, realistic single-article dump, deliberately including
# non-ASCII (Arabic-script) content alongside a template call, so a
# test failure here would show either garbled ASCII-only text (an
# encoding problem) or a raised UnicodeDecodeError/similar (a
# decompression problem), not silently pass either way.
_INPUT_XML = '''<mediawiki>
  <siteinfo><sitename>Test</sitename></siteinfo>
  <page>
    <title>Test Article</title>
    <ns>0</ns>
    <id>1</id>
    <revision>
      <id>100</id>
      <text bytes="50">Hello {{greeting}}, and hello سنڌی too.</text>
    </revision>
  </page>
</mediawiki>
'''

_TEMPLATES_XML = '''<mediawiki>
  <page>
    <title>Template:Greeting</title>
    <ns>10</ns>
    <id>2</id>
    <revision>
      <id>101</id>
      <text bytes="10">World</text>
    </revision>
  </page>
</mediawiki>
'''

_EXPECTED_OUTPUT = ('<doc id="1" url="?curid=1" title="Test Article">\n'
                     'Test Article\n\n'
                     'Hello World, and hello سنڌی too.\n\n'
                     '</doc>\n')


class ArticleModeTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = os.path.dirname(os.path.abspath(__file__))
        self.input_plain = os.path.join(self.tmpdir, '_am_input.xml')
        self.templates_plain = os.path.join(self.tmpdir, '_am_templates.xml')
        self.input_bz2 = os.path.join(self.tmpdir, '_am_input.xml.bz2')
        self.templates_bz2 = os.path.join(self.tmpdir, '_am_templates.xml.bz2')

        with open(self.input_plain, 'w', encoding='utf-8') as f:
            f.write(_INPUT_XML)
        with open(self.templates_plain, 'w', encoding='utf-8') as f:
            f.write(_TEMPLATES_XML)
        with bz2.open(self.input_bz2, 'wt', encoding='utf-8') as f:
            f.write(_INPUT_XML)
        with bz2.open(self.templates_bz2, 'wt', encoding='utf-8') as f:
            f.write(_TEMPLATES_XML)

        self._orig_argv = sys.argv
        self._orig_stdout = sys.stdout

    def tearDown(self):
        sys.argv = self._orig_argv
        sys.stdout = self._orig_stdout
        for path in (self.input_plain, self.templates_plain,
                     self.input_bz2, self.templates_bz2):
            if os.path.exists(path):
                os.remove(path)

    def run_article_mode(self, input_path, templates_path):
        sys.argv = ['WikiExtractor.py', '-a', '--templates', templates_path, input_path]
        buf = io.StringIO()
        sys.stdout = buf
        try:
            we.main()
        finally:
            sys.stdout = self._orig_stdout
        return buf.getvalue()


class PlainXmlTests(ArticleModeTestCase):

    def test_plain_xml_extracts_correctly(self):
        result = self.run_article_mode(self.input_plain, self.templates_plain)
        self.assertEqual(result, _EXPECTED_OUTPUT)


class CompressedInputTests(ArticleModeTestCase):
    """The clearest, most direct regression test: before the fix, this
    failed with UnicodeDecodeError on every platform, not just Windows
    -- bare open() tried to read compressed binary data as UTF-8 text.
    """

    def test_bz2_input_and_templates_extract_correctly(self):
        result = self.run_article_mode(self.input_bz2, self.templates_bz2)
        self.assertEqual(result, _EXPECTED_OUTPUT)

    def test_bz2_input_with_plain_templates(self):
        # Mixed case: only the main input is compressed.
        result = self.run_article_mode(self.input_bz2, self.templates_plain)
        self.assertEqual(result, _EXPECTED_OUTPUT)

    def test_plain_input_with_bz2_templates(self):
        # Mixed case: only --templates is compressed.
        result = self.run_article_mode(self.input_plain, self.templates_bz2)
        self.assertEqual(result, _EXPECTED_OUTPUT)


class ExplicitEncodingTests(ArticleModeTestCase):
    """Confirms the actual mechanism of the fix, not just its outward
    behavior: --article mode must never open --templates or the main
    input file without an explicit encoding, since relying on the
    platform's locale-preferred default is exactly what made this
    unreliable on Windows in the first place.
    """

    def test_article_mode_never_opens_files_without_explicit_encoding(self):
        import builtins
        real_open = builtins.open
        offending_calls = []

        def watching_open(file, mode='r', *args, **kwargs):
            # Only care about text-mode opens of our own test files --
            # not every incidental open() elsewhere in the interpreter.
            is_ours = isinstance(file, str) and (
                file == self.input_plain or file == self.templates_plain)
            if is_ours and 'b' not in mode and 'encoding' not in kwargs:
                offending_calls.append((file, mode))
            return real_open(file, mode, *args, **kwargs)

        builtins.open = watching_open
        try:
            self.run_article_mode(self.input_plain, self.templates_plain)
        finally:
            builtins.open = real_open

        self.assertEqual(offending_calls, [],
                          f"opened without explicit encoding: {offending_calls}")


if __name__ == '__main__':
    unittest.main()
