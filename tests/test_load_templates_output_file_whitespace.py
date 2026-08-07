"""
Tests for a real bug in load_templates()'s output_file writer: it
unconditionally wrote three literal spaces immediately before every
</text> tag, regardless of what the actual template content ended
with -- silently appending spurious trailing whitespace to EVERY
template ever written through this path.

Traced from a real, reported symptom: a template's expansion showed
an extra space that wasn't present when reading the same template
straight from a full dump, but WAS present when reading from a
templates-only file previously built via `-o`/output_file. Confirmed
directly (not assumed) by writing a template with deliberately zero
trailing whitespace through load_templates(..., output_file=...) and
inspecting the raw output bytes -- the source had none, the output
file had three spaces before </text>, added purely by the write
itself.

Root cause: '   <title>...</title>\\n' and '   <ns>...</ns>\\n' use a
3-space indent for readability, since each is a fresh, self-contained
line. '   </text>\\n' copied that same 3-space-prefix convention, but
<text> is opened WITHOUT a trailing newline (`output.write('<text>')`)
and the actual page content is then written raw, immediately
afterward -- so </text>'s "indentation" spaces land directly appended
onto the end of the real content instead of starting a new line,
silently corrupting the template's own text on every single template
written this way.

This matters well beyond a single template: any templates-only file
ever built via load_templates(..., output_file=...) -- as opposed to
extract_templates_by_id.py, which writes raw page blocks verbatim and
does not have this bug -- carries this exact three-space corruption on
every template it contains, and any tool reading that file back
inherits it, even though it's faithfully doing a verbatim copy of an
already-corrupted source.

Run with:
    python -m unittest tests.test_load_templates_output_file_whitespace -v
or, from the tests/ directory:
    python -m unittest test_load_templates_output_file_whitespace -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we
import wikiextractor.extract as ex

_DUMP_TEMPLATE = '''<mediawiki>
<siteinfo>
<namespaces>
<namespace key="0" case="first-letter" />
<namespace key="10" case="first-letter">Template</namespace>
</namespaces>
</siteinfo>
<page>
<title>Template:{title_suffix}</title>
<ns>10</ns>
<id>1</id>
<revision>
<id>1</id>
<text>{text}</text>
</revision>
</page>
</mediawiki>
'''


class LoadTemplatesOutputFileWhitespaceTests(unittest.TestCase):

    def setUp(self):
        we.templateNamespace = ''
        ex.Extractor.templatePrefix = ''
        self._tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup_tmpdir)

    def _cleanup_tmpdir(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def round_trip(self, title_suffix, text):
        """Writes a synthetic dump containing one template, runs it
        through load_templates(..., output_file=...), and returns the
        raw bytes of the resulting output file."""
        dump_path = os.path.join(self._tmpdir, 'dump.xml')
        output_path = os.path.join(self._tmpdir, 'output.xml')
        with open(dump_path, 'w', encoding='utf-8') as f:
            f.write(_DUMP_TEMPLATE.format(title_suffix=title_suffix, text=text))

        with we.decode_open(dump_path) as f:
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

        with we.decode_open(dump_path) as f:
            we.load_templates(f, output_file=output_path)

        with open(output_path, encoding='utf-8') as f:
            return f.read()

    def test_zero_trailing_whitespace_source_stays_clean(self):
        # The core, directly-confirmed case: content with NO trailing
        # whitespace at all must come out with none either.
        output = self.round_trip('Clean', 'No trailing whitespace at all')
        self.assertIn('<text>No trailing whitespace at all</text>', output)
        self.assertNotIn('at all   </text>', output)
        self.assertNotIn('at all </text>', output)

    def test_genuine_trailing_newline_is_preserved_not_stripped(self):
        # The fix must not overcorrect: real trailing content that
        # was actually part of the source should survive intact.
        output = self.round_trip('HasNewline',
                                  'Real content ending in a genuine trailing newline\n')
        self.assertIn(
            '<text>Real content ending in a genuine trailing newline\n</text>',
            output)

    def test_full_round_trip_produces_no_spurious_expansion_whitespace(self):
        # End-to-end: a template with clean content, written via
        # output_file, then LOADED BACK via load_templates() a second
        # time (simulating "read from a previously-saved templates
        # file"), must expand identically to loading it fresh --
        # confirming the fix actually closes the loop this bug was
        # found through, not just the raw byte-level symptom.
        output = self.round_trip('Wrapper', 'wrapped[{{{1}}}]')

        roundtrip_path = os.path.join(self._tmpdir, 'roundtrip.xml')
        with open(roundtrip_path, 'w', encoding='utf-8') as f:
            f.write(output)

        we.templateNamespace = ''
        ex.Extractor.templatePrefix = ''
        with we.decode_open(roundtrip_path) as f:
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
        roundtrip_templates = {}
        with we.decode_open(roundtrip_path) as f:
            we.load_templates(f, templates=roundtrip_templates)

        wikitext = '{{Wrapper|value}}'
        extractor = ex.Extractor(1, '1', 'https://x', 'Test', [wikitext],
                                  templates=roundtrip_templates)
        result = extractor.clean_text(wikitext, expand_templates=True)
        self.assertEqual(result, ['wrapped[value]'])


if __name__ == '__main__':
    unittest.main()
