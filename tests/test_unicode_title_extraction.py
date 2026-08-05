"""
Regression test for GitHub issue #92:
"Error: WikiExtractor.py:2703"
https://github.com/WikiExtractor/wikiextractor/issues/92

The reporter downloaded a Special:Export XML for the article "24th
Waffen Mountain Division of the SS Karstjäger" (note the German
umlaut, "ä") and got an error at extraction time. A follow-up comment
narrowed it further: "the extractor has problems with filenames that
contains special characters like ū or ć" -- i.e. non-ASCII characters
in the page title itself.

No longer reproduces. Confirmed directly against the real article
(downloaded fresh from Special:Export, uploaded as
Wikipedia-20260805002958.xml) with both invocation patterns mentioned
in the issue thread:

    python3 -m wikiextractor.WikiExtractor <file>
    python3 -m wikiextractor.WikiExtractor <file> -o -

Both complete successfully and produce a <doc> for this article with
its title intact. This test reconstructs the same shape (the exact
real title, which is what the original bug actually turned on) against
a small synthetic dump, run through the real CLI end to end -- the
same style as test_dump_completeness.py -- so a regression in title
encoding/handling would be caught here rather than requiring someone
to notice a thesis-blocking error on a specific Wikipedia article again.

Run with:
    python -m unittest tests.test_unicode_title_extraction -v
or, from the tests/ directory:
    python -m unittest test_unicode_title_extraction -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

# The real wikiextractor package lives one level up from tests/ --
# needed on PYTHONPATH once cwd is pointed at a tmpdir instead, so
# "python -m wikiextractor.WikiExtractor" can still find it there.
PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)

# The exact real title from issue #92 -- the umlaut is what the bug
# actually turned on, not just an arbitrary stand-in title.
REAL_TITLE = "24th Waffen Mountain Division of the SS Karstjäger"

# A couple of the other characters a follow-up comment on the same
# issue specifically called out ("ū or ć") as also triggering it.
OTHER_TITLES = [
    "Ūdege language",
    "Kazimierz Wielki Ćwik",
]

DUMP_TEMPLATE = '''<mediawiki xml:lang="en">
  <siteinfo>
    <sitename>Test</sitename>
    <namespaces>
      <namespace key="0" case="first-letter" />
      <namespace key="10" case="first-letter">Template</namespace>
    </namespaces>
  </siteinfo>
{pages}
</mediawiki>
'''

PAGE_TEMPLATE = '''  <page>
    <title>{title}</title>
    <ns>0</ns>
    <id>{id}</id>
    <revision>
      <id>{revid}</id>
      <text bytes="40" xml:space="preserve">A short placeholder article body.</text>
    </revision>
  </page>
'''


def build_synthetic_dump(path, titles):
    pages = ''.join(
        PAGE_TEMPLATE.format(title=title, id=100 + i, revid=1000 + i)
        for i, title in enumerate(titles)
    )
    path.write_text(DUMP_TEMPLATE.format(pages=pages), encoding='utf-8')


class UnicodeTitleExtractionTests(unittest.TestCase):

    def run_extraction(self, titles, extra_args):
        # mkdtemp() (not TemporaryDirectory()'s context-manager form) --
        # the directory needs to stay alive after this method returns,
        # for the caller to read the default-output-mode files out of;
        # addCleanup() removes it once the test itself is done instead.
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)

        dump_path = Path(tmpdir) / "test_dump.xml"
        build_synthetic_dump(dump_path, titles)

        # cwd=tmpdir (rather than the package root) so that default
        # output mode -- no --output flag, exactly the first
        # invocation pattern from the issue -- writes its relative
        # "text/" directory somewhere this test controls and cleans
        # up, instead of into the real package checkout. PYTHONPATH
        # is needed since cwd no longer implicitly provides it.
        env = {**os.environ, 'PYTHONPATH': PACKAGE_ROOT}
        result = subprocess.run(
            [sys.executable, "-m", "wikiextractor.WikiExtractor",
             "--no-templates", *extra_args, str(dump_path)],
            cwd=tmpdir, capture_output=True, text=True, timeout=60, env=env,
        )
        return result, Path(tmpdir)

    def assertTitlesExtracted(self, titles, output_text):
        for title in titles:
            self.assertIn(f'title="{title}"', output_text,
                           f"expected a <doc> for {title!r} in the output")

    def test_default_output_mode(self):
        # python3 -m wikiextractor.WikiExtractor <file>
        # Default mode writes to a directory (text/AA/wiki_00 etc.)
        # rather than stdout.
        titles = [REAL_TITLE] + OTHER_TITLES
        result, tmpdir = self.run_extraction(titles, extra_args=[])
        self.assertEqual(result.returncode, 0,
                          f"WikiExtractor.py failed: {result.stderr}")

        combined = ''
        for out_file in tmpdir.rglob("wiki_*"):
            combined += out_file.read_text(encoding='utf-8')
        self.assertTitlesExtracted(titles, combined)

    def test_stdout_output_mode(self):
        # python3 -m wikiextractor.WikiExtractor <file> -o -
        titles = [REAL_TITLE] + OTHER_TITLES
        result, _ = self.run_extraction(titles, extra_args=["-o", "-"])
        self.assertEqual(result.returncode, 0,
                          f"WikiExtractor.py failed: {result.stderr}")
        self.assertTitlesExtracted(titles, result.stdout)


if __name__ == '__main__':
    unittest.main()
