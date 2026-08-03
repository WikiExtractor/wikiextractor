"""
End-to-end regression test for a real, confirmed bug: the LAST page in
a dump was silently missing from the extracted output.

Root cause, confirmed directly: reduce_process() writes completed
pages to the output file but never explicitly flushed or closed it
before returning. Since reduce_process runs as its own forked child
process, its writes were buffered in that process's own memory --
separate from the parent's copy of the same file object (each process
gets its own copy after fork(), sharing only the underlying OS file
descriptor). The parent later calls output.close() after reduce.join(),
but that's the PARENT's copy, which has nothing of its own to flush.
Without an explicit flush/close inside reduce_process itself before it
exits, its last buffered write is simply lost, with no error at all --
reliably dropping exactly the last page of every dump tested (30, 100,
and others), regardless of size or --processes count.

collect_pages() itself was confirmed NOT to be the cause (it correctly
yields every page, including the last one) -- the loss happened
strictly in how the output got (or didn't get) flushed to disk.

This test suite runs the actual WikiExtractor.py CLI end to end
against small synthetic dumps and confirms every expected page shows
up in the output -- exactly the kind of "N pages in, N pages out"
check that would have caught this immediately, and should now guard
against it recurring.

Run with:
    python -m unittest tests.test_dump_completeness -v
or, from the tests/ directory:
    python -m unittest test_dump_completeness -v
"""

import bz2
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, '..')  # allow running directly from tests/ without installing


def build_synthetic_dump(path, n_pages):
    """Writes a minimal, valid MediaWiki XML dump with n_pages plain
    (no templates, no links) articles, numbered 0 to n_pages - 1."""
    header = (
        '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.10/" '
        'version="0.10">\n'
        '  <siteinfo>\n'
        '    <sitename>Test</sitename>\n'
        '    <namespaces>\n'
        '      <namespace key="0" case="first-letter" />\n'
        '    </namespaces>\n'
        '  </siteinfo>\n'
    )
    footer = '</mediawiki>\n'
    with bz2.open(path, 'wt', encoding='utf-8') as f:
        f.write(header)
        for i in range(n_pages):
            f.write(
                f'  <page>\n'
                f'    <title>Article {i}</title>\n'
                f'    <ns>0</ns>\n'
                f'    <id>{i}</id>\n'
                f'    <revision>\n'
                f'      <id>{1000 + i}</id>\n'
                f'      <text>Plain content for article {i}.</text>\n'
                f'    </revision>\n'
                f'  </page>\n'
            )
        f.write(footer)


class DumpCompletenessTestCase(unittest.TestCase):
    """Shared helper: runs the real WikiExtractor.py CLI end to end
    (as a subprocess, exercising the actual dump parsing, extraction,
    and output-writing pipeline together, not just one function in
    isolation) against a synthetic dump, and returns the set of
    article titles found in the output.
    """

    def run_extraction(self, n_pages, processes=1):
        with tempfile.TemporaryDirectory() as tmpdir:
            dump_path = Path(tmpdir) / "test_dump.xml.bz2"
            output_path = Path(tmpdir) / "output"
            build_synthetic_dump(dump_path, n_pages)

            result = subprocess.run(
                [sys.executable, "-m", "wikiextractor.WikiExtractor",
                 "--no-templates", "--processes", str(processes),
                 "--output", str(output_path), str(dump_path)],
                cwd="..", capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0,
                              f"WikiExtractor.py failed: {result.stderr}")

            titles_found = set()
            for out_file in output_path.rglob("wiki_*"):
                with open(out_file, encoding='utf-8') as f:
                    for line in f:
                        if line.startswith('<doc '):
                            start = line.index('title="') + len('title="')
                            end = line.index('"', start)
                            titles_found.add(line[start:end])
            return titles_found


class AllPagesPresentTests(DumpCompletenessTestCase):
    """The core regression check: every page in the dump, INCLUDING
    the last one, must appear in the output.
    """

    def test_10_page_dump_all_pages_present(self):
        n = 10
        expected = {f"Article {i}" for i in range(n)}
        found = self.run_extraction(n)
        missing = expected - found
        self.assertEqual(
            missing, set(),
            f"{len(missing)} page(s) missing from output: {sorted(missing)}"
        )

    def test_20_page_dump_all_pages_present(self):
        n = 20
        expected = {f"Article {i}" for i in range(n)}
        found = self.run_extraction(n)
        missing = expected - found
        self.assertEqual(
            missing, set(),
            f"{len(missing)} page(s) missing from output: {sorted(missing)}"
        )

    def test_30_page_dump_all_pages_present(self):
        n = 30
        expected = {f"Article {i}" for i in range(n)}
        found = self.run_extraction(n)
        missing = expected - found
        self.assertEqual(
            missing, set(),
            f"{len(missing)} page(s) missing from output: {sorted(missing)}"
        )

    def test_last_page_specifically_present(self):
        # Targeted regression check for the exact historical failure
        # mode: the last page in the dump, specifically, must not be
        # dropped (this was the one consistently-affected position
        # across every size previously tested).
        n = 15
        found = self.run_extraction(n)
        self.assertIn(f"Article {n - 1}", found,
                      "the last page must be present -- this exact "
                      "position was the one silently dropped before "
                      "reduce_process was fixed to flush/close its own "
                      "output before exiting")

    def test_all_pages_present_regardless_of_process_count(self):
        # Confirmed independent of multiprocessing: the original bug
        # reproduced even with --processes 1 (fully sequential), so
        # this checks correctness is not specific to any one
        # concurrency level either.
        n = 12
        expected = {f"Article {i}" for i in range(n)}
        for processes in (1, 3):
            with self.subTest(processes=processes):
                found = self.run_extraction(n, processes=processes)
                missing = expected - found
                self.assertEqual(
                    missing, set(),
                    f"with --processes {processes}: "
                    f"{len(missing)} page(s) missing: {sorted(missing)}"
                )


if __name__ == '__main__':
    unittest.main()
