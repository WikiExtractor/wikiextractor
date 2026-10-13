"""Tests for tags whose attribute quoting is malformed.

A tag ends at its first '>' (see tests/test_tag_end_at_first_gt.py for
that rule and MediaWiki's statement of it), which means a quote that
never closes where its editor intended cannot reach past the tag it
sits in. The three articles below are why that matters: each one has a
tag whose quoting is broken, and each broke differently when a quoted
value was allowed to run to the next quote character in the text.

  dewiki Barockmusik -- <ref name="Braun" details="S. 7/>. The second
  value never closes. No '>' satisfied the open pattern at all, so
  dropNested() paired nothing, the orphan sweep in clean() stripped
  the page's </ref> tags, and all 68 of the article's citations stayed
  in the text with their opening tags escaped to "&lt;ref" on output.

  enwiki Abraxas -- a <gallery> caption quoting a book title, so the
  value ends early at the inner '"' and an apostrophe in the remainder
  opens a run whose partner is a paragraph below. The open tag reached
  past the </gallery>, which then had nothing to pair with, and the
  gallery's file list stayed in the article.

  urwiki ابن سینا -- <ref ="la> followed by a citation whose title is
  quoted. The run "la>Nahyan A.G. Fancy (2006), pp. 80-81, " begins in
  the attribute, crosses the tag's real '>', and ends at the title's
  opening quote. The open tag came out 256 characters long, paired
  with a far later </ref>, and dropNested() deleted 33,000 characters
  -- the second half of the article, with nothing in the output to
  mark the loss.

The last one is why confining a quoted value to a single line is not
enough: that run is 256 characters on one line.

Run with:
    python -m unittest tests.test_malformed_tag_attributes -v
or, from the tests/ directory:
    python -m unittest test_malformed_tag_attributes -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def clean(wikitext):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates={}, templatePrefix='Template:')
    return '\n'.join(extractor.clean_text(wikitext, expand_templates=False))


# Barockmusik's own shape: the malformed citation, then ordinary ones
# whose values contain the quote characters an unbounded scan lands on.
BAROCK = ('Satz eins.<ref name="Braun" details="S. 7/>\n'
          'Absatz zwei.<ref>Susanne Rode-Breymann: "Das 17. Jahrhundert." S. 68.</ref>\n'
          'Absatz drei.<ref name="HLange_117">Lange, S. 117.</ref>\n'
          'Absatz vier.<ref>Noch eine Quelle.</ref>\n')


class UnterminatedQuoteTests(unittest.TestCase):

    def test_following_paragraphs_survive(self):
        result = clean(BAROCK)
        for paragraph in ('Satz eins.', 'Absatz zwei.', 'Absatz drei.', 'Absatz vier.'):
            self.assertIn(paragraph, result)

    def test_following_citations_are_still_dropped(self):
        result = clean(BAROCK)
        self.assertNotIn('ref', result)
        self.assertNotIn('Rode-Breymann', result)
        self.assertNotIn('Lange, S. 117', result)

    def test_the_malformed_tag_itself_leaves_nothing_behind(self):
        self.assertEqual(clean('Satz eins.<ref name="Braun" details="S. 7/>'), 'Satz eins.')

    def test_result_matches_the_same_article_written_correctly(self):
        self.assertEqual(clean(BAROCK),
                         clean(BAROCK.replace('details="S. 7/>', 'details="S. 7"/>')))

    def test_an_unterminated_quote_on_a_paired_tag(self):
        # No trailing slash this time, so the tag is a genuine open
        # whose </ref> follows. Its damage still stops at the newline.
        result = clean('Eins.<ref name="Braun details="S. 7">Quelle.</ref>\nZwei.\nDrei.\n')
        self.assertIn('Zwei.', result)
        self.assertIn('Drei.', result)


class NestedQuoteTests(unittest.TestCase):
    """enwiki's Abraxas: a caption quoting a book title inside a
    double-quoted value, so the value ends early and an apostrophe in
    the remainder opens a quoted run of its own. The next apostrophe
    is a paragraph away, and everything up to it is read as part of
    the tag -- taking the </gallery> with it, so nothing pairs and the
    file list stays in the article."""

    ARTICLE = (
        'Gallery.\n'
        '<gallery caption="Prints from Bernard de Montfaucon\'s '
        '"L\'antiquite expliquee et representee en figures" '
        '(Band 2,2) page 358 ff.">\n'
        'File:Montfaucon 358 Abraxas.xcf|Plaque 144\n'
        'File:Montfaucon Abraxas Plaque 145.xcf|Plaque 145\n'
        '</gallery>\n'
        'Anguipede.\n'
        "In a great majority of instances it's associated with a composite figure.\n")

    def test_the_gallery_is_dropped(self):
        result = clean(self.ARTICLE)
        self.assertNotIn('gallery', result)
        self.assertNotIn('Plaque 144', result)
        self.assertNotIn('Montfaucon', result)

    def test_the_prose_on_both_sides_survives(self):
        result = clean(self.ARTICLE)
        self.assertIn('Gallery.', result)
        self.assertIn('Anguipede.', result)
        self.assertIn('composite figure.', result)


class UrduRefTests(unittest.TestCase):
    """urwiki's ابن سینا: <ref ="la>, where the citation that follows
    quotes its title on the same line. A quoted run starting at the
    attribute's own '"' reaches that title's quote, 256 characters
    later and well past the tag's real '>'.
    """

    ARTICLE = ('Pred.<ref ="la>Nahyan A.G. Fancy (2006), pp. 80-81, '
               '"Pulmonary Transit and Bodily Resurrection", Electronic Theses.</ref>\n'
               'Sledeci odstavek.\n'
               'In se eden.\n')

    def test_only_the_citation_is_dropped(self):
        self.assertEqual(clean(self.ARTICLE), 'Pred.\nSledeci odstavek.\nIn se eden.')

    def test_result_matches_the_same_article_written_correctly(self):
        self.assertEqual(clean(self.ARTICLE),
                         clean(self.ARTICLE.replace('<ref ="la>', '<ref>')))


class QuotedAttributeValueTests(unittest.TestCase):
    """Well-formed quoting, for contrast: these never depended on a
    quoted value being read as a unit."""

    def test_greater_than_inside_a_paired_tag_value(self):
        # The tag ends at the inner '>', so ' b">' is part of what the
        # </ref> closes and goes with the element.
        self.assertEqual(clean('A<ref style="a > b">cite</ref>B'), 'AB')

    def test_a_slash_inside_a_value_is_not_a_self_closing_tag(self):
        self.assertEqual(clean('A<ref name="geo/18aug2018-1">cite</ref>B'), 'AB')

    def test_a_line_break_tag_keeps_its_attributes(self):
        self.assertEqual(clean('Eins<br clear="all">Zwei'), 'Eins Zwei')


class MultilineTagTests(unittest.TestCase):
    """A tag may be written across lines, and [^>]* takes newlines
    like any other character, so the tag still ends at its real '>'.
    """

    def test_attributes_split_over_two_lines(self):
        self.assertEqual(clean('A<ref\n  name="x">cite</ref>B'), 'AB')

    def test_a_value_spanning_two_lines(self):
        self.assertEqual(clean('A<ref name="line one\nline two">cite</ref>B'), 'AB')


if __name__ == '__main__':
    unittest.main()
