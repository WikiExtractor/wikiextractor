"""
Tests for escaping in the <doc> tag that extract() writes.

The tag was built by direct string interpolation:

    '<doc id="%s" url="%s" title="%s">' % (self.id, self.url, self.title)

so an article title containing a double quote closed the attribute
early. '"Weird Al" Yankovic' (id 18938265) came out as:

    <doc id="18938265" url="..." title=""Weird Al" Yankovic">

which no XML or HTML parser reads as one title, and which silently
truncates the value to the empty string for the lenient ones. Titles
containing &, < or > break it in the same way; ampersands in
particular are common enough (AT&T, Rock & Roll) that the output was
not well formed on a decent number of enwiki pages.

escapeDocAttribute() escapes ", &, < and > and deliberately leaves
apostrophes alone -- they are safe between double quotes, and
escaping them would rewrite a large share of all titles to no
purpose.

The title also opens the document as ordinary text on the line below
the tag. That copy is escaped the way clean_text() escapes the rest of
the body: & < > become entities and quotes are left as they are, under
the same html_safe flag.

So the two copies answer to different things, which is the split
HtmlSafeScopeTests below pins down. The attribute is markup this code
emits and is escaped whatever --html-safe says; the title line is
article text passing through and follows the flag, exactly as the body
does.

Run with:
    python -m unittest tests.test_doc_tag_escaping -v
or, from the tests/ directory:
    python -m unittest test_doc_tag_escaping -v
"""

import io
import json
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ElementTree

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def render(title, body='Some text about it.', page_id=1, html_safe=True, **kwargs):
    extractor = ex.Extractor(page_id, "1", "https://en.wikipedia.org/wiki?curid=",
                             title, [body], **kwargs)
    out = io.StringIO()
    extractor.extract(out, html_safe=html_safe)
    return out.getvalue()


def header_of(output):
    return output.split('\n', 1)[0]


def title_line_of(output):
    return output.split('\n')[1]


class EscapeDocAttributeTests(unittest.TestCase):

    def test_double_quote(self):
        self.assertEqual(ex.escapeDocAttribute('"Weird Al" Yankovic'),
                         '&quot;Weird Al&quot; Yankovic')

    def test_ampersand(self):
        self.assertEqual(ex.escapeDocAttribute('AT&T'), 'AT&amp;T')

    def test_angle_brackets(self):
        self.assertEqual(ex.escapeDocAttribute('a <b> c'), 'a &lt;b&gt; c')

    def test_apostrophe_is_left_alone(self):
        self.assertEqual(ex.escapeDocAttribute("King's College London"),
                         "King's College London")

    def test_ampersand_is_escaped_before_the_others(self):
        # Escaping < first and & second would give &amp;lt;.
        self.assertEqual(ex.escapeDocAttribute('<'), '&lt;')
        self.assertEqual(ex.escapeDocAttribute('&lt;'), '&amp;lt;')

    def test_ordinary_title_is_unchanged(self):
        self.assertEqual(ex.escapeDocAttribute('William Perrin (bishop)'),
                         'William Perrin (bishop)')

    def test_non_string_values_are_accepted(self):
        # id is passed through as whatever the caller supplied.
        self.assertEqual(ex.escapeDocAttribute(18938265), '18938265')


class DocHeaderTests(unittest.TestCase):

    def test_quoted_title_does_not_close_the_attribute(self):
        header = header_of(render('"Weird Al" Yankovic'))
        self.assertIn('title="&quot;Weird Al&quot; Yankovic"', header)
        self.assertNotIn('title=""Weird Al"', header)

    def test_ampersand_in_title(self):
        self.assertIn('title="AT&amp;T"', header_of(render('AT&T')))

    def test_angle_brackets_in_title(self):
        self.assertIn('title="Nokia 3310 &lt;&gt; 3410"',
                      header_of(render('Nokia 3310 <> 3410')))

    def test_apostrophe_in_title_is_not_rewritten(self):
        self.assertIn('title="King\'s College London"',
                      header_of(render("King's College London")))

    def test_id_is_escaped(self):
        self.assertIn('id="a&amp;b"', header_of(render('T', page_id='a&b')))


class WellFormednessTests(unittest.TestCase):
    """The point of the escaping: the tag parses, and the title
    survives a round trip through a parser."""

    awkward_titles = [
        '"Weird Al" Yankovic',
        'AT&T',
        "King's College London",
        'Nokia 3310 <> 3410',
        '"Heroes"',
        'Sanford & Son',
        '<nowiki> tag',
        'A "mixed" & <awkward> title',
    ]

    def test_header_parses_as_xml(self):
        for title in self.awkward_titles:
            with self.subTest(title=title):
                header = header_of(render(title))
                ElementTree.fromstring(header + '</doc>')

    def test_title_round_trips_through_a_parser(self):
        for title in self.awkward_titles:
            with self.subTest(title=title):
                header = header_of(render(title))
                element = ElementTree.fromstring(header + '</doc>')
                self.assertEqual(element.get('title'), title)

    def test_whole_document_parses_as_xml(self):
        output = render('A "mixed" & <awkward> title', body='Plain body text.')
        element = ElementTree.fromstring(output)
        self.assertEqual(element.get('title'), 'A "mixed" & <awkward> title')

    def test_id_and_url_round_trip(self):
        output = render('T', page_id=18938265)
        element = ElementTree.fromstring(output)
        self.assertEqual(element.get('id'), '18938265')
        self.assertEqual(element.get('url'),
                         'https://en.wikipedia.org/wiki?curid=?curid=18938265')


