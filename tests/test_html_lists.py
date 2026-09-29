"""
Tests for list handling in compact(), which only produces output at
all under --html (HtmlFormatting); without it every list line is
dropped, text and all.

Three things were wrong with it.

The line that ends a list was deleted. Closing the list was written
as one more branch of compact()'s if/elif chain over the line:

    elif len(listLevel):
        for c in reversed(listLevel):
            page.append(listClose[c])
        listLevel = []

Reaching that branch is what consumed the line, so the line never
made it to the branch further down that appends ordinary prose. Every
list in --html output was followed by a hole where its next sentence
should be.

A heading got the closing tag after it, not before. The section
branch runs earlier in the same chain and ends in "continue", so a
list followed by "== Next ==" emitted <h2> first and </ul> only once
some later line arrived -- if one ever did.

A list running to the end of the text was never closed at all,
because nothing followed it to trigger the branch.

Separately, listItem['#'] read '<li>%s</<li>', so every numbered list
item closed with a malformed tag.

Run with:
    python -m unittest tests.test_html_lists -v
or, from the tests/ directory:
    python -m unittest test_html_lists -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def html(wikitext):
    """Extract with HtmlFormatting on, which is the only mode in which
    lists survive."""
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates={}, templatePrefix='Template:',
                             HtmlFormatting=True)
    return '\n'.join(extractor.clean_text(wikitext, expand_templates=False))


def plain(wikitext):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates={}, templatePrefix='Template:')
    return '\n'.join(extractor.clean_text(wikitext, expand_templates=False))


class LineAfterAListTests(unittest.TestCase):
    """The list has to be closed without eating the line that closed
    it."""

    def test_prose_after_a_list_survives(self):
        self.assertEqual(html('Below:\n* a\n* b\nAfter the list.'),
                         'Below:\n<ul>\n<li>a</li>\n<li>b</li>\n</ul>\nAfter the list.')

    def test_prose_after_a_numbered_list_survives(self):
        self.assertIn('After.', html('Intro:\n# one\n# two\nAfter.'))

    def test_prose_after_a_definition_list_survives(self):
        self.assertIn('After.', html('Intro:\n; term\nAfter.'))

    def test_prose_after_a_nested_list_survives(self):
        self.assertIn('After.', html('Intro:\n* a\n** a1\nAfter.'))

    def test_an_indent_after_a_list_survives(self):
        self.assertIn('indented', html('Intro:\n* a\n: indented\nAfter.'))

    def test_prose_between_two_lists_survives(self):
        result = html('* a\nmiddle\n* b\nend')
        self.assertIn('middle', result)
        self.assertIn('end', result)

    def test_a_blank_line_after_a_list_still_closes_it(self):
        self.assertEqual(html('Below:\n* a\n\nAfter the blank.'),
                         'Below:\n<ul>\n<li>a</li>\n</ul>\nAfter the blank.')

    def test_the_words_of_the_list_itself_are_kept(self):
        result = html('* रञ्जना लिपि\n* प्रचलित लिपि')
        self.assertIn('रञ्जना लिपि', result)
        self.assertIn('प्रचलित लिपि', result)


class ClosingTagPlacementTests(unittest.TestCase):

    def test_a_list_is_closed_before_a_following_heading(self):
        result = html('Below:\n* a\n* b\n== Next ==\nSection text.')
        self.assertLess(result.index('</ul>'), result.index('<h2>'))

    def test_the_heading_and_its_text_both_survive(self):
        result = html('Below:\n* a\n== Next ==\nSection text.')
        self.assertIn('<h2>Next</h2>', result)
        self.assertIn('Section text.', result)

    def test_a_list_at_the_end_of_the_text_is_closed(self):
        self.assertEqual(html('Intro:\n* a\n* b'),
                         'Intro:\n<ul>\n<li>a</li>\n<li>b</li>\n</ul>')

    def test_a_nested_list_at_the_end_closes_every_level(self):
        self.assertEqual(html('* a\n** a1').count('</ul>'), 2)

    def test_open_and_close_tags_balance(self):
        result = html('Intro:\n* a\n** a1\nmiddle\n# one\n# two\n'
                      '== H ==\n; term\nend')
        for open_tag, close_tag in (('<ul>', '</ul>'), ('<ol>', '</ol>'),
                                    ('<dl>', '</dl>')):
            with self.subTest(tag=open_tag):
                self.assertEqual(result.count(open_tag), result.count(close_tag))


class ListItemMarkupTests(unittest.TestCase):

    def test_a_bulleted_item_is_well_formed(self):
        self.assertIn('<li>a</li>', html('* a'))

    def test_a_numbered_item_is_well_formed(self):
        # listItem['#'] used to close with '</<li>'.
        result = html('# one')
        self.assertIn('<li>one</li>', result)
        self.assertNotIn('</<li>', result)

    def test_every_li_closes_properly(self):
        result = html('* a\n* b\nmiddle\n# one\n# two')
        self.assertEqual(result.count('<li>'), result.count('</li>'))
        self.assertNotIn('</<', result)


class PlainTextModeTests(unittest.TestCase):
    """Without --html the list lines are still dropped. That is
    compact()'s longstanding behavior and none of the above changes
    it; these pin that the line after a list is not collateral
    damage there either."""

    def test_list_lines_are_dropped(self):
        self.assertEqual(plain('Below:\n* a\n* b\nAfter the list.'),
                         'Below:\nAfter the list.')

    def test_no_markup_leaks_into_plain_output(self):
        result = plain('Intro:\n* a\n# one\n; term\nAfter.')
        self.assertNotIn('<ul>', result)
        self.assertNotIn('<li>', result)
        self.assertIn('After.', result)

    def test_a_heading_after_a_list_is_unaffected(self):
        result = plain('Below:\n* a\n== Next ==\nSection text.')
        self.assertIn('Next.', result)
        self.assertIn('Section text.', result)


if __name__ == '__main__':
    unittest.main()
