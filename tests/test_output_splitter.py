"""
Tests for OutputSplitter's --compress (bz2) output path, which was
completely broken: open() used bz2.BZ2File(filename, 'w'), which is
strictly binary-only (unlike bz2.open(), it has no text-mode variant
and accepts no encoding parameter at all), while write() passed it
str data unchanged. Confirmed directly, end to end via the real CLI:
this crashes immediately with

    TypeError: memoryview: a bytes-like object is required, not 'str'

-- but silently from the user's perspective, since the crash happens
inside reduce_process, a separate forked worker process. The main
process reports a clean exit ("wrote 0 article(s) total",
returncode 0) with no visible indication anything failed; --compress
just silently produced empty output.

The plain, uncompressed branch had a second, separate, unrelated gap:
no explicit encoding on open(), same class of issue as the earlier
load_templates()/--article fixes (relies on the platform's
locale-preferred default rather than UTF-8 explicitly).

First attempt at a fix used bz2.open(filename, 'wt', encoding='utf-8')
for the compressed branch -- confirmed this introduces a DIFFERENT
regression: reserve()'s file-splitting logic calls self.file.tell(),
which bz2.open()'s text-mode wrapper does not support (the underlying
compressed stream isn't seekable in write mode), raising
io.UnsupportedOperation. Confirmed directly that plain bz2.BZ2File
(binary) DOES support .tell() correctly, returning the uncompressed
byte offset written so far -- exactly what reserve() needs. So the
actual fix keeps bz2.BZ2File for the compressed branch (preserving
.tell()), and instead has write() itself encode to UTF-8 bytes before
writing, only for the compressed case.

Later replaced with a cleaner approach (adapted from PR #333):
tracking a self.size counter incremented by write()'s own return
value, rather than depending on self.file.tell() at all -- this
avoids needing any particular stream type to support .tell()
correctly in the first place. But this introduced a different, real
bug of its own: for a text-mode file object, write(str) returns the
number of CHARACTERS written, not bytes -- fine for pure ASCII, but a
significant undercount for non-ASCII-heavy content (confirmed with
real Saraiki/Arabic-script text, where most characters take 2+ bytes
in UTF-8), silently letting max_file_size be measured in the wrong
unit and output files grow substantially larger than requested (a
100,000-byte target produced a 156,000-byte actual file). Fixed by
computing len(data.encode('utf-8')) explicitly for both the reserve()
check and the size counter, rather than trusting write()'s return
value or len(data) (character count) for either.

Run with:
    python -m unittest tests.test_output_splitter -v
or, from the tests/ directory:
    python -m unittest test_output_splitter -v
"""

import bz2
import glob
import os
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we


class FakeNextFile:
    """Minimal stand-in for the real NextFile: hands out a fresh,
    numbered path under a given prefix each time next() is called,
    matching the one method OutputSplitter actually uses.
    """
    def __init__(self, prefix):
        self.prefix = prefix
        self.n = 0

    def next(self):
        self.n += 1
        return f'{self.prefix}_{self.n}'


class OutputSplitterTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = os.path.dirname(os.path.abspath(__file__))
        self._paths = []

    def tearDown(self):
        for pattern in self._paths:
            for path in glob.glob(pattern):
                os.remove(path)

    def make_splitter(self, name, compress, max_file_size=1_000_000):
        prefix = os.path.join(self.tmpdir, name)
        self._paths.append(prefix + '*')
        return we.OutputSplitter(FakeNextFile(prefix), max_file_size, compress)


class ByteAccurateSizeTrackingTests(OutputSplitterTestCase):
    """The file-splitting size limit is documented and specified in
    bytes (--bytes / max_file_size), so it must be tracked in bytes
    regardless of how many bytes a given piece of text happens to
    take in UTF-8 -- confirmed this silently broke for non-ASCII-heavy
    content specifically, not just as a theoretical unit mismatch.
    """

    # Real, representative Saraiki (Arabic-script) text -- not just a
    # couple of accented Latin characters, since the bug's actual
    # impact scales with how much of the content is outside ASCII.
    SARAIKI_CHUNK = 'ݙݙݙ سنسکرت ٻولی وچ لکھے ہوئے ودا کتاباں دی \n'

    def test_compressed_output_file_size_respects_byte_limit_with_non_ascii_text(self):
        max_size = 100_000
        splitter = self.make_splitter('_os_bytes_compressed', compress=True,
                                       max_file_size=max_size)
        for _ in range(2000):
            splitter.write(self.SARAIKI_CHUNK)
        splitter.close()

        files = sorted(glob.glob(f'{splitter.nextFile.prefix}_*.bz2'))
        self.assertGreater(len(files), 1,
                            "expected this much text to require more than one file")
        for path in files[:-1]:  # the last file is a partial remainder, not bound by the limit
            with bz2.open(path, 'rt', encoding='utf-8') as f:
                actual_bytes = len(f.read().encode('utf-8'))
            self.assertLessEqual(
                actual_bytes, max_size,
                f"{path}: {actual_bytes} bytes exceeds the {max_size}-byte limit "
                f"-- size tracking must be counting characters, not bytes")

    def test_uncompressed_output_file_size_respects_byte_limit_with_non_ascii_text(self):
        max_size = 100_000
        splitter = self.make_splitter('_os_bytes_plain', compress=False,
                                       max_file_size=max_size)
        for _ in range(2000):
            splitter.write(self.SARAIKI_CHUNK)
        splitter.close()

        files = sorted(glob.glob(f'{splitter.nextFile.prefix}_*'))
        self.assertGreater(len(files), 1,
                            "expected this much text to require more than one file")
        for path in files[:-1]:
            actual_bytes = os.path.getsize(path)
            self.assertLessEqual(
                actual_bytes, max_size,
                f"{path}: {actual_bytes} bytes exceeds the {max_size}-byte limit")


