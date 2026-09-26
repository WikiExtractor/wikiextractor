"""
Tests for dropHiddenElements(): removing elements a page hides with
display:none, content and all.

MediaWiki templates use display:none for machinery a reader never
sees -- update banners, sort keys, metadata for gadgets and apps --
so its content does not belong in extracted text however cleanly it
parses.

Two real cases drove this.

jawiki's As-of banner on bhwiki's भारत, whose hidden <sup> survived
into the article as literal "[[update]]" five times over. Its shape
is worth keeping in mind: {{fullurl:}} is unsupported and expands to
nothing, leaving "[ &#91;update&#93;]"; link processing correctly
leaves that alone; unescape then makes it "[ [update]]"; and the
punctuation cleanup collapses "[ " to "[", minting link syntax after
the parser has stopped looking. Removing the hidden element removes
the whole problem at its source.

enwiki's Template:Short description, whose output carries
    class="shortdescription nomobile noexcerpt noprint searchaux"
    style="display:none"
-- every one of those a don't-show marker. The description reaches
apps and search through the {{SHORTDESC:}} magic word in the same
template, which sets a page property; the div is a hidden copy.

Nesting is the part that needs care: a plain <span> inside a hidden
one means the first </span> is not the matching close.

Run with:
    python -m unittest tests.test_hidden_elements -v
or, from the tests/ directory:
    python -m unittest test_hidden_elements -v
"""

import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.extract as ex


def clean(wikitext, templates=None):
    extractor = ex.Extractor(1, "1", "https://x", "Test Article", [],
                             templates=templates or {}, templatePrefix='Template:')
    return '\n'.join(extractor.clean_text(wikitext, expand_templates=True))


class DropHiddenElementsTests(unittest.TestCase):
    """The pass in isolation."""

    def test_a_hidden_span_goes_with_its_content(self):
        self.assertEqual(
            ex.dropHiddenElements('a<span style="display:none">gone</span>b'), 'ab')

    def test_a_visible_span_is_untouched(self):
        source = 'a<span style="color:red">keep</span>b'
        self.assertEqual(ex.dropHiddenElements(source), source)

    def test_another_display_value_is_untouched(self):
        source = 'a<span style="display:inline">keep</span>b'
        self.assertEqual(ex.dropHiddenElements(source), source)

    def test_a_property_merely_containing_none_is_untouched(self):
        source = 'a<span style="width:none">keep</span>b'
        self.assertEqual(ex.dropHiddenElements(source), source)

    def test_whitespace_around_the_colon(self):
        self.assertEqual(
            ex.dropHiddenElements('a<span style="display: none;">gone</span>b'), 'ab')

    def test_single_quoted_style(self):
        self.assertEqual(
            ex.dropHiddenElements("a<span style='display:none'>gone</span>b"), 'ab')

    def test_alongside_other_properties(self):
        self.assertEqual(
            ex.dropHiddenElements('a<span style="color:red;display:none">gone</span>b'),
            'ab')

    def test_important_qualifier(self):
        self.assertEqual(
            ex.dropHiddenElements('a<span style="display:none !important">x</span>b'),
            'ab')

    def test_case_is_ignored(self):
        self.assertEqual(
            ex.dropHiddenElements('a<DIV STYLE="DISPLAY:NONE">gone</DIV>b'), 'ab')

    def test_any_tag_name(self):
        for tag in ('span', 'div', 'sup', 's', 'small', 'td'):
            with self.subTest(tag=tag):
                source = 'a<%s style="display:none">gone</%s>b' % (tag, tag)
                self.assertEqual(ex.dropHiddenElements(source), 'ab')

    def test_other_attributes_before_the_style(self):
        self.assertEqual(
            ex.dropHiddenElements('a<span class="x y" id="z" style="display:none">'
                                  'gone</span>b'),
            'ab')

    def test_several_in_one_line(self):
        self.assertEqual(
            ex.dropHiddenElements('a<span style="display:none">x</span>'
                                  'b<span style="display:none">y</span>c'),
            'abc')

    def test_text_with_no_hidden_element_is_returned_unchanged(self):
        source = 'Ordinary text with <span>a span</span> in it.'
        self.assertEqual(ex.dropHiddenElements(source), source)


class NestingTests(unittest.TestCase):
    """Finding the matching close tag rather than the first one."""

    def test_a_same_name_tag_inside_does_not_close_it_early(self):
        self.assertEqual(
            ex.dropHiddenElements(
                'a<span style="display:none">hide<span>inner</span>more</span>b'),
            'ab')

    def test_two_levels_of_nesting(self):
        self.assertEqual(
            ex.dropHiddenElements(
                'a<span style="display:none">1<span>2<span>3</span>4</span>5</span>b'),
            'ab')

    def test_a_different_tag_inside_is_no_obstacle(self):
        self.assertEqual(
            ex.dropHiddenElements(
                'a<div style="display:none">x<span>y</span>z</div>b'),
            'ab')

    def test_a_hidden_element_inside_a_visible_one(self):
        self.assertEqual(
            ex.dropHiddenElements(
                'a<span class="v">keep<span style="display:none">gone</span>'
                'also</span>b'),
            'a<span class="v">keepalso</span>b')

    def test_a_following_element_survives(self):
        self.assertEqual(
            ex.dropHiddenElements(
                'a<span style="display:none">x<span>y</span></span>'
                '<span>after</span>b'),
            'a<span>after</span>b')