class TitleLineTests(unittest.TestCase):
    """The title's second appearance, as the opening line of the
    document text."""

    def test_ampersand_is_escaped_like_the_body(self):
        self.assertEqual(title_line_of(render('AT&T')), 'AT&amp;T')

    def test_angle_brackets_are_escaped_like_the_body(self):
        self.assertEqual(title_line_of(render('Nokia 3310 <> 3410')),
                         'Nokia 3310 &lt;&gt; 3410')

    def test_quotes_are_left_as_they_are_like_the_body(self):
        # clean_text() escapes with quote=False; the title line
        # matches, so the text reads as it does on the wiki.
        self.assertEqual(title_line_of(render('"Weird Al" Yankovic')),
                         '"Weird Al" Yankovic')

    def test_html_safe_off_leaves_the_title_line_raw(self):
        output = render('AT&T', html_safe=False)
        self.assertEqual(title_line_of(output), 'AT&T')

    def test_html_safe_off_still_escapes_the_attribute(self):
        # The attribute has to be escaped whatever html_safe says:
        # it is markup this code emits, not article text passing
        # through.
        self.assertIn('title="AT&amp;T"', header_of(render('AT&T', html_safe=False)))


class HtmlSafeScopeTests(unittest.TestCase):
    """What --html-safe/--no-html-safe reaches.

    Inside extract.py it is one call: html.escape(text, quote=False)
    at the end of clean(), plus the title line, which matches it. The
    source's own entities are decoded earlier by unescape() in either
    case, so --no-html-safe means the decoded characters are handed
    over as they are rather than re-encoded.
    """

    BODY = 'Text with < and & in it.'

    def test_body_is_escaped_by_default(self):
        self.assertIn('Text with &lt; and &amp; in it.', render('T', body=self.BODY))

    def test_body_is_raw_when_html_safe_is_off(self):
        self.assertIn('Text with < and & in it.',
                      render('T', body=self.BODY, html_safe=False))

    def test_body_quotes_are_never_escaped_either_way(self):
        for html_safe in (True, False):
            with self.subTest(html_safe=html_safe):
                output = render('T', body='He said "hello".', html_safe=html_safe)
                self.assertIn('He said "hello".', output)

    def test_title_line_and_body_agree_with_each_other(self):
        for html_safe in (True, False):
            with self.subTest(html_safe=html_safe):
                output = render('AT&T', body='More AT&T text.', html_safe=html_safe)
                expected = 'AT&amp;T' if html_safe else 'AT&T'
                self.assertEqual(title_line_of(output), expected)
                self.assertIn(expected + ' text.', output)

    def test_attribute_does_not_follow_the_flag(self):
        for html_safe in (True, False):
            with self.subTest(html_safe=html_safe):
                header = header_of(render('AT&T', html_safe=html_safe))
                self.assertIn('title="AT&amp;T"', header)

    def test_no_html_safe_is_accepted_by_the_cli(self):
        # --html-safe is declared with BooleanOptionalAction, so the
        # negative form exists and takes no value. Without that, there
        # was no way to turn the flag off from the command line at
        # all. argparse rejects unknown options during parse_args(),
        # before the (nonexistent) input file is opened, so this needs
        # no dump.
        result = subprocess.run(
            [sys.executable, "-m", "wikiextractor.WikiExtractor",
             "--no-html-safe", "nonexistent_input_file.xml"],
            cwd="..", capture_output=True, text=True
        )
        self.assertNotIn("unrecognized arguments", result.stderr)
        self.assertNotIn("expected one argument", result.stderr)

    def test_html_safe_takes_no_value(self):
        result = subprocess.run(
            [sys.executable, "-m", "wikiextractor.WikiExtractor",
             "--html-safe", "False", "nonexistent_input_file.xml"],
            cwd="..", capture_output=True, text=True
        )
        # "False" is read as a positional, not as the flag's value, so
        # argparse sees too many positionals rather than accepting a
        # string that would have been truthy whatever it said.
        self.assertNotEqual(result.returncode, 0)


class OtherOutputModesTests(unittest.TestCase):

    def test_json_mode_carries_the_title_unescaped(self):
        # json.dumps does its own quoting, so the title is stored as
        # written and comes back identical.
        output = render('"Weird Al" Yankovic', to_json=True)
        self.assertEqual(json.loads(output)['title'], '"Weird Al" Yankovic')

    def test_text_mode_emits_no_doc_tag(self):
        output = render('"Weird Al" Yankovic', to_text=True)
        self.assertNotIn('<doc', output)


if __name__ == '__main__':
    unittest.main()
