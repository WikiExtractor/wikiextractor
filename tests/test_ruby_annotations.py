"""
Tests for East Asian ruby annotation markup: <ruby>, <rb>, <rt>, <rp>
and <rtc>.

These are HTML elements, not a scripting construct -- <ruby> wraps a
run of text, <rb> its base, <rt> the small reading printed above it
(furigana in Japanese), <rtc> a second reading, and <rp> the
parentheses a renderer without ruby support falls back to.

None of the five appeared in ignoredTags or discardElements, so they
fell through the tag handling entirely and reached the output
HTML-escaped, as on the real jawiki article 鳥山明 (id 194):

    旧姓：加藤&lt;ruby &gt;&lt;rb&gt;由美&lt;/rb&gt;&lt;rp&gt;（&lt;/rp&gt;
    &lt;rt&gt;よしみ&lt;/rt&gt;&lt;rp&gt;）&lt;/rp&gt;&lt;/ruby&gt;

All five are now in ignoredTags, so every piece of the annotation is
kept and only the tags go -- which reproduces the fallback rendering a
browser without ruby support shows, and is the right plain text
whenever the markup supplies <rp>.

Much of it does not, and then base and reading run together into one
string that is neither: 漢字かんじ rather than 漢字（かんじ）. So
parenthesizeRuby() runs first and supplies the fallback punctuation
where the block has none of its own.

Deciding "none of its own" is the part that needs care, because
jawiki's Template:読み仮名 -- which gives the reading in the opening
sentence of a very large share of all jawiki articles -- marks its
parentheses up as <span class="rp"> rather than as <rp>:

    <ruby class="yomigana"><rb>{{{1}}}</rb><span class="rp">（</span>
    <rt>{{{2}}}</rt><span class="rp">）</span></ruby>

Matching the tag name alone misses those and parenthesizes every one
of those readings twice.

Run with:
    python -m unittest tests.test_ruby_annotations -v
or, from the tests/ directory:
    python -m unittest test_ruby_annotations -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def clean(wikitext, templates=None):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates=templates or {}, templatePrefix='Template:')
    return '\n'.join(extractor.clean_text(wikitext, expand_templates=True))


class RubyTagRegistrationTests(unittest.TestCase):

    def test_every_ruby_tag_keeps_its_content(self):
        for tag in ('ruby', 'rb', 'rt', 'rtc', 'rp'):
            with self.subTest(tag=tag):
                self.assertIn(tag, ex.ignoredTags)

    def test_no_ruby_tag_is_discarded_with_its_content(self):
        for tag in ('ruby', 'rb', 'rt', 'rtc', 'rp'):
            with self.subTest(tag=tag):
                self.assertNotIn(tag, ex.discardElements)


class ParenthesizeRubyTests(unittest.TestCase):
    """The pass in isolation: it rewrites the markup, and leaves the
    tag removal itself to the ordinary ignoredTags handling."""

    def test_reading_without_fallback_punctuation_gains_it(self):
        self.assertEqual(ex.parenthesizeRuby('<ruby>漢字<rt>かんじ</rt></ruby>'),
                         '<ruby>漢字<rt>（かんじ）</rt></ruby>')

    def test_block_with_rp_is_returned_unchanged(self):
        source = '<ruby>漢字<rp>（</rp><rt>かんじ</rt><rp>）</rp></ruby>'
        self.assertEqual(ex.parenthesizeRuby(source), source)

    def test_block_with_span_class_rp_is_returned_unchanged(self):
        source = ('<ruby class="yomigana"><rb>美術家</rb>'
                  '<span class="rp">（</span><rt>びじゅつか</rt>'
                  '<span class="rp">）</span></ruby>')
        self.assertEqual(ex.parenthesizeRuby(source), source)

    def test_span_with_rp_among_several_classes_still_counts(self):
        source = ('<ruby><rb>x</rb><span class="foo rp bar">（</span>'
                  '<rt>y</rt></ruby>')
        self.assertEqual(ex.parenthesizeRuby(source), source)

    def test_a_class_merely_containing_rp_does_not_count(self):
        source = '<ruby><rb>x</rb><span class="rp-like">z</span><rt>y</rt></ruby>'
        self.assertIn('（y）', ex.parenthesizeRuby(source))

    def test_text_without_ruby_is_untouched(self):
        source = 'Ordinary text with <span class="rp">no ruby</span> around it.'
        self.assertEqual(ex.parenthesizeRuby(source), source)

    def test_each_block_is_judged_separately(self):
        source = ('<ruby>甲<rt>こう</rt></ruby>'
                  '<ruby>乙<rp>（</rp><rt>おつ</rt><rp>）</rp></ruby>')
        result = ex.parenthesizeRuby(source)
        self.assertIn('<rt>（こう）</rt>', result)
        self.assertIn('<rt>おつ</rt>', result)


class RubyExtractionTests(unittest.TestCase):

    def test_annotation_with_rp_keeps_base_and_reading(self):
        result = clean('旧姓：加藤<ruby><rb>由美</rb><rp>（</rp>'
                       '<rt>よしみ</rt><rp>）</rp></ruby>')
        self.assertIn('加藤由美（よしみ）', result)

    def test_annotation_without_an_rb_element(self):
        result = clean('<ruby>漢字<rt>かんじ</rt></ruby>')
        self.assertIn('漢字（かんじ）', result)

    def test_base_and_reading_are_never_fused(self):
        result = clean('前<ruby>漢字<rt>かんじ</rt></ruby>後')
        self.assertIn('前漢字（かんじ）後', result)
        self.assertNotIn('漢字かんじ', result)

    def test_reading_is_not_parenthesized_twice(self):
        result = clean('<ruby><rb>東京</rb><rp>（</rp>'
                       '<rt>とうきょう</rt><rp>）</rp></ruby>')
        self.assertIn('東京（とうきょう）', result)
        self.assertNotIn('（（', result)

    def test_attributes_on_the_ruby_tag_do_not_prevent_removal(self):
        result = clean('<ruby class="yomigana" style="font-size:1em">'
                       '<rb>日本</rb><rt>にほん</rt></ruby>')
        self.assertIn('日本（にほん）', result)
        self.assertNotIn('yomigana', result)

    def test_per_character_readings_are_each_parenthesized(self):
        result = clean('<ruby>漢<rt>かん</rt>字<rt>じ</rt></ruby>')
        self.assertIn('漢（かん）字（じ）', result)

    def test_rtc_second_reading_is_kept(self):
        result = clean('<ruby><rb>東京</rb><rt>とうきょう</rt>'
                       '<rtc><rt>Tokyo</rt></rtc></ruby>')
        self.assertIn('東京（とうきょう）（Tokyo）', result)

    def test_several_annotations_in_one_line(self):
        result = clean('<ruby>山<rt>やま</rt></ruby>と'
                       '<ruby>川<rt>かわ</rt></ruby>')
        self.assertIn('山（やま）と川（かわ）', result)

    def test_no_escaped_tag_markup_survives(self):
        result = clean('加藤<ruby><rb>由美</rb><rp>（</rp>'
                       '<rt>よしみ</rt><rp>）</rp></ruby>')
        for fragment in ('&lt;', '&gt;', '<ruby', '</ruby', '<rb', '<rt', '<rp'):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, result)


class YomiganaTemplateTests(unittest.TestCase):
    """jawiki's Template:読み仮名, reduced. Its parentheses are spans,
    which is what makes the fallback detection more than a tag-name
    check."""

    templates = {
        'Template:読み仮名': (
            '<ruby class="yomigana" style="ruby-position:inline">'
            '<rb style="display:inline">{{{1}}}</rb>'
            '<span class="rp">（</span>'
            '<rt style="display:inline">{{{2}}}</rt>'
            '<span class="rp">）</span></ruby>'),
    }

    def test_opening_sentence_keeps_its_reading(self):
        result = clean("{{読み仮名|'''美術家'''|びじゅつか}}とは、", self.templates)
        self.assertIn('美術家（びじゅつか）とは、', result)

    def test_reading_is_not_parenthesized_twice(self):
        result = clean("{{読み仮名|'''美術家'''|びじゅつか}}", self.templates)
        self.assertNotIn('（（', result)
        self.assertNotIn('））', result)

    def test_empty_parentheses_are_not_produced(self):
        # The regression this replaced: rt discarded with its content,
        # the span parentheses kept, leaving 美術家（）.
        result = clean("{{読み仮名|'''美術家'''|びじゅつか}}とは、", self.templates)
        self.assertNotIn('（）', result)


class RubyTemplateEndToEndTests(unittest.TestCase):
    """jawiki's Template:Ruby, which marks its parentheses up as real
    <rp> elements. The reduced definition keeps the space left where
    the conditional class attribute expands to nothing."""

    templates = {
        'Template:Ruby': ('<ruby {{#if:{{{class|}}}|class="{{{class}}}"}}>'
                          '<rb>{{{1}}}</rb><rp>（</rp><rt>{{{2}}}</rt>'
                          '<rp>）</rp></ruby>'),
    }

    def test_expanded_template_keeps_base_and_reading(self):
        result = clean('旧姓：加藤{{ruby|由美|よしみ}}', self.templates)
        self.assertIn('加藤由美（よしみ）', result)

    def test_expanded_template_with_a_class_argument(self):
        result = clean('{{ruby|由美|よしみ|class=yomigana}}', self.templates)
        self.assertIn('由美（よしみ）', result)
        self.assertNotIn('yomigana', result)

    def test_surrounding_sentence_is_left_intact(self):
        result = clean('妻は少女漫画家のみかみなち(旧姓：加藤{{ruby|由美|よしみ}})と結婚。',
                       self.templates)
        self.assertIn('妻は少女漫画家のみかみなち(旧姓：加藤由美（よしみ）)と結婚。', result)


if __name__ == '__main__':
    unittest.main()
