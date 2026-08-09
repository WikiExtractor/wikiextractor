"""
Tests for urlencode/urldecode -- note no '#' prefix, matching real
MediaWiki's own syntax for these (part of the "string functions"
extension, like formatnum, lc, uc, padleft/padright -- not the
'#'-prefixed ParserFunctions-family branching functions).

urlencode already existed (a direct alias for urllib.parse.quote) but
had no dedicated test coverage anywhere in this suite -- confirmed via
a direct search before writing these. urldecode is new, added as the
matching inverse (urllib.parse.unquote), deliberately chosen to stay
consistent with whatever urlencode actually does today: both are
built on Python's plain quote()/unquote() pair (%20 for spaces), not
quote_plus()/unquote_plus() (+ for spaces) -- so a round trip through
both is correct even though this doesn't implement MediaWiki's
optional second "type" parameter (QUERY/PATH/WIKI) that would select
between those conventions; neither function accepts it today, and
both silently discard it via their own *rest catch-all in the
parserFunctions dict if a real template happens to pass one.

Both are plain aliases (from urllib.parse import quote as urlencode /
unquote as urldecode), not separately-named wrapper functions like
sharp_padleft/sharp_formatnum -- so they're tested directly as
ex.urlencode()/ex.urldecode() below, and separately through the real
parser-function dispatch path via clean_text() in the end-to-end test.

Run with:
    python -m unittest tests.test_urlencode_urldecode -v
or, from the tests/ directory:
    python -m unittest test_urlencode_urldecode -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


class UrlencodeTests(unittest.TestCase):

    def test_spaces_become_percent_20(self):
        self.assertEqual(ex.urlencode('hello world'), 'hello%20world')

    def test_reserved_characters_are_encoded(self):
        self.assertEqual(ex.urlencode('a=b&c?d'), 'a%3Db%26c%3Fd')

    def test_non_ascii_text_is_encoded(self):
        # Matches the actual kind of data this codebase processes --
        # Urdu script, not just English test strings.
        encoded = ex.urlencode('سائی را')
        self.assertNotIn('سائی', encoded)
        self.assertTrue(encoded.startswith('%'))


class UrldecodeTests(unittest.TestCase):

    def test_percent_20_becomes_space(self):
        self.assertEqual(ex.urldecode('hello%20world'), 'hello world')

    def test_reserved_characters_are_decoded(self):
        self.assertEqual(ex.urldecode('a%3Db%26c%3Fd'), 'a=b&c?d')


class UrlencodeUrldecodeRoundTripTests(unittest.TestCase):
    """The property that actually matters most in practice: encoding
    then decoding returns the original text exactly, for the kind of
    content this codebase really processes (non-ASCII script, and
    reserved URL characters together in the same string).
    """

    def test_round_trip_ascii_with_reserved_characters(self):
        original = 'a=b&c?d e'
        self.assertEqual(ex.urldecode(ex.urlencode(original)), original)

    def test_round_trip_non_ascii_text(self):
        original = 'سائی را نرسمہا ریڈی & foo=bar'
        self.assertEqual(ex.urldecode(ex.urlencode(original)), original)

    def test_real_pipeline_round_trip_through_nested_template_calls(self):
        # Not the functions in isolation -- the real chain
        # (clean_text() -> expandTemplate() -> callParserFunction()),
        # confirming both are actually reachable and correctly nested
        # via real wikitext syntax, not just callable directly.
        # Avoids '&' here specifically: clean_text()'s own output is
        # HTML-escaped (a separate, correct, already-covered concern
        # elsewhere), which would turn a literal '&' into '&amp;' and
        # have nothing to do with url encode/decode round-tripping.
        extractor = ex.Extractor(1, "1", "https://x", "Test Article", [])
        wikitext = '{{urldecode:{{urlencode:سائی را نرسمہا ریڈی ? foo=bar}}}}'
        result = extractor.clean_text(wikitext, expand_templates=True)
        self.assertIn('سائی را نرسمہا ریڈی ? foo=bar', '\n'.join(result))


if __name__ == '__main__':
    unittest.main()