class MalformedTests(unittest.TestCase):
    """A tag with no matching close loses only the tag, since the
    content after it belongs to someone else."""

    def test_self_closing(self):
        self.assertEqual(ex.dropHiddenElements('a<span style="display:none" />b'), 'ab')

    def test_no_close_tag_keeps_the_rest_of_the_text(self):
        self.assertEqual(
            ex.dropHiddenElements('a<span style="display:none">dangling'), 'adangling')

    def test_an_unbalanced_one_does_not_swallow_a_later_element(self):
        result = ex.dropHiddenElements(
            'a<span style="display:none">dangling<div>later</div>')
        self.assertIn('later', result)

    def test_an_unclosed_element_does_not_borrow_a_nested_close(self):
        # Counting is what makes this work: the </span> balances the
        # inner open, leaving the outer one unmatched, so the text
        # between them survives. A non-greedy match would take that
        # close as the outer's and swallow the text.
        self.assertEqual(
            ex.dropHiddenElements(
                '<span style="display:none"> lots of text '
                '<span style="display:none">foo</span>'),
            ' lots of text ')

    def test_an_unclosed_element_keeps_a_plain_nested_one(self):
        self.assertEqual(
            ex.dropHiddenElements(
                '<span style="display:none"> lots of text <span>foo</span>'),
            ' lots of text <span>foo</span>')

    def test_a_closed_one_is_removed_even_beside_an_unclosed_one(self):
        self.assertEqual(
            ex.dropHiddenElements(
                '<span style="display:none">a</span> lots of text '
                '<span style="display:none">b'),
            ' lots of text b')

    def test_an_unbalanced_one_terminates(self):
        # The scan has to move past a tag it could not pair, or loop.
        self.assertEqual(
            ex.dropHiddenElements('<span style="display:none"><span '
                                  'style="display:none">'), '')


class EndToEndTests(unittest.TestCase):

    def test_hidden_content_does_not_reach_the_output(self):
        self.assertEqual(clean('before <span style="display:none">hidden</span> after'),
                         'before after')

    def test_it_runs_before_the_tags_are_stripped(self):
        # span is in ignoredTags, so once its tags are gone there is
        # no element left to recognize as hidden.
        self.assertNotIn('hidden', clean('x <span style="display:none">hidden</span> y'))

    def test_visible_markup_still_has_its_tags_stripped(self):
        result = clean('x <span style="color:red">visible</span> y')
        self.assertIn('visible', result)
        self.assertNotIn('<span', result)


class AsOfBannerTests(unittest.TestCase):
    """The bhwiki भारत case, reduced: a hidden banner whose content
    turns into link syntax on the way out."""

    templates = {
        'Template:As of': (
            '{{{1|}}} तक ले <sup class="plainlinks noprint asof-tag update" '
            'style="display:none;">[{{fullurl:{{PAGENAME}}|action=edit}} '
            '&#91;update&#93;]</sup>'),
    }

    def test_no_link_syntax_survives(self):
        result = clean('{{As of|2011}}, 4,866 लाख', self.templates)
        self.assertNotIn('[[', result)
        self.assertNotIn('update', result)

    def test_the_sentence_around_it_is_intact(self):
        result = clean('{{As of|2011}}, 4,866 लाख', self.templates)
        self.assertIn('2011 तक ले, 4,866 लाख', result)


class ShortDescriptionTests(unittest.TestCase):
    """enwiki's Template:Short description, reduced. Its output is
    hidden in every skin; the description itself travels as a page
    property set by {{SHORTDESC:}}."""

    templates = {
        'Template:Short description': (
            '<div class="shortdescription nomobile noexcerpt noprint searchaux" '
            'style="display:none">{{{1|}}}</div>'),
    }

    def test_the_description_does_not_reach_the_output(self):
        result = clean('{{Short description|Anglican bishop (1848–1934)}}\n'
                       "William Willcox Perrin was an Anglican bishop.",
                       self.templates)
        self.assertNotIn('Anglican bishop (1848–1934)', result)

    def test_the_article_body_is_untouched(self):
        result = clean('{{Short description|Anglican bishop (1848–1934)}}\n'
                       "William Willcox Perrin was an Anglican bishop.",
                       self.templates)
        self.assertIn('William Willcox Perrin was an Anglican bishop.', result)


if __name__ == '__main__':
    unittest.main()
