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

They are now split across the two lists by which half of the
annotation they hold. ruby and rb are in ignoredTags, so the base text
survives with the tags removed. rt, rtc and rp are in
discardElements, so the reading goes with them: keeping it would put a
second spelling of the same word into the running text, and the
majority of ruby markup omits rp, which would leave base and reading
fused with nothing between them -- 漢字かんじ rather than 漢字.

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

    def test_ruby_and_rb_keep_their_content(self):
        self.assertIn('ruby', ex.ignoredTags)
        self.assertIn('rb', ex.ignoredTags)

    def test_reading_elements_are_discarded_with_their_content(self):
        self.assertIn('rt', ex.discardElements)
        self.assertIn('rtc', ex.discardElements)
        self.assertIn('rp', ex.discardElements)

    def test_no_ruby_tag_is_in_both_lists(self):
        for tag in ('ruby', 'rb', 'rt', 'rtc', 'rp'):
            with self.subTest(tag=tag):
                self.assertFalse(tag in ex.ignoredTags and tag in ex.discardElements)


class RubyExtractionTests(unittest.TestCase):

    def test_full_annotation_leaves_only_the_base_text(self):
        result = clean('旧姓：加藤<ruby><rb>由美</rb><rp>（</rp>'
                       '<rt>よしみ</rt><rp>）</rp></ruby>')
        self.assertIn('加藤由美', result)
        self.assertNotIn('よしみ', result)
        self.assertNotIn('（', result)

    def test_annotation_without_an_rb_element_keeps_its_base_text(self):
        # The common shorter form: base text sits directly inside
        # <ruby> with no <rb> around it.
        result = clean('<ruby>漢字<rt>かんじ</rt></ruby>')
        self.assertIn('漢字', result)
        self.assertNotIn('かんじ', result)

    def test_annotation_without_rp_does_not_fuse_base_and_reading(self):
        result = clean('前<ruby>漢字<rt>かんじ</rt></ruby>後')
        self.assertIn('前漢字後', result)
        self.assertNotIn('漢字かんじ', result)

    def test_attributes_on_the_ruby_tag_do_not_prevent_removal(self):
        result = clean('<ruby class="yomigana" style="font-size:1em">'
                       '<rb>日本</rb><rt>にほん</rt></ruby>')
        self.assertIn('日本', result)
        self.assertNotIn('にほん', result)
        self.assertNotIn('yomigana', result)

    def test_rtc_second_reading_is_discarded(self):
        result = clean('<ruby><rb>東京</rb><rt>とうきょう</rt>'
                       '<rtc><rt>Tokyo</rt></rtc></ruby>')
        self.assertIn('東京', result)
        self.assertNotIn('とうきょう', result)
        self.assertNotIn('Tokyo', result)

    def test_several_annotations_in_one_line_are_each_reduced(self):
        result = clean('<ruby>山<rt>やま</rt></ruby>と'
                       '<ruby>川<rt>かわ</rt></ruby>')
        self.assertIn('山と川', result)

    def test_no_escaped_tag_markup_survives(self):
        result = clean('加藤<ruby><rb>由美</rb><rp>（</rp>'
                       '<rt>よしみ</rt><rp>）</rp></ruby>')
        for fragment in ('&lt;', '&gt;', '<ruby', '</ruby', '<rb', '<rt', '<rp'):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, result)


class RubyTemplateEndToEndTests(unittest.TestCase):
    """jawiki's Template:Ruby emits the markup rather than the article
    doing it inline, so the tags arrive after template expansion. The
    reduced definition here keeps that shape, including the space left
    where the conditional class attribute expands to nothing."""

    templates = {
        'Template:Ruby': ('<ruby {{#if:{{{class|}}}|class="{{{class}}}"}}>'
                          '<rb>{{{1}}}</rb><rp>（</rp><rt>{{{2}}}</rt>'
                          '<rp>）</rp></ruby>'),
    }

    def test_expanded_template_leaves_only_the_base_text(self):
        result = clean('旧姓：加藤{{ruby|由美|よしみ}}', self.templates)
        self.assertIn('加藤由美', result)
        self.assertNotIn('よしみ', result)

    def test_expanded_template_with_a_class_argument(self):
        result = clean('{{ruby|由美|よしみ|class=yomigana}}', self.templates)
        self.assertIn('由美', result)
        self.assertNotIn('yomigana', result)

    def test_surrounding_sentence_is_left_intact(self):
        result = clean('妻は少女漫画家のみかみなち(旧姓：加藤{{ruby|由美|よしみ}})と結婚。',
                       self.templates)
        self.assertIn('妻は少女漫画家のみかみなち(旧姓：加藤由美)と結婚。', result)


if __name__ == '__main__':
    unittest.main()
