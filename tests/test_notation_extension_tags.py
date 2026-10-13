"""
Tests for extension tags whose body is notation, code or data rather
than prose.

<score> holds LilyPond, <syntaxhighlight> holds source code, <graph>,
<mapframe> and <maplink> hold Vega or GeoJSON, <templatedata> holds
JSON, <templatestyles> holds CSS, and <charinsert> and <indicator>
hold a character palette and a page-status icon. A reader sees an
engraved stave or a map, never the markup, so none of it belongs in
extracted text.

A <score> block surfaced this, arriving in enwiki output as

    &lt;score raw="1" sound="1" &gt;
     \\header {
     piece = "Allegretto grazioso"
    \\acciaccatura b'8(c-.) d,-.

<chem> and <ce> are the exception and stay: they wrap a chemical
formula that reads as part of the sentence around it, so the formula
is kept and only the tags go.

Run with:
    python -m unittest tests.test_notation_extension_tags -v
or, from the tests/ directory:
    python -m unittest test_notation_extension_tags -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def clean(wikitext):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates={}, templatePrefix='Template:')
    return '\n'.join(extractor.clean_text(wikitext, expand_templates=False))


class DiscardedTests(unittest.TestCase):

    def test_a_score_block_goes_entirely(self):
        self.assertEqual(
            clean('Before.\n<score raw="1" sound="1">\n'
                  '\\header { piece = "Allegretto grazioso" }\n'
                  "\\acciaccatura b'8(c-.) d,-. e4\n</score>\nAfter."),
            'Before.\nAfter.')

    def test_syntaxhighlight_goes_like_its_older_name_source(self):
        self.assertEqual(
            clean('Code:\n<syntaxhighlight lang="c">int main(){}'
                  '</syntaxhighlight>\nDone.'),
            'Code:\nDone.')

    def test_data_and_style_bodies_go(self):
        for tag, body in (('graph', '{"version":2}'),
                          ('mapframe', '{"type":"Feature"}'),
                          ('maplink', '{"type":"Feature"}'),
                          ('templatedata', '{"params":{}}'),
                          ('templatestyles', '.x{color:red}'),
                          ('charinsert', 'á é í'),
                          ('indicator', 'padlock')):
            with self.subTest(tag=tag):
                self.assertEqual(
                    clean('A <%s>%s</%s> B' % (tag, body, tag)), 'A B')

    def test_the_prose_around_a_score_is_untouched(self):
        result = clean('The theme is stated twice.\n'
                       '<score>\\relative c\' { c d e f }</score>\n'
                       'It returns in the coda.')
        self.assertIn('The theme is stated twice.', result)
        self.assertIn('It returns in the coda.', result)
        self.assertNotIn('relative', result)


class ChemistryIsKeptTests(unittest.TestCase):
    """A formula is part of the sentence, so only the tags go."""

    def test_chem(self):
        self.assertEqual(clean('The reaction yields <chem>H2O</chem> and salt.'),
                         'The reaction yields H2O and salt.')

    def test_ce(self):
        self.assertEqual(clean('Water is <ce>H2O</ce>.'), 'Water is H2O.')

    def test_no_tag_text_survives(self):
        result = clean('Water is <ce>H2O</ce>.')
        self.assertNotIn('ce', result.replace('Water', '').replace('H2O', ''))

    def test_a_similarly_spelled_tag_is_not_matched(self):
        # <ce> must not match the start of <center>.
        self.assertIn('middle', clean('a<center>middle</center>b'))


if __name__ == '__main__':
    unittest.main()
