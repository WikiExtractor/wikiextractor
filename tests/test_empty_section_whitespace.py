"""
Tests for compact()'s empty-section detection, which previously only
recognized a literally, perfectly empty line ('') as blank -- not a
line containing only whitespace (e.g. a single space).

That gap matters because a template call that expands to nothing can
still leave a single residual space behind in its place (a separate,
known behavior -- e.g. a template wrapped for CSS-hiding purposes
like Wikipedia's real Template:Short_description, or various citation/
maintenance templates), rather than genuinely, completely vanishing.
Confirmed directly on a real article (English Wikipedia's "Anarchism"):
several sub-headings ("Secondary sources.", "Tertiary sources.") each
had nothing under them but a single, residual whitespace-only line --
and because `not line` is False for a string that's just a space,
that line got treated as real, first-line section content instead of
recognized as blank, which cleared the pending-empty-section flag and
kept a heading that should have been dropped as empty.

Fixed by checking `not line.strip()` instead of `not line` -- a line
that's empty after stripping whitespace is treated the same as one
that was already completely empty.

Tests call compact() directly rather than going through the full
clean_text()/expand_templates() pipeline: confirmed directly that the
template-expansion step does its own whitespace handling that can mask
the exact shape needed to trigger this, while compact() itself
reproduces it precisely and reliably on its own.

Run with:
    python -m unittest tests.test_empty_section_whitespace -v
or, from the tests/ directory:
    python -m unittest test_empty_section_whitespace -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex
from wikiextractor.extract import Extractor


class WhitespaceOnlySectionDroppedTests(unittest.TestCase):

    def test_heading_followed_only_by_whitespace_residue_is_dropped(self):
        text = 'Intro.\n\n==Heading==\n \n\n==Next Heading==\nReal content.'
        _extractor = Extractor(1, "1", "https://x", "Test", [text])
        result = ex.compact(text, extractor=_extractor)
        self.assertNotIn('Heading.', result)
        self.assertIn('Next Heading.', result)
        self.assertIn('Real content.', result)

    def test_real_world_shape_nested_empty_subheadings(self):
        # Matches the real case this was found on: a top-level heading
        # whose only content is further sub-headings that are
        # themselves entirely empty (whitespace residue only) -- the
        # whole chain should be dropped, not just the innermost ones.
        text = ('Some real article content.\n\n'
                '==References==\n'
                '===General sources===\n'
                '===Secondary sources===\n'
                ' \n\n'
                '===Tertiary sources===\n'
                ' \n')
        _extractor = Extractor(1, "1", "https://x", "Test", [text])
        result = ex.compact(text, extractor=_extractor)
        self.assertNotIn('References.', result)
        self.assertNotIn('General sources.', result)
        self.assertNotIn('Secondary sources.', result)
        self.assertNotIn('Tertiary sources.', result)
        self.assertIn('Some real article content.', result)


class NonEmptySectionsStillKeptTests(unittest.TestCase):
    """Sanity check: a section with genuine content -- even content
    that happens to be short, or that sits right next to whitespace --
    is never incorrectly dropped.
    """

    def test_heading_with_real_content_survives(self):
        text = 'Intro.\n\n==Heading==\nReal, substantive content here.\n'
        _extractor = Extractor(1, "1", "https://x", "Test", [text])
        result = ex.compact(text, extractor=_extractor)
        self.assertIn('Heading.', result)
        self.assertIn('Real, substantive content here.', result)

    def test_heading_with_short_but_real_content_survives(self):
        # Even very short real content (not just whitespace) must
        # still count as non-empty.
        text = 'Intro.\n\n==See also==\nRelated topic\n'
        _extractor = Extractor(1, "1", "https://x", "Test", [text])
        result = ex.compact(text, extractor=_extractor)
        self.assertIn('See also.', result)
        self.assertIn('Related topic', result)


if __name__ == '__main__':
    unittest.main()