class CompressedWritingTests(OutputSplitterTestCase):
    """The core regression coverage the earlier exploration didn't
    have at all: writing to bz2-compressed output.
    """

    def test_simple_ascii_write_does_not_crash(self):
        # Before the fix, this line alone was enough to crash with
        # TypeError -- the most basic possible use of --compress.
        splitter = self.make_splitter('_os_ascii', compress=True)
        splitter.write('Hello, world.\n')
        splitter.close()

        with bz2.open(f'{splitter.nextFile.prefix}_1.bz2', 'rt', encoding='utf-8') as f:
            self.assertEqual(f.read(), 'Hello, world.\n')

    def test_non_ascii_content_round_trips_correctly(self):
        # Real, representative content -- not just Latin-1-safe text.
        splitter = self.make_splitter('_os_unicode', compress=True)
        text = 'Hello سنڌی and 日本語 too.\n'
        splitter.write(text)
        splitter.close()

        with bz2.open(f'{splitter.nextFile.prefix}_1.bz2', 'rt', encoding='utf-8') as f:
            self.assertEqual(f.read(), text)

    def test_multiple_writes_to_same_file_accumulate_correctly(self):
        splitter = self.make_splitter('_os_multi', compress=True)
        splitter.write('first.\n')
        splitter.write('second.\n')
        splitter.write('third.\n')
        splitter.close()

        with bz2.open(f'{splitter.nextFile.prefix}_1.bz2', 'rt', encoding='utf-8') as f:
            self.assertEqual(f.read(), 'first.\nsecond.\nthird.\n')

    def test_file_splitting_works_across_multiple_compressed_files(self):
        # This is exactly what an earlier, alternate fix attempt broke:
        # switching to bz2.open()'s text-mode wrapper made reserve()'s
        # self.file.tell() call raise io.UnsupportedOperation, since a
        # compressed stream isn't seekable in write mode. Plain
        # bz2.BZ2File (binary) supports .tell() correctly, which is
        # what the real fix relies on.
        splitter = self.make_splitter('_os_split', compress=True, max_file_size=20)
        splitter.write('first chunk of text\n')
        splitter.write('second chunk of text\n')
        splitter.write('third chunk of text\n')
        splitter.close()

        files = sorted(glob.glob(f'{splitter.nextFile.prefix}_*.bz2'))
        self.assertEqual(len(files), 3,
                          f"expected 3 separate output files, got {len(files)}: {files}")
        contents = []
        for path in files:
            with bz2.open(path, 'rt', encoding='utf-8') as f:
                contents.append(f.read())
        self.assertEqual(contents, ['first chunk of text\n',
                                     'second chunk of text\n',
                                     'third chunk of text\n'])


class UncompressedWritingTests(OutputSplitterTestCase):
    """Sanity checks: the plain, non-compressed path must keep
    working exactly as before, including with non-ASCII content
    (verifying the explicit encoding='utf-8' addition didn't just
    happen to work by coincidence on this system's own locale).
    """

    def test_plain_write_round_trips_correctly(self):
        splitter = self.make_splitter('_os_plain', compress=False)
        splitter.write('Hello, world.\n')
        splitter.close()

        with open(f'{splitter.nextFile.prefix}_1', encoding='utf-8') as f:
            self.assertEqual(f.read(), 'Hello, world.\n')

    def test_plain_non_ascii_content_round_trips_correctly(self):
        splitter = self.make_splitter('_os_plain_unicode', compress=False)
        text = 'Hello سنڌی and 日本語 too.\n'
        splitter.write(text)
        splitter.close()

        with open(f'{splitter.nextFile.prefix}_1', encoding='utf-8') as f:
            self.assertEqual(f.read(), text)

    def test_plain_file_splitting_still_works(self):
        splitter = self.make_splitter('_os_plain_split', compress=False, max_file_size=20)
        splitter.write('first chunk of text\n')
        splitter.write('second chunk of text\n')
        splitter.close()

        files = sorted(glob.glob(f'{splitter.nextFile.prefix}_*'))
        self.assertEqual(len(files), 2,
                          f"expected 2 separate output files, got {len(files)}: {files}")


if __name__ == '__main__':
    unittest.main()
